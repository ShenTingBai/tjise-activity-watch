"""公共配置、会话与状态读写。"""
import contextlib
import json
import os
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
REPORTS = ROOT / "reports"
RAW = DATA / "raw"
PROFILE = DATA / "chrome-profile"
STATE_FILE = DATA / "state.json"
SESSION_FILE = DATA / "session.json"
CONFIG_FILE = ROOT / "config.json"
LOCK_FILE = DATA / ".state.lock"

# 学校站点 origin（含 scheme）。换学校时只改这一处，或在运行环境里设 SCHOOL_ORIGIN。
# 默认值就是当前学校，所以不配置时行为和以前完全一致。
# 端点路径（/dr/api、/stu/）是站点自己的路由，不属于换学校的变量，保持不动。
SCHOOL_ORIGIN = (os.environ.get("SCHOOL_ORIGIN") or "https://xg.tjise.edu.cn").rstrip("/")

API_BASE = f"{SCHOOL_ORIGIN}/dr/api"
ACTIVITY_API = f"{API_BASE}/rjyHd/listActivity"
HOME_URL = f"{SCHOOL_ORIGIN}/stu/"
REFERER = HOME_URL

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")

# 清单最多列几条。卡片（card.py）和纯文本兜底（render.py）必须共用同一个值，
# 否则同一批活动在两条通道里条数不同，用户会以为漏推了。
MAX_LIST_ITEMS = 8

DEFAULT_CONFIG = {
    "filter": {
        # 只保留这一个开关：其余过滤都是面向公用群的固定规则，不做成配置
        "keyword_blocklist": [],
    },
    "notify": {
        "channel": "",
        "platform": "feishu",
        "webhook_url": "",
        "secret": "",
        "qq": {
            "api_base": "",
            "token": "",
            "group_id": "",
        },
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    user_cfg = {}
    if CONFIG_FILE.exists():
        try:
            user_cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            user_cfg = {}
    return _deep_merge(DEFAULT_CONFIG, user_cfg)


def _read_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def load_session() -> dict:
    return _read_json(SESSION_FILE, {})


def save_session(session: dict) -> None:
    """原子写。续期脚本每 2 小时覆盖一次，抓取脚本随时可能在读，
    直接 write_text 会让对方读到半截 JSON。"""
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = SESSION_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(session, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(str(tmp), str(SESSION_FILE))


def load_state() -> dict:
    """state.json 损坏时抛异常，而不是静默返回空 dict。

    静默返回空会把所有在推的活动重新当成「新增」，
    下一次 save 又把这份空状态写回去 —— 历史被覆盖，重复推送随之而来。
    宁可本轮失败并留下痕迹，也不要假装什么都没发生。
    """
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(
                f"state.json 解析失败（{exc}）。原文件未改动，请先修复再运行。"
            ) from exc
    return {}


def save_state(state: dict) -> None:
    """先写临时文件再原子替换，避免写一半被读到、留下损坏的 JSON。"""
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(STATE_FILE))


@contextlib.contextmanager
def state_lock():
    """跨进程互斥，防止 21:30 的清单和扫描同时读改写 state.json 互相覆盖。

    Windows 上没有 fcntl，降级为不加锁（本地只在手动调试时跑，风险可接受）。
    """
    DATA.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:
        yield
        return

    with open(str(LOCK_FILE), "w") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def update_state(mutator):
    """加锁读改写：mutator(state) 返回新的 state。"""
    with state_lock():
        state = load_state()
        result = mutator(state)
        save_state(state if result is None else result)
        return state


def fmt_when(sign_start: str, now: datetime = None) -> str:
    """统一的时间写法：2026-10-20 12:00:00 -> 「10-20 12:00」。

    用绝对日期而不是「今天/明天」——群里翻记录时相对说法会失真，
    而且同一条消息里出现多个「明天」很容易看错。
    跨年时才补上年份。
    """
    if not sign_start:
        return ""
    ts = str(sign_start).strip()
    if len(ts) < 16:
        return ts[:16]
    year, month, day, hhmm = ts[0:4], ts[5:7], ts[8:10], ts[11:16]
    if now is not None and year != str(now.year):
        return "%s-%s-%s %s" % (year, month, day, hhmm)
    return "%s-%s %s" % (month, day, hhmm)
