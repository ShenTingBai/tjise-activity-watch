"""抓取智慧元小元的学生活动，输出日报 Markdown + 结构化 JSON（纯 HTTP，不开浏览器）。

登录态来自 data/session.json（由 login.py 写入），请求时放在 token 头里。
接口：POST /dr/api/rjyHd/listActivity

退出码：
  0  正常
  2  登录态失效（token 过期或未登录），需要重跑 login.py
  3  接口返回异常或解析失败
"""

import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (ACTIVITY_API, DATA, REFERER, REPORTS, USER_AGENT,  # noqa: E402
                    business_now, load_config, load_session, load_state,
                    update_state)

# 接口一次能返回的上限远大于 100：实测 pageSize=500 会把当时全部 285 条一次给全
# （0.21s，code=1），pageSize=1000 结果相同。原先按 100 翻页，一次全量要打 3 个请求，
# 而 scan.py 每 5 分钟跑一轮，一天白多花近 600 次请求。取 500 留足增长空间。
PAGE_SIZE = 500

# 单次 HTTP 请求超时。默认 30 秒沿用原值；动态监测会传更小的值，
# 让「单轮超时」在请求层就先兜住，而不是靠线程半途放弃。
REQUEST_TIMEOUT = 30

# 公开字段白名单：卡片、事件、日志只允许出现这些字段。
# signed（我报没报）属于个人执行状态，公共链路一律不带。
PUBLIC_FIELDS = ("id", "name", "type", "place", "sign_start", "sign_end",
                 "act_start", "act_end", "checkin")

STATUS_PENDING = "pending"
STATUS_OPEN = "open"
STATUS_CLOSED = "closed"
STATUS_UNKNOWN = "unknown"
STATUSES = (STATUS_PENDING, STATUS_OPEN, STATUS_CLOSED, STATUS_UNKNOWN)


def call_api(token: str, page_num: int, page_size: int = PAGE_SIZE,
             timeout: int = REQUEST_TIMEOUT) -> dict:
    payload = {"pageSize": page_size, "pageNum": page_num,
               "hdmc": "", "hdzt": None, "jssj": "", "kssj": ""}
    req = urllib.request.Request(
        ACTIVITY_API,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "token": token,
            "User-Agent": USER_AGENT,
            "Referer": REFERER,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def parse_dt(s: str):
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(s).strip(), fmt)
        except ValueError:
            continue
    return None


def _checkin_text(raw: dict) -> str:
    """签到时间（可空）。

    列表接口只在部分场次返回签到字段（qdkssj/qdjssj，字段名与详情接口一致），
    实测大多数活动没有。取不到就返回空串，卡片直接不显示这一行——
    绝不为了「看起来完整」去猜一个默认签到时间。
    """
    start = str(raw.get("qdkssj") or "").strip()
    end = str(raw.get("qdjssj") or "").strip()
    if not start and not end:
        return ""
    if start and end:
        return "签到 %s–%s" % (start[11:16] or start, end[11:16] or end)
    return "签到 %s 起" % (start[11:16] if start else end[11:16])


def normalize(raw: dict) -> dict:
    return {
        "id": raw.get("id", ""),
        "name": (raw.get("hdmc") or "").strip(),
        "type": raw.get("hdlx") or "其他",
        "place": raw.get("hddd") or "",
        "sign_start": raw.get("bmkssj") or "",
        "sign_end": raw.get("bmjssj") or "",
        "act_start": raw.get("hdkssj") or "",
        "act_end": raw.get("hdjssj") or "",
        "checkin": _checkin_text(raw),
        "signed": raw.get("bmzt") == 1,
    }


def public_item(item: dict) -> dict:
    """公开字段视图：去掉 signed 这类「我报没报」的个人字段。

    群卡片、增量事件、日志都只允许拿这份数据。少一个字段就少一次
    「把个人报名状态发到公共群」的机会，所以这里用白名单而不是黑名单。
    """
    out = {}
    for key in PUBLIC_FIELDS:
        value = item.get(key)
        out[key] = "" if value is None else value
    return out


def item_status(item: dict, now: datetime) -> str:
    """按业务北京时间把一场活动分成四种公开状态。

    - closed：报名已截止，或活动已经开始（再报名没有意义）
    - unknown：学校没给报名开放时间，无法判断能不能报
    - pending：还没到报名开放时间
    - open：已开放、且报名还没截止

    边界条件先判 closed 再判 pending：一条活动如果 sign_start 在未来、
    但 sign_end 已经过去（学校改期没改全），按「已经结束」处理，
    不能让它永远挂在「待开放」里。
    """
    sign_end = parse_dt(item.get("sign_end"))
    if sign_end and now > sign_end:
        return STATUS_CLOSED
    act_start = parse_dt(item.get("act_start"))
    if act_start and act_start <= now:
        return STATUS_CLOSED
    sign_start = parse_dt(item.get("sign_start"))
    if not sign_start:
        return STATUS_UNKNOWN
    if sign_start > now:
        return STATUS_PENDING
    return STATUS_OPEN


