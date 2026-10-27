"""推送判定与公共群文案契约测试。

两件事在这里钉死：

1. **推送成败判定**：HTTP 2xx 但业务码失败，必须算失败。
   起因：飞书 / 钉钉 / 企微的 webhook 都是「HTTP 200 + body 里带 code」，
   只看 HTTP 状态码会把「签名错、被限流、机器人被移除」全判成推送成功。
   一旦误判成功，去重基线就会被写入，这条活动以后再也不会重推 —— 永久丢失。
   现在的判据是**按平台**的：只有平台明确说了成功才算成功。

2. **公共群文案契约**：公共卡片与 query.py 的纯文本兜底只写学校公开状态
   （待开放报名 / 报名已开放 / 报名已开放一段时间 / 报名已结束 / 报名时间待确认），
   不出现「碰运气」「抢名额」「提交次数」「已报名」这类个人执行结论；
   私聊预报名卡片才保留预报名动作文案。两条通道的边界是产品边界，不是文案偏好。

这个文件只调用判定与卡片构造函数，不读配置、不发通知、不联网。
用法：python tests/test_notify.py
"""
import json
import sys

try:  # Windows 控制台默认 GBK，打 ✓/✗ 会直接抛 UnicodeEncodeError；统一按 UTF-8 输出
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from notify import _business_ok  # noqa: E402
import card                      # noqa: E402
import query                     # noqa: E402

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
    print("== 推送成败判定 ==")
    failed = check_business_ok()

    print("\n== 公共群文案契约 ==")
    failed += check_public_copy()

    print("\n" + ("== 通过 ==" if failed == 0 else "== 失败 %d 项 ==" % failed))
    return 0 if failed == 0 else 1


def check_business_ok() -> int:
    failed = []
    for body, platform, expect, desc in CASES:
        got = _business_ok(body, platform)
        mark = "✓" if got == expect else "✗"
        if got != expect:
            failed.append(desc)
        print("  %s %-10s %-24s 期望 %-5s 实际 %s"
              % (mark, platform, desc, expect, got))

    print("\n共 %d 项，失败 %d 项" % (len(CASES), len(failed)))
    for item in failed:
        print("  ✗", item)
    return len(failed)


# ---------- 公共群文案契约 ----------
# 这些词只允许出现在私聊预报名卡片里。公共群一旦出现，用户会以为
# 「机器人替我抢了/我报上了」，而实际动作根本没有发生。
PUBLIC_FORBIDDEN = ("碰运气", "抢名额", "提交次数", "预报名成功", "已报名",
                    "名额可能已满", "还能报名")

NOW = datetime(2026, 10, 27, 13, 0)

STATUS_CASES = [
    ({"status": "pending", "sign_start": "2026-10-28 18:00:00"}, "待开放报名"),
    ({"status": "open", "sign_start": "2026-10-27 12:00:00"}, "报名已开放"),
    ({"status": "open", "sign_start": "2026-10-27 02:00:00"}, "报名已开放一段时间"),
    ({"status": "closed", "sign_start": "2026-10-20 12:00:00"}, "报名已结束"),
    ({"status": "unknown", "sign_start": ""}, "报名时间待确认"),
]


def _public_cards() -> dict:
    """所有会进公共群的卡片。"""
    open_item = {
        "id": "e1", "name": "活动一", "type": "讲座", "place": "礼堂",
        "sign_start": "2026-10-27 12:00:00", "sign_end": "2026-10-29 18:00:00",
        "act_start": "2026-10-30 18:00:00", "checkin": "签到 17:30–18:00",
        "status": "open",
    }
    pending_item = dict(open_item, status="pending",
                        sign_start="2026-10-28 18:00:00")
    summary = {"pending": [pending_item], "open": [open_item],
               "open_count": 1, "pending_count": 1,
               "generated_at": "2026-10-27 13:00:00"}
    events = [{
        "id": "e1", "kind": "new_open", "status": "open", "name": "活动一",
        "type": "讲座", "place": "礼堂", "sign_start": "2026-10-27 08:00:00",
        "sign_end": "2026-10-29 18:00:00", "act_start": "2026-10-30 18:00:00",
        "discovered_at": "2026-10-27 13:00:00", "checkin": "",
    }]
    return {
        "digest": card.digest_card(summary),
        "pending": card.pending_card([pending_item], NOW),
        "open": card.open_card([open_item], NOW),
        "all": card.all_card([pending_item], [open_item], NOW),
        "rush": card.rush_card([pending_item], NOW),
        "event": card.public_event_card(events, NOW),
        "menu": card.menu_card(NOW),
    }


