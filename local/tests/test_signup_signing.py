"""报名签名离线测试：NIST 块向量、PKCS7、契约与失败路径。

Windows 上 NIST 向量直接实跑 CNG（不可 mock 替代）；非 Windows 平台标记
blocked/skip。模拟块加密器仅用于校验喂入的已填充 UTF-8 明文与小写 hex 契约。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

LOCAL = Path(__file__).resolve().parents[1]
if str(LOCAL) not in sys.path:
    sys.path.insert(0, str(LOCAL))

import signup_signing
from signup_signing import SignerUnavailable, build_signup_hash, pkcs7_pad


class Pkcs7PadTests(unittest.TestCase):
    def test_pads_fifteen_to_sixteen(self):
        self.assertEqual(len(pkcs7_pad(b"0" * 15)), 16)

    def test_pads_sixteen_to_thirty_two(self):
        out = pkcs7_pad(b"0" * 16)
        self.assertEqual(len(out), 32)
        self.assertEqual(out[-16:], bytes([16]) * 16)

    def test_pads_seventeen_to_thirty_two(self):
        out = pkcs7_pad(b"0" * 17)
        self.assertEqual(len(out), 32)
        self.assertEqual(out[-15:], bytes([15]) * 15)

    def test_pads_whole_block_by_full_block(self):
        out = pkcs7_pad(b"")
        self.assertEqual(len(out), 16)
        self.assertEqual(out, bytes([16]) * 16)


@unittest.skipUnless(sys.platform == "win32", "NIST 向量需实跑 Windows CNG，非 Windows 标 blocked")
class NativeNistVectorTests(unittest.TestCase):
    """AES-128-ECB 经典块向量：直接验 CNG 无填充块加密（真实，不 mock）。"""

    def test_nist_aes128_ecb_block_vector(self):
        key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        plain = bytes.fromhex("00112233445566778899aabbccddeeff")
        expect = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
        self.assertEqual(signup_signing._encrypt_block_cng(plain, key), expect)


class MockCipherContractTests(unittest.TestCase):
    """模拟块加密器：校验喂入的是已填充 UTF-8 明文，输出为小写 hex。"""

    def _capture(self, activity_id: str, epoch_ms: int):
        import hashlib

        seen = {}

        def fake(plaintext_padded: bytes, key: bytes) -> bytes:
            seen["padded"] = plaintext_padded
            seen["key"] = key
            # 确定但随输入变化的伪密文（模拟真实加密器）：校验 hex 契约与毫秒敏感性
            return hashlib.sha256(plaintext_padded).digest()

        with mock.patch.object(signup_signing, "_encrypt_block_cng", fake):
            out = build_signup_hash(activity_id, epoch_ms)
        return out, seen

    def test_plaintext_is_padded_utf8_of_id_and_ms(self):
        _, seen = self._capture("00000000000000000000000000000000", 1793289600000)
        raw = "00000000000000000000000000000000,1793289600000".encode("utf-8")
        self.assertEqual(seen["padded"], pkcs7_pad(raw))
        self.assertEqual(seen["key"], signup_signing._SIGN_KEY)

    def test_output_is_lowercase_hex(self):
        out, _ = self._capture("act-1", 1793289600000)
        self.assertEqual(out, out.lower())
        self.assertRegex(out, r"^[0-9a-f]+$")
        self.assertEqual(len(out), 64)  # 32 字节密文

    def test_same_input_is_deterministic(self):
        a, _ = self._capture("act-1", 1793289600000)
        b, _ = self._capture("act-1", 1793289600000)
        self.assertEqual(a, b)

    def test_epoch_ms_change_changes_signature(self):
        a, _ = self._capture("act-1", 1793289600000)
        b, _ = self._capture("act-1", 1793289600001)
        self.assertNotEqual(a, b)


class InputValidationTests(unittest.TestCase):
    def _boom(self, *a, **k):
        raise AssertionError("非法输入不应触达加密器")

    def test_empty_activity_id_rejected(self):
        with mock.patch.object(signup_signing, "_encrypt_block_cng", self._boom):
            with self.assertRaises(ValueError):
                build_signup_hash("", 1793289600000)

    def test_comma_in_activity_id_rejected(self):
        with mock.patch.object(signup_signing, "_encrypt_block_cng", self._boom):
            with self.assertRaises(ValueError):
                build_signup_hash("a,b", 1793289600000)

    def test_newline_in_activity_id_rejected(self):
        with mock.patch.object(signup_signing, "_encrypt_block_cng", self._boom):
            with self.assertRaises(ValueError):
                build_signup_hash("a\nb", 1793289600000)

    def test_non_int_epoch_rejected(self):
        with mock.patch.object(signup_signing, "_encrypt_block_cng", self._boom):
            with self.assertRaises(ValueError):
                build_signup_hash("act-1", "1793289600000")

    def test_bool_epoch_rejected(self):
        with mock.patch.object(signup_signing, "_encrypt_block_cng", self._boom):
            with self.assertRaises(ValueError):
                build_signup_hash("act-1", True)

    def test_non_positive_epoch_rejected(self):
        with mock.patch.object(signup_signing, "_encrypt_block_cng", self._boom):
            with self.assertRaises(ValueError):
                build_signup_hash("act-1", 0)


class NativeFailureTests(unittest.TestCase):
    def test_api_failure_raises_signer_unavailable(self):
        # 打桩 bcrypt：OpenAlgorithmProvider 返回失败 NTSTATUS -> SignerUnavailable。
        # __getattr__ 自动补齐所有 BCrypt* 属性（可设 argtypes/restype 的可调用）。
        class _FakeFn:
            def __init__(self, ret):
                self.argtypes = None
                self.restype = None
                self._ret = ret

            def __call__(self, *a, **k):
                return self._ret

        class _FakeBcrypt:
            def __getattr__(self, name):
                ret = 0xC0000001 if name == "BCryptOpenAlgorithmProvider" else 0
                fn = _FakeFn(ret)
                setattr(self, name, fn)
                return fn

        with mock.patch("ctypes.WinDLL", return_value=_FakeBcrypt()):
            with self.assertRaises(SignerUnavailable):
                signup_signing._encrypt_block_cng(b"0" * 16, b"0" * 16)

    @unittest.skipIf(sys.platform == "win32", "本机即 Windows，非 Windows 分支无从触发")
    def test_non_windows_raises_signer_unavailable(self):
        with self.assertRaises(SignerUnavailable):
            build_signup_hash("act-1", 1793289600000)


if __name__ == "__main__":
    unittest.main()
