"""报名签名：CNG AES-128-ECB（bcrypt.dll）——离线契约实现。

仅负责签名计算，不读时钟、不访问网络、不回退自造算法。毫秒时间戳由主循环按
business_clock 现算后传入（build_signup_hash），保证签名时刻与真实时间一致。
"""
from __future__ import annotations

import sys

# 学校脚本内嵌的 AES-128 key（16 字节），仅用于签名，不写入日志。
_SIGN_KEY = b"PrWHFY6wDrmtr5TM"

# CNG 常量
_BCRYPT_AES = "AES"
_BCRYPT_CHAINING_MODE = "ChainingMode"
_BCRYPT_CHAIN_MODE_ECB = "ChainingModeECB"
_STATUS_SUCCESS = 0


class SignerUnavailable(RuntimeError):
    """CNG 签名不可用：非 Windows、bcrypt 缺失或任一 API 失败。

    不退回自造算法；调用方应据此降级为 click 兜底，异常消息不含 token/完整 URL。
    """


def pkcs7_pad(data: bytes) -> bytes:
    """PKCS7 填充：补齐 1..16 字节（整块也补一整块 16）。"""
    pad_len = 16 - (len(data) % 16)
    return data + bytes([pad_len]) * pad_len


def _encrypt_block_cng(plaintext_padded: bytes, key: bytes) -> bytes:
    """用 CNG（bcrypt.dll）AES-128-ECB 加密一段已填充（16 字节倍数）明文。

    所有 NTSTATUS 均检查；失败抛 SignerUnavailable，不自造算法。所有路径
    DestroyKey / CloseAlgorithmProvider；key 对象缓冲区在 key 句柄销毁前保持存活。
    """
    import ctypes
    from ctypes import wintypes

    if sys.platform != "win32":
        raise SignerUnavailable("仅 Windows 提供 CNG 签名")
    if len(plaintext_padded) % 16 != 0:
        raise ValueError("加密块只接受 16 字节倍数明文")
    if len(key) != 16:
        raise ValueError("AES-128 需要 16 字节 key")

    try:
        bcrypt = ctypes.WinDLL("bcrypt.dll")
    except OSError as exc:
        raise SignerUnavailable("无法加载 bcrypt.dll") from exc

    ULONG = wintypes.ULONG
    BYTE_P = ctypes.POINTER(ctypes.c_ubyte)
    ULONG_P = ctypes.POINTER(ULONG)
    HANDLE_P = ctypes.POINTER(ctypes.c_void_p)

    # NTSTATUS 是有符号 32 位 LONG；STATUS_SUCCESS == 0
    bcrypt.BCryptOpenAlgorithmProvider.argtypes = [HANDLE_P, ctypes.c_wchar_p, ctypes.c_wchar_p, ULONG]
    bcrypt.BCryptOpenAlgorithmProvider.restype = wintypes.LONG
    bcrypt.BCryptSetProperty.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, BYTE_P, ULONG, ULONG]
    bcrypt.BCryptSetProperty.restype = wintypes.LONG
    bcrypt.BCryptGetProperty.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, BYTE_P, ULONG, ULONG_P, ULONG]
    bcrypt.BCryptGetProperty.restype = wintypes.LONG
    bcrypt.BCryptGenerateSymmetricKey.argtypes = [ctypes.c_void_p, HANDLE_P, BYTE_P, ULONG, BYTE_P, ULONG, ULONG]
    bcrypt.BCryptGenerateSymmetricKey.restype = wintypes.LONG
    bcrypt.BCryptEncrypt.argtypes = [
        ctypes.c_void_p, BYTE_P, ULONG, wintypes.LPVOID, BYTE_P, ULONG, BYTE_P, ULONG, ULONG_P, ULONG,
    ]
    bcrypt.BCryptEncrypt.restype = wintypes.LONG
    bcrypt.BCryptDestroyKey.argtypes = [ctypes.c_void_p]
    bcrypt.BCryptDestroyKey.restype = wintypes.LONG
    bcrypt.BCryptCloseAlgorithmProvider.argtypes = [ctypes.c_void_p, ULONG]
    bcrypt.BCryptCloseAlgorithmProvider.restype = wintypes.LONG

    def _check(status: int, what: str) -> None:
        if status != _STATUS_SUCCESS:
            raise SignerUnavailable(f"{what} 失败（NTSTATUS 0x{status & 0xFFFFFFFF:08X}）")

    alg_handle = ctypes.c_void_p()
    key_handle = ctypes.c_void_p()
    try:
        _check(
            bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(alg_handle), _BCRYPT_AES, None, 0),
            "OpenAlgorithmProvider(AES)",
        )
        # ChainingModeECB：pbInput 为含结尾 NUL 的宽字符字节
        chain = (_BCRYPT_CHAIN_MODE_ECB + "\x00").encode("utf-16-le")
        chain_buf = (ctypes.c_ubyte * len(chain)).from_buffer_copy(chain)
        _check(
            bcrypt.BCryptSetProperty(alg_handle, _BCRYPT_CHAINING_MODE, chain_buf, len(chain), 0),
            "SetProperty(ChainingModeECB)",
        )
        # 查询 key 对象所需缓冲区长度
        object_len_buf = (ctypes.c_ubyte * 4)()
        got_len = ULONG(0)
        _check(
            bcrypt.BCryptGetProperty(
                alg_handle, "ObjectLength", object_len_buf, 4, ctypes.byref(got_len), 0,
            ),
            "GetProperty(ObjectLength)",
        )
        object_len = int.from_bytes(bytes(object_len_buf), "little")
        if object_len <= 0:
            raise SignerUnavailable("ObjectLength 非法")
        # key 对象缓冲区必须在 key 句柄存活期间保持有效（finally 前不释放）
        key_object = (ctypes.c_ubyte * object_len)()
        key_secret = (ctypes.c_ubyte * len(key)).from_buffer_copy(key)
        _check(
            bcrypt.BCryptGenerateSymmetricKey(
                alg_handle, ctypes.byref(key_handle), key_object, object_len, key_secret, len(key), 0,
            ),
            "GenerateSymmetricKey",
        )
        input_buf = (ctypes.c_ubyte * len(plaintext_padded)).from_buffer_copy(plaintext_padded)
        output_buf = (ctypes.c_ubyte * len(plaintext_padded))()
        out_len = ULONG(0)
        _check(
            bcrypt.BCryptEncrypt(
                key_handle, input_buf, len(plaintext_padded), None, None, 0,
                output_buf, len(plaintext_padded), ctypes.byref(out_len), 0,
            ),
            "Encrypt",
        )
        return bytes(output_buf[: out_len.value])
    finally:
        # 所有路径释放句柄；key 对象随函数返回自然释放（晚于 key 句柄销毁）
        if key_handle:
            bcrypt.BCryptDestroyKey(key_handle)
        if alg_handle:
            bcrypt.BCryptCloseAlgorithmProvider(alg_handle, 0)


def build_signup_hash(activity_id: str, epoch_ms: int) -> str:
    """按学校脚本契约算报名 hash：AES-128-ECB(UTF-8('id,ms'), PKCS7) 的小写 hex。

    activity_id 非空且不含逗号/换行；epoch_ms 为正整数（非 bool）。不读时钟。
    """
    if not activity_id or any(ch in activity_id for ch in (",", "\n", "\r")):
        raise ValueError("activity_id 非空且不含逗号/换行")
    if isinstance(epoch_ms, bool) or not isinstance(epoch_ms, int) or epoch_ms <= 0:
        raise ValueError("epoch_ms 为正整数")
    plain = f"{activity_id},{epoch_ms}".encode("utf-8")
    padded = pkcs7_pad(plain)
    ciphertext = _encrypt_block_cng(padded, _SIGN_KEY)
    return ciphertext.hex().lower()
