"""公共配置、会话与状态读写。"""
import contextlib
import json
import os
import tempfile
import threading
import time
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

# 进程内互斥：Windows 的 msvcrt 文件锁只到进程级，挡不住同进程多线程；
# 这把锁 + 每路径重入深度让 file_lock 在两端都真正串行（见 file_lock）。
_PROCESS_LOCK = threading.RLock()
_HELD: dict[str, int] = {}

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


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    """同目录临时文件 + fsync + 原子替换，权限固定由调用方指定。

    续期脚本/抓取脚本/人工 CLI 会并发读写同一份文件：先写临时文件再 replace，
    读方永远看不到半截 JSON；fsync 保证进程被 kill 后不会留下 0 字节文件。
    Windows 没有目录 fsync，跳过；临时文件权限在 replace 前设置，避免短暂 0644。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp",
                                    dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(str(tmp), mode)
        except OSError:
            pass          # 某些文件系统不支持 POSIX 权限，不阻断写入
        os.replace(str(tmp), str(path))
        if os.name != "nt":
            try:
                dir_fd = os.open(str(path.parent), os.O_RDONLY)
            except OSError:
                dir_fd = None
            if dir_fd is not None:
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


@contextlib.contextmanager
def file_lock(path=None):
    """跨进程互斥（POSIX flock / Windows msvcrt），锁内只做本地读改写。

    原来只有 state_lock 用 fcntl，Windows 静默降级为不加锁；watchlist 的
    add/remove/mark 与 runner 并发时会互相覆盖（P2-3）。这里给出一个
    两端都真正加锁的可复用实现，锁文件由调用方指定。

    Windows 的 msvcrt 锁是**进程级**的：同进程两个线程各开一个句柄不会互相
    阻塞。因此在 OS 锁外层再加一把进程内 RLock，并按路径记重入深度——同一
    线程嵌套取同一把锁不再重复申请（POSIX 上会自我阻塞）。
    """
    lock_path = Path(path or LOCK_FILE)
    key = str(lock_path.resolve())
    with _PROCESS_LOCK:
        depth = _HELD.get(key, 0)
        _HELD[key] = depth + 1
        handle = None
        locked = False
        try:
            if depth == 0:
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                handle = open(str(lock_path), "a+b")
                if os.name == "nt":
                    import msvcrt
                    while not locked:
                        try:
                            handle.seek(0)
                            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                            locked = True
                        except OSError:
                            # LK_LOCK 内部重试 10 次后抛错：继续等，直到拿到锁
                            time.sleep(0.05)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    locked = True
            yield
        finally:
            _HELD[key] = max(0, _HELD.get(key, 1) - 1)
            if handle is not None:
                try:
                    if locked:
                        if os.name == "nt":
                            import msvcrt
                            handle.seek(0)
                            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                finally:
                    handle.close()


def save_session(session: dict) -> None:
    """原子写（0600）。续期脚本每 2 小时覆盖一次，抓取脚本随时可能在读，
    直接 write_text 会让对方读到半截 JSON。"""
    atomic_write_text(SESSION_FILE,
                      json.dumps(session, ensure_ascii=False, indent=2),
                      mode=0o600)


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
    atomic_write_text(STATE_FILE,
                      json.dumps(state, ensure_ascii=False, indent=2),
                      mode=0o600)


@contextlib.contextmanager
def state_lock():
    """跨进程互斥，防止 21:30 的清单和扫描同时读改写 state.json 互相覆盖。"""
    with file_lock(LOCK_FILE):
        yield


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