def _flatten(payload) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _button_values(payload) -> list:
    values = []
    for element in payload.get("elements") or []:
        for action in element.get("actions") or []:
            values.append(str(action.get("value") or ""))
    return values


def check_public_copy() -> int:
    failed = []
    checked = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal checked
        checked += 1
        print("  %s %s%s" % ("✓" if ok else "✗", label, "" if ok else "　" + detail))
        if not ok:
            failed.append(label)

    # 1) 五个状态词必须稳定
    for item, expect in STATUS_CASES:
        got = card.status_label(item, NOW)
        check("状态词 %-14s -> %s" % (expect, got), got == expect,
              "期望 %s，实际 %s" % (expect, got))

    # 2) 公共卡片不得出现个人执行结论
    for name, payload in _public_cards().items():
        blob = _flatten(payload)
        hits = [word for word in PUBLIC_FORBIDDEN if word in blob]
        check("公共卡片 %-7s 不含个人执行结论" % name, not hits, "命中 %s" % hits)

    # 3) 公共卡片按钮只能是查询视图（v:*），不能有 g:/u: 个人动作
    for name, payload in _public_cards().items():
        values = _button_values(payload)
        personal = [v for v in values if v.startswith(("g:", "u:"))]
        check("公共卡片 %-7s 只有查询按钮" % name, not personal, "命中 %s" % personal)

    # 4) 首次发现已开放的活动必须同时给两个时刻
    event_blob = _flatten(_public_cards()["event"])
    check("动态卡片同时给原定开放与本次发现",
          "原定开放" in event_blob and "本次发现" in event_blob)
    check("动态卡片写明报名已开放", "报名已开放" in event_blob)

    # 5) 私聊预报名卡片仍保留个人动作文案（这是它存在的意义）
    queued = [{"id": "e1", "name": "活动一", "type": "讲座",
               "sign_start": "2026-10-28 18:00:00", "sign_end": "2026-10-29 18:00:00"}]
    grab = card.grab_pending_card(queued, NOW)
    check("私聊卡片保留预报名动作", "预报名" in _flatten(grab))
    check("私聊卡片按钮带 g: 动作值",
          any(v.startswith("g:") for v in _button_values(grab)))

    # 6) query.py 的纯文本兜底与公共卡片同一语义
    #    这条路径只在卡片下发失败时出现，但它同样是公共群文案，
    #    不能一边说「报名已开放」一边说「碰碰运气」。
    open_row = {"id": "e1", "name": "活动一", "type": "讲座", "place": "礼堂",
                "sign_start": "2026-10-27 12:00:00", "sign_end": "2026-10-29 18:00:00"}
    open_text = query.render_open([open_row], NOW)
    check("query open 文案说明报名已开放", "报名已开放" in open_text)
    check("query open 文案给出自查动作", "可前往学校页面查看并报名" in open_text)
    forbidden_open = [w for w in ("碰碰运气", "抢名额", "名额可能已满")
                      if w in open_text]
    check("query open 文案不含占有性措辞", not forbidden_open,
          "命中 %s" % forbidden_open)
    empty_text = query.render_open([], NOW)
    check("query open 空列表只陈述状态", "没有报名已开放的活动" in empty_text)

    source = (ROOT / "scripts" / "query.py").read_text(encoding="utf-8")
    source_hits = [w for w in ("碰碰运气", "抢名额") if w in source]
    check("query.py 源码不再出现碰碰运气/抢名额", not source_hits,
          "命中 %s" % source_hits)

    print("\n共 %d 项，失败 %d 项" % (checked, len(failed)))
    for item in failed:
        print("  ✗", item)
    return len(failed)


if __name__ == "__main__":
    sys.exit(main())
