"""推送成败判定测试：HTTP 2xx 但业务码失败，必须算失败。

起因：飞书 / 钉钉 / 企微的 webhook 都是「HTTP 200 + body 里带 code」，
只看 HTTP 状态码会把「签名错、被限流、机器人被移除」全判成推送成功。
一旦误判成功，去重基线就会被写入，这条活动以后再也不会重推 —— 永久丢失。

现在的判据是**按平台**的：只有平台明确说了成功才算成功。
「看不懂的响应」不再默认算成功（generic 通道除外，它保留兼容语义并单独标注）。

这个文件只调用判定函数本身，不读配置、不发通知、不联网。
用法：python tests/test_notify.py
"""
import sys

try:  # Windows 控制台默认 GBK，打 ✓/✗ 会直接抛 UnicodeEncodeError；统一按 UTF-8 输出
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from notify import _business_ok  # noqa: E402

# (响应体, 平台, 期望结果, 说明)
CASES = [
    # ---------- 飞书：code / StatusCode 都必须是 0 ----------
    ('{"code":0,"msg":"success"}', "feishu", True, "飞书 成功"),
    ('{"code":19001,"msg":"bad request"}', "feishu", False, "飞书 参数错"),
    ('{"code":9499,"msg":"bad request"}', "feishu", False, "飞书 限流"),
    ('{"StatusCode":0,"StatusMessage":"success"}', "feishu", True, "飞书 大写字段 成功"),
    ('{"StatusCode":99991400,"StatusMessage":"invalid"}', "feishu", False, "飞书 大写字段 失败"),
    ('{"code":"0"}', "feishu", True, "业务码是字符串 0"),
    ('{"code":"19001"}', "feishu", False, "业务码是字符串非 0"),
    ('{"code":" 0 "}', "feishu", True, "字符串 0 允许首尾空格"),
    ('{"code":0,"StatusCode":0}', "feishu", True, "两个成功字段同时出现"),
    ('{"code":0,"StatusCode":1}', "feishu", False, "冲突字段必须都为 0"),
    ('{"code":false}', "feishu", False, "布尔 false 不是业务码"),
    ('{"code":true}', "feishu", False, "布尔 true 不是业务码"),
    ('{"code":0.0}', "feishu", False, "小数不算成功"),
    ('{"code":null}', "feishu", False, "空值不算成功"),
    ('{"errcode":0}', "feishu", False, "飞书不认 errcode 作为成功凭据"),
    ('{}', "feishu", False, "空对象缺成功字段"),
    ('[]', "feishu", False, "数组不是成功响应"),
    ('{"message":"pong"}', "feishu", False, "没有业务码不能算成功"),
    ('ok', "feishu", False, "非 JSON 响应"),
    ('', "feishu", False, "空响应"),

    # ---------- 钉钉 / 企微：只认 errcode ----------
    ('{"errcode":0,"errmsg":"ok"}', "dingtalk", True, "钉钉 成功"),
    ('{"errcode":310000,"errmsg":"sign not match"}', "dingtalk", False, "钉钉 签名错"),
    ('{"errcode":"0","errmsg":"ok"}', "dingtalk", True, "钉钉 字符串 0"),
    ('{"code":0}', "dingtalk", False, "钉钉缺 errcode 不能算成功"),
    ('{"errcode":0,"errmsg":"ok"}', "wecom", True, "企微 成功"),
    ('{"errcode":93000,"errmsg":"invalid webhook url"}', "wecom", False, "企微 地址失效"),
    ('{}', "wecom", False, "企微 空对象"),

    # ---------- QQ（OneBot）：retcode 必须 0，status 存在时必须 ok ----------
    ('{"retcode":0,"status":"ok"}', "qq", True, "OneBot 成功"),
    ('{"retcode":"0","status":"ok"}', "qq", True, "OneBot 字符串 0"),
    ('{"retcode":0}', "qq", True, "OneBot 没有 status 也算成功"),
    ('{"retcode":1200,"status":"failed"}', "qq", False, "OneBot 失败"),
    ('{"retcode":0,"status":"failed"}', "qq", False, "OneBot status 与 retcode 矛盾"),
    ('{"status":"ok"}', "qq", False, "OneBot 缺 retcode"),

    # ---------- generic：保留旧兼容语义（弱确认） ----------
    ('{"code":0}', "generic", True, "generic 明确成功码"),
    ('{"code":1}', "generic", False, "generic 明确错误码仍判失败"),
    ('{"errcode":7}', "generic", False, "generic 明确错误码"),
    ('{"status":"failed"}', "generic", False, "generic 明确失败状态"),
    ('{"status":"ok"}', "generic", True, "generic 明确成功状态"),
    ('{"message":"pong"}', "generic", True, "generic 无业务码按 HTTP 2xx（弱确认）"),
    ('{}', "generic", True, "generic 空对象（弱确认）"),
    ('[]', "generic", True, "generic 非对象（弱确认）"),
    ('ok', "generic", True, "generic 非 JSON（弱确认）"),
    ('', "generic", True, "generic 空响应（弱确认）"),

    # ---------- 未登记的平台名按 generic 处理 ----------
    ('{"message":"pong"}', "smtp", True, "未知平台走 generic 兼容"),
    ('{"code":1}', "smtp", False, "未知平台仍尊重明确错误码"),
]


def main() -> int:
    failed = []
    for body, platform, expect, desc in CASES:
        got = _business_ok(body, platform)
        mark = "✓" if got == expect else "✗"
        if got != expect:
            failed.append(desc)
        print("  %s %-10s %-24s 期望 %-5s 实际 %s"
              % (mark, platform, desc, expect, got))

    print("\n共 %d 项，失败 %d 项" % (len(CASES), len(failed)))
    if failed:
        for item in failed:
            print("  ✗", item)
        return 1
    print("== 通过 ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