def classify_public(items: list, now: datetime) -> list:
    """给每条活动补上公开状态，不做性别、黑名单、个人报名过滤。

    动态监测要的是「学校最近新发布了什么」，不是「推给谁」；
    过滤是推送侧（classify）的事，这里一旦过滤就会漏掉别人还没报的活动。
    """
    out = []
    for it in items:
        row = public_item(it)
        row["status"] = item_status(it, now)
        out.append(row)
    return out


def gender_mismatch(name: str, gender: str) -> bool:
    """名字里带性别标记时，过滤掉不是本人的场次。"""
    if not gender:
        return False
    if gender == "男" and "女生" in name:
        return True
    if gender == "女" and "男生" in name:
        return True
    return False


# 4001 不必然是登录态真失效。整点 cron 齐发时学校接口会偶发一次
# 「身份验证失败」：2026-10-30 07:00:01 token_probe 用同一 token 打同一接口
# 返回 ok，07:00:02 这边就 4001。隔一会儿重试该页即可；直接把单次 4001
# 判成失效，会连带 auto_login 立刻重登，反而把还在用的 token 真换掉。
LOGIN_RETRY_DELAY_S = 1.5
LOGIN_RETRY_ATTEMPTS = 2


def call_page(token: str, page_num: int, timeout: int = REQUEST_TIMEOUT) -> dict:
    """取一页；遇到 4001 先重试，仍失败才把判断交给调用方。

    只对 4001 重试。其它错误码（分页为空、接口异常）照旧立即上抛：
    那些是真实的数据问题，重试只会把错误推迟一轮。
    """
    last = {}
    for attempt in range(LOGIN_RETRY_ATTEMPTS):
        last = call_api(token, page_num, timeout=timeout)
        if last.get("code") != 4001:
            return last
        if attempt + 1 < LOGIN_RETRY_ATTEMPTS:
            time.sleep(LOGIN_RETRY_DELAY_S)
    return last


def fetch_all(token: str, timeout: int = REQUEST_TIMEOUT) -> list:
    first = call_page(token, 1, timeout=timeout)
    if first.get("code") == 4001:
        raise PermissionError(first.get("message", "身份验证失败"))
    if first.get("code") != 1:
        raise RuntimeError(f"接口返回异常：{first.get('code')} {first.get('message')}")

    data = first.get("data") or {}
    total = int(data.get("count") or 0)
    items = list(data.get("list") or [])
    page = 1
    while len(items) < total and page < 30:
        page += 1
        more = call_page(token, page, timeout=timeout)
        code = more.get("code")
        if code == 4001:
            raise PermissionError(more.get("message", "身份验证失败"))
        # 分页失败不能静默 break：那样只会拿到前几页，
        # 后面的活动从清单里消失，而系统看起来一切正常。
        if code != 1:
            raise RuntimeError(f"第 {page} 页抓取失败：{code} {more.get('message')}")
        chunk = (more.get("data") or {}).get("list") or []
        if not chunk:
            raise RuntimeError(f"第 {page} 页返回空，已取 {len(items)}/{total} 条")
        items.extend(chunk)

    if total and len(items) < total:
        raise RuntimeError(f"分页不完整：只取到 {len(items)}/{total} 条，"
                           f"已到第 {page} 页上限")
    return [normalize(i) for i in items if i.get("hdmc")]


def classify(items: list, gender: str, blocklist: list, now: datetime):
    """把活动分成「待开放报名」和「报名已开放」两组。

    两组都只保留活动本身还没开始、报名还没截止的条目。
    已开放报名那组是给「看到就能自己去报」用的——名额可能已满，
    但活动详情页仍然会如实告诉你还能不能报。

    状态判定统一走 item_status()，避免公共事件和日报各写一套时间边界：
    两套边界一旦漂移，群里就会出现「事件说已结束、日报说待开放」。
    """
    pending, open_now = [], []
    for it in items:
        # 注意：这里不按「我是否已报名」过滤。
        # 推送目标是公用宿舍群，别人还没报，不能因为我报了就不显示。
        if gender_mismatch(it["name"], gender):
            continue
        if any(w in it["name"] for w in blocklist):
            continue

        status = item_status(it, now)
        if status == STATUS_PENDING:
            pending.append(it)
        elif status == STATUS_OPEN:
            open_now.append(it)

    pending.sort(key=lambda x: x["sign_start"] or "")
    open_now.sort(key=lambda x: x["sign_end"] or "")
    return pending, open_now


