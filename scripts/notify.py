"""把活动简报推送到群 / 个人。

支持四种通道，只需在 config.json 里选一种并填地址：

1. 飞书群机器人（最省事，1 分钟建好）
     channel = "webhook", platform = "feishu"
     webhook_url = https://open.feishu.cn/open-apis/bot/v2/hook/xxxx
     可选 secret（开了「签名校验」才需要）

2. 钉钉群机器人
     platform = "dingtalk"
     webhook_url = https://oapi.dingtalk.com/robot/send?access_token=xxxx
     可选 secret（开了「加签」才需要）

3. 企业微信群机器人
     platform = "wecom"
     webhook_url = https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx

4. QQ（OneBot v11：NapCat / Lagrange / go-cqhttp）
     channel = "qq"
     qq = {api_base, token, group_id}

没配置时返回 None（静默跳过），配置了但发送失败返回 False。
"""

import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from common import DATA, load_config


def _post(url: str, payload: dict, token: str = "", timeout: int = 20,
          method: str = "POST"):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300, resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}: {exc.read().decode('utf-8', 'ignore')[:300]}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _is_zero_code(value) -> bool:
    """业务码「成功」只认整数 0 或字符串 "0"。

    bool 是 int 的子类（False == 0），小数和空值也不该被当成成功：
    这些形状说明对方返回的不是我们预期的业务码，宁可判失败再重试一次。
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value == 0
    if isinstance(value, str):
        return value.strip() == "0"
    return False


def _business_ok(body: str, platform: str = "feishu") -> bool:
    """HTTP 2xx 不等于业务成功，且「看不懂的响应」不能再默认算成功。

    飞书 / 钉钉 / 企微的自定义机器人都会在 HTTP 200 时返回业务错误码
    （限流、签名错、机器人被禁用、载荷非法）。只看 HTTP 状态会把「没发出去」
    判成「已推送」，去重集合一旦写入就再也不会重推 —— 用户永远收不到。

    各平台的成功判据：
    - feishu：code 或 StatusCode 必须是 0；两者同时出现时都必须为 0；
    - dingtalk / wecom：errcode 必须是 0；
    - qq：retcode 必须是 0，带 status 时还必须是 ok/success；
    - generic：保留旧的兼容语义（有明确错误码才判失败），因为通用 webhook
      的响应格式无法枚举，这里单独记为「弱确认」。
    """
    name = (platform or "").strip().lower()
    known = ("feishu", "dingtalk", "wecom", "qq")
    kind = name if name in known else "generic"

    try:
        data = json.loads(body)
    except Exception:
        # 非 JSON：只有 generic 通道允许「看不懂就当成功」
        return kind == "generic"
    if not isinstance(data, dict):
        return kind == "generic"

    if kind == "feishu":
        fields = [key for key in ("code", "StatusCode", "status_code") if key in data]
        if not fields:
            return False
        return all(_is_zero_code(data[key]) for key in fields)

    if kind in ("dingtalk", "wecom"):
        if "errcode" not in data:
            return False
        return _is_zero_code(data["errcode"])

    if kind == "qq":
        if "retcode" not in data or not _is_zero_code(data["retcode"]):
            return False
        status = data.get("status")
        if status is None:
            return True
        if not isinstance(status, str):
            return False
        return status.strip().lower() in ("ok", "success")

    # generic：有明确错误码时仍然判失败
    for key in ("code", "errcode", "retcode", "StatusCode", "status_code"):
        if key in data:
            try:
                return int(data[key]) == 0
            except (TypeError, ValueError):
                return False
    if isinstance(data.get("status"), str):
        return data["status"].strip().lower() in ("ok", "success")
    return True


# 飞书整点压力大会直接拒卡片：11232 是官方文档里「因系统压力导致的限流」，
# 9499 / 99991400 是同类的频控码。这类错误等几秒重发基本都能过，
# 一旦落到纯文本兜底，用户看到的就是「样式不对」的裸文（已发生过一次）。
RETRY_CODES = (11232, 9499, 99991400)
CARD_RETRIES = 4
CARD_BACKOFF = 1.5


def _body_code(body: str):
    """取响应里的业务码，取不到返回 None。"""
    try:
        data = json.loads(body)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    for key in ("code", "StatusCode", "status_code", "errcode", "retcode"):
        if key in data:
            try:
                return int(data[key])
            except (TypeError, ValueError):
                return None
    return None


def _post_retry(url: str, payload: dict, token: str = "",
                attempts: int = CARD_RETRIES, label: str = "卡片"):
    """只在限流码上退避重发；内容错、鉴权错这类重发也没用，立即返回。"""
    ok, body = False, ""
    for i in range(attempts):
        ok, body = _post(url, payload, token=token)
        if ok and _business_ok(body, "feishu"):
            if i:
                print("[i] 第 %d 次重试后%s推送成功" % (i + 1, label))
            return ok, body
        if _body_code(body) not in RETRY_CODES or i == attempts - 1:
            break
        wait = CARD_BACKOFF * (2 ** i)
        print("[i] %s被限流（code=%s），%.1fs 后重试" % (label, _body_code(body), wait))
        time.sleep(wait)
    return ok, body


def _reason(body: str, platform: str) -> str:
    """把响应压成一行原因，只带业务码字段，不回显整段响应。

    响应里可能回显请求内容，日志打到 cron 文件后难以清理；
    这里只保留判定需要的字段名和值。
    """
    try:
        data = json.loads(body)
    except Exception:
        return "响应不是 JSON"
    if not isinstance(data, dict):
        return "响应不是对象"
    parts = []
    for key in ("code", "StatusCode", "status_code", "errcode", "retcode", "status"):
        if key in data:
            parts.append("%s=%s" % (key, data[key]))
    if not parts:
        return "响应里没有可识别的业务码"
    return " ".join(parts)[:120]


# ---------- 各平台签名 ----------
def _feishu_sign(secret: str, timestamp: int) -> str:
    key = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(key, b"", digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _dingtalk_sign(secret: str, timestamp: int) -> str:
    key = secret.encode("utf-8")
    msg = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(key, msg, digestmod=hashlib.sha256).digest()
    return urllib.parse.quote_plus(base64.b64encode(digest))


# ---------- 各平台载荷 ----------
def _build_feishu(text: str, secret: str, url: str) -> tuple:
    payload = {"msg_type": "text", "content": {"text": text}}
    if secret:
        ts = int(time.time())
        payload["timestamp"] = str(ts)
        payload["sign"] = _feishu_sign(secret, ts)
    return url, payload


def _build_dingtalk(text: str, secret: str, url: str) -> tuple:
    if secret:
        ts = int(time.time() * 1000)
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}timestamp={ts}&sign={_dingtalk_sign(secret, ts)}"
    return url, {"msgtype": "text", "text": {"content": text}}


def _build_wecom(text: str, secret: str, url: str) -> tuple:
    return url, {"msgtype": "text", "text": {"content": text}}


def _build_generic(text: str, secret: str, url: str) -> tuple:
    return url, {"msgtype": "text", "text": {"content": text}, "content": text}


BUILDERS = {
    "feishu": _build_feishu,
    "dingtalk": _build_dingtalk,
    "wecom": _build_wecom,
    "generic": _build_generic,
}


def send(text: str):
    cfg = load_config()["notify"]
    channel = (cfg.get("channel") or "").strip()

    if channel == "qq":
        return _send_qq(text, cfg.get("qq") or {})

    url = (cfg.get("webhook_url") or "").strip()
    if not url:
        print("[i] 未配置推送通道，本次只在对话内播报")
        return None

    platform = (cfg.get("platform") or "generic").strip().lower()
    builder = BUILDERS.get(platform, _build_generic)
    final_url, payload = builder(text, (cfg.get("secret") or "").strip(), url)

    ok, body = _post(final_url, payload)
    if ok and not _business_ok(body, platform):
        ok = False
    detail = "成功" if ok else "失败：" + _reason(body, platform)
    print(f"[{'✓' if ok else 'x'}] {platform} 推送{detail}")
    return ok


def _send_qq(text: str, qq: dict):
    api_base = (qq.get("api_base") or "").rstrip("/")
    group_id = str(qq.get("group_id") or "")
    if not api_base or not group_id:
        print("[i] 未配置 QQ 机器人接口，本次只在对话内播报")
        return None

    gid = int(group_id) if group_id.isdigit() else group_id
    ok, body = _post(f"{api_base}/send_group_msg",
                     {"group_id": gid, "message": text},
                     token=qq.get("token", ""))
    if ok and not _business_ok(body, "qq"):
        ok = False
    detail = "成功" if ok else "失败：" + _reason(body, "qq")
    print(f"[{'✓' if ok else 'x'}] QQ 群推送{detail}")
    return ok


def send_card(card: dict, fallback_text: str = ""):
    """发飞书卡片；失败或非飞书通道时退回纯文本。"""
    cfg = load_config()["notify"]
    url = (cfg.get("webhook_url") or "").strip()
    platform = (cfg.get("platform") or "").strip().lower()

    if cfg.get("channel") == "webhook" and url and platform == "feishu":
        ok, body = _post_retry(url, {"msg_type": "interactive", "card": card})
        if ok and _business_ok(body, "feishu"):
            print("[✓] feishu 卡片推送成功")
            return True
        print("[!] 卡片推送失败（%s），退回纯文本" % _reason(body, "feishu"))
        if fallback_text:
            # 兜底文本成功才算整体成功；失败保持 False，让上层保留重试资格
            return send(fallback_text)
        return False

    if fallback_text:
        return send(fallback_text)
    return None


def _tenant_token(app: dict, force: bool = False) -> str:
    """带缓存地取 tenant_access_token。

    飞书这个 token 有效期 7200 秒，每次都重新申请既慢又容易触发限流，
    所以缓存到本地，提前 5 分钟刷新。
    """
    if not app.get("app_id") or not app.get("app_secret"):
        return ""

    now = time.time()
    cache_file = DATA / "token_cache.json"
    if not force:
        try:
            with open(str(cache_file), encoding="utf-8") as f:
                cached = json.load(f)
            if (cached.get("app_id") == app["app_id"]
                    and cached.get("token")
                    and float(cached.get("expire_at") or 0) - 300 > now):
                return cached["token"]
        except Exception:
            pass

    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    ok, body = _post(url, {"app_id": app.get("app_id", ""),
                           "app_secret": app.get("app_secret", "")})
    if not ok:
        return ""
    try:
        data = json.loads(body)
    except Exception:
        return ""
    token = data.get("tenant_access_token", "")
    if not token:
        return ""

    try:
        DATA.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({
            "app_id": app["app_id"],
            "token": token,
            "expire_at": now + float(data.get("expire") or 7200),
            "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(str(cache_file), 0o600)
    except Exception:
        pass
    return token


def _p2p_chat_id(cfg: dict) -> str:
    """私人提醒的收件人：只认显式配置的 owner 本人。

    以前取的是 users_file 里**第一个** p2p 记录 —— 应用可见范围内任何人只要
    先私聊机器人一次，就能把绑定账号的私人提醒接走。现在必须同时满足：

    1) 环境变量 PRIVATE_OWNER_OPEN_ID 给出真人 open_id（不是机器人的 BOT_OPEN_ID）；
    2) users_file 里按这个 key 能查到 rec.open_id 一致、chat_type == 'p2p'
       且 chat_id 非空的记录；
    3) 若另外写了 private_chat_id，必须与这条记录一致，防止配置错投。

    任一条件不满足都返回空，绝不退化成「遍历取第一个」。
    """
    owner = (os.environ.get("PRIVATE_OWNER_OPEN_ID") or "").strip()
    if not owner:
        print("[i] 未配置 PRIVATE_OWNER_OPEN_ID，私人提醒已关闭")
        return ""

    users_file = (cfg.get("users_file") or "").strip()
    if not users_file:
        print("[i] 未配置 users_file，私人提醒已关闭")
        return ""
    try:
        with open(users_file, encoding="utf-8") as f:
            db = json.load(f)
    except Exception:
        # 不打印路径细节之外的用户表内容，避免把 open_id/chat_id 写进日志
        print("[!] users_file 读取或解析失败，私人提醒已关闭")
        return ""
    if not isinstance(db, dict):
        print("[!] users_file 结构不是对象，私人提醒已关闭")
        return ""

    rec = db.get(owner)
    if not isinstance(rec, dict):
        print("[i] owner 还没有私聊过机器人，私人提醒已关闭")
        return ""
    if rec.get("open_id") != owner:
        print("[!] owner 记录的 open_id 与配置不一致，私人提醒已关闭")
        return ""
    if rec.get("chat_type") != "p2p":
        print("[i] owner 的记录不是私聊会话，私人提醒已关闭")
        return ""
    chat_id = str(rec.get("chat_id") or "").strip()
    if not chat_id:
        print("[i] owner 的私聊会话没有 chat_id，私人提醒已关闭")
        return ""

    explicit = (cfg.get("private_chat_id") or "").strip()
    if explicit and explicit != chat_id:
        print("[!] private_chat_id 与 owner 的私聊会话不一致，私人提醒已关闭")
        return ""
    return chat_id


def send_private(text: str):
    """私聊发给本人（个人提醒用），和群推送完全分开。"""
    cfg = load_config()["notify"]
    app = cfg.get("feishu_app") or {}
    if not app.get("app_id") or not app.get("app_secret"):
        print("[i] 未配置飞书应用凭据，跳过私聊")
        return None

    chat_id = _p2p_chat_id(cfg)
    if not chat_id:
        # 收件人不明确时保留 None：不发、不取 token、不构造请求
        print("[i] 没有可用的 owner 私聊会话，跳过私人提醒")
        return None

    token = _tenant_token(app)
    if not token:
        print("[x] 获取 tenant_access_token 失败")
        return False

    url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
    ok, body = _post(url, {"receive_id": chat_id, "msg_type": "text",
                           "content": json.dumps({"text": text}, ensure_ascii=False)},
                     token=token)
    if ok and _business_ok(body, "feishu"):
        print("[✓] 私聊推送成功")
        return True
    print("[x] 私聊推送失败：%s" % _reason(body, "feishu"))
    return False


def send_private_card(card: dict, fallback_text: str = ""):
    """私聊发卡片（预报名清单用）。

    群推送走 send_card（webhook 自定义机器人），这里走应用私聊，两条通道
    完全分开：群卡片里不能出现任何只给本人看的内容。
    收件人不明确时返回 None（不发、不构造请求），与 send_private 一致。

    成功时返回飞书消息 ID（字符串）：预报名卡片要靠它原地更新按钮状态，
    只回 True 的话上层就拿不到 message_id，只能每天重发一张新卡。
    """
    cfg = load_config()["notify"]
    app = cfg.get("feishu_app") or {}
    if not app.get("app_id") or not app.get("app_secret"):
        print("[i] 未配置飞书应用凭据，跳过私聊卡片")
        return None

    chat_id = _p2p_chat_id(cfg)
    if not chat_id:
        print("[i] 没有可用的 owner 私聊会话，跳过私聊卡片")
        return None

    token = _tenant_token(app)
    if not token:
        print("[x] 获取 tenant_access_token 失败")
        return False

    url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
    ok, body = _post_retry(url, {"receive_id": chat_id,
                                 "msg_type": "interactive",
                                 "content": json.dumps(card, ensure_ascii=False)},
                           token=token, attempts=2, label="私聊卡片")
    if ok and _business_ok(body, "feishu"):
        mid = ""
        try:
            mid = ((json.loads(body).get("data") or {}).get("message_id") or "")
        except Exception:
            mid = ""
        print("[✓] 私聊卡片推送成功")
        return mid or True
    print("[x] 私聊卡片推送失败：%s" % _reason(body, "feishu"))
    if fallback_text:
        return send_private(fallback_text)
    return False


def update_card(message_id: str, card: dict):
    """原地覆盖已发出的卡片（点「预报名」后把按钮换成「取消预报名」）。

    飞书没有「只改一个按钮」的接口，只能按 message_id 整卡覆盖：
    调用方先把新卡片整份构造好，这里只负责 PATCH 回去。
    卡片超过 14 天或消息已被撤回时飞书会拒绝，属正常失败，返回 False 即可。
    """
    if not message_id:
        return False
    cfg = load_config()["notify"]
    app = cfg.get("feishu_app") or {}
    if not app.get("app_id") or not app.get("app_secret"):
        return False
    token = _tenant_token(app)
    if not token:
        print("[x] 获取 tenant_access_token 失败，卡片没能更新")
        return False

    url = "https://open.feishu.cn/open-apis/im/v1/messages/%s" % message_id
    ok, body = _post(url, {"content": json.dumps(card, ensure_ascii=False)},
                     token=token, method="PATCH")
    if ok and _business_ok(body, "feishu"):
        return True
    print("[!] 卡片更新失败：%s" % _reason(body, "feishu"))
    return False


if __name__ == "__main__":
    msg = sys.stdin.read() if not sys.argv[1:] else " ".join(sys.argv[1:])
    result = send(msg)
    sys.exit(0 if result is not False else 1)