def mine_items(items: list, now: datetime):
    """我报名了的活动，只留活动还没开始的（已结束的没意义）。"""
    mine = []
    for it in items:
        if not it["signed"]:
            continue
        act_start = parse_dt(it["act_start"])
        if act_start and act_start < now:
            continue
        mine.append(it)
    mine.sort(key=lambda x: x["act_start"] or x["sign_start"] or "")
    return mine


def main() -> int:
    cfg = load_config()
    session = load_session()
    token = (session.get("token") or "").strip()
    if not token:
        print("[x] 没有登录态，请先运行：python scripts/login.py")
        return 2

    try:
        items = fetch_all(token)
    except PermissionError as exc:
        print(f"[x] 登录态已失效（{exc}），请重新运行：python scripts/login.py")
        return 2
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"[x] 网络异常：{exc}")
        return 3
    except Exception as exc:
        print(f"[x] 抓取失败：{type(exc).__name__}: {exc}")
        return 3

    if not items:
        print("[x] 接口返回 0 条活动，可能页面结构或有权限变化。")
        return 3

    now = business_now()
    fcfg = cfg["filter"]
    gender = session.get("gender", "")
    blocklist = [w for w in fcfg.get("keyword_blocklist", []) if w]

    # 只保留「还没开放报名」的活动。
    # 已经开始报名的不进「待开放」清单——那份清单是预告，不是可报名列表；
    # 已开放的活动由动态监测和固定日报的「报名已开放」部分单独展示，
    # 开放前 10 分钟另有一条只提醒时间的临近提醒（scan.py）。
    pending, open_now = classify(items, gender, blocklist, now)

    # 「新增」= 从未成功推送过。这里只读判断，真正的标记在 run.py 发送成功后写入，
    # 否则推送失败也会被记成"已见"，下次就不标新增了（漏推）。
    try:
        raw_state = load_state()
    except RuntimeError as exc:
        print(f"[x] {exc}")
        return 4
    # 不回落到旧版 seen：那是「抓取过」不是「推送成功过」，混用会吞掉新增标记。
    announced = set(raw_state.get("announced") or {})
    for it in pending:
        it["is_new"] = (it["id"] not in announced) and (it["name"] not in announced)

    def _touch(st):
        st["last_run"] = now.strftime("%Y-%m-%d %H:%M:%S")
        return st

    update_state(_touch)

    new_count = sum(1 for i in pending if i["is_new"])

    # 日报
    md = [f"# 智慧元小元 · 待开放报名清单",
          "",
          f"抓取时间：{now:%Y-%m-%d %H:%M}　"
          f"活动库 {len(items)} 条　待开放 {len(pending)} 条　新增 {new_count} 条",
          "",
          "> 只列还没开放报名的活动。已开放报名的按「报名已开放」展示，"
          "能否报名与名额以活动详情页为准；开放前 10 分钟会单独提醒。",
          ""]
    if not pending:
        md.append("暂时没有待开放报名的活动。")
    for idx, it in enumerate(pending, 1):
        tag = "🆕 " if it["is_new"] else ""
        md.append(f"### {idx}. {tag}【{it['type']}】{it['name']}")
        md.append(f"- 报名开放：{it['sign_start']}　截止：{it['sign_end'] or '—'}")
        md.append(f"- 活动时间：{it['act_start'] or '—'}　地点：{it['place'] or '—'}")
        md.append("")

    text = "\n".join(md)
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "latest.md").write_text(text, encoding="utf-8")
    (REPORTS / f"{now:%Y-%m-%d}.md").write_text(text, encoding="utf-8")
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "latest.json").write_text(
        json.dumps({"pending": pending}, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = {
        "total_in_pool": len(items),
        "pending_count": len(pending),
        "open_count": len(open_now),
        "new_count": new_count,
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "pending": [
            {"id": i["id"], "name": i["name"], "type": i["type"],
             "sign_start": i["sign_start"], "sign_end": i["sign_end"],
             "act_start": i["act_start"], "place": i["place"], "is_new": i["is_new"],
             # signed 必须跟着 summary 走：预报名卡片靠它过滤掉「我已经报过名」的场次。
             # 漏掉这个字段时过滤条件恒为真，已报名的活动照样挂着「预报名」按钮。
             "signed": bool(i.get("signed"))}
            for i in pending
        ],
        "open": [
            {"name": i["name"], "type": i["type"],
             "sign_end": i["sign_end"], "place": i["place"]}
            for i in open_now[:20]
        ],
    }
    print("SUMMARY_JSON_BEGIN")
    print(json.dumps(summary, ensure_ascii=False))
    print("SUMMARY_JSON_END")
    return 0


if __name__ == "__main__":
    sys.exit(main())
