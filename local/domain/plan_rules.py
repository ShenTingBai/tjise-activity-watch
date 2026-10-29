# -*- coding: utf-8 -*-
"""计划纯策略：计划项归一化/门禁、列表合并、任务终态分类。

纯函数：缓存与现值由调用方传入，不取时钟、不读文件、不起进程。
模式分类（classify_mode）与冲突校验（validate_fn）、学校时间换算（epoch_of）
都由 composition root（local/app.py）注入——domain 不得直接依赖 signup_plan
等混合具体模块。错误载荷形状与用户文案是冻结契约，原样保留。

@author LiXinwen
"""
from __future__ import annotations

from domain.retry_rules import retry_payload, with_attempts
from domain.time_text import task_deadline

# 报名成功后学校列表可能短暂滞后：本地已确认成功在这段窗口内不被撤销
SIGNED_SYNC_WINDOW_S = 120.0


def normalize_plan_items(items, activities: dict, accepted_at: float):
    """前端只提交「选了哪一场」：资格与时间一律以进程内缓存为准。

    返回 (归一化列表, None) 或 (None, 错误载荷)。陈旧前端快照绝不能覆盖
    更新的 signed/截止时间（R05）；任何一项不合格都整单拒绝，不静默跳过。
    """
    if not isinstance(items, list) or not items:
        return None, {"ok": False, "text": "先至少选择一场活动"}
    normalized = []
    seen_ids: set = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            return None, {"ok": False,
                          "text": "计划里有格式不对的活动项，请刷新列表后重新选择。",
                          "errors": [{"kind": "bad_item", "index": index}]}
        aid = str(item.get("id") or "").strip()
        if not aid:
            return None, {"ok": False,
                          "text": "计划里有缺少 id 的活动，请刷新列表后重新选择。",
                          "errors": [{"kind": "missing_id", "index": index}]}
        if aid in seen_ids:
            return None, {"ok": False, "text": "同一活动被选了两遍，去掉重复的那一条。",
                          "errors": [{"kind": "duplicate_id", "activity_id": aid}]}
        seen_ids.add(aid)
        cached = activities.get(aid)
        if not cached:
            # 列表还没加载过/被清空：没有权威快照就没有放行依据
            return None, {"ok": False,
                          "text": "列表里找不到这一场活动，请点「刷新」重新加载后再选。",
                          "errors": [{"kind": "unknown_activity", "activity_id": aid}]}
        changed = [key for key in ("sign_start", "sign_end", "act_start", "act_end")
                   if key in item and str(item.get(key) or "") != str(cached.get(key) or "")]
        if "signed" in item and bool(item.get("signed")) != bool(cached.get("signed")):
            changed.append("signed")
        if changed:
            # 不静默换成另一个时间开始：让用户刷新后重新确认
            return None, {"ok": False,
                          "text": "活动信息已变化，请刷新后重新确认。",
                          "errors": [{"kind": "activity_changed", "activity_id": aid,
                                      "name": cached.get("name"), "fields": changed}]}
        normalized.append(dict(cached))
    return normalized, None


def closed_items_error(normalized, accepted_at: float, classify_mode):
    """报名截止是硬门禁（前端复选/校验之外的后端最终防线）。

    classify_mode：模式分类纯函数（app.py 注入 signup_plan.classify_signup_mode）。
    已截止活动不能进入 worker；pre_register 与 instant_signup 是两个正式
    模式，混合计划允许共存，逐场按各自模式运行。全部合格返回 None。
    """
    closed = [item for item in normalized
              if classify_mode(str(item.get("sign_start") or ""),
                               str(item.get("sign_end") or ""),
                               accepted_at) == "closed"]
    if not closed:
        return None
    names = "、".join("「%s」" % (item.get("name") or "活动") for item in closed[:3])
    more = " 等 %d 场" % len(closed) if len(closed) > 3 else ""
    return {"ok": False,
            "text": "报名已截止：%s%s（报名截止 %s）不能加入计划，只能在列表里查看。"
                    % (names, more, closed[0].get("sign_end") or "时间未公布"),
            "errors": [{"kind": "sign_end_passed", "activity_id": item.get("id"),
                        "name": item.get("name"), "sign_end": item.get("sign_end")}
                       for item in closed]}


def check_conflicts(normalized, accepted_at: float, signed_baseline: list,
                    validate_fn) -> dict:
    """已报名活动占用的举办时段也是冲突基线（act_start/act_end 比较）。

    validate_fn：冲突校验纯函数（app.py 注入 signup_plan.validate_plan）。
    """
    checked = validate_fn(normalized, now=accepted_at,
                          signed_activities=signed_baseline)
    if checked["ok"]:
        return {"ok": True}
    return {"ok": False, "text": "计划里有不能同时报名的活动",
            "conflicts": checked["conflicts"], "errors": checked["errors"]}


def merge_list_activities(items, prev: dict, now: "float | None",
                          window_s: float = SIGNED_SYNC_WINDOW_S) -> list[dict]:
    """列表刷新结果与本地已确认成功合并，返回可下发界面的新列表。

    学校说已报名 → 权威；本地已确认成功但学校还没同步 → 窗口内保留
    signed=true 并标 sync_pending，让界面继续把这场留在「已报名」而不可再选。
    """
    merged: list[dict] = []
    for raw in items or []:
        item = dict(raw)
        aid = str(item.get("id") or "")
        prev_item = (prev or {}).get(aid) or {}
        if bool(item.get("signed")):
            item["signed_source"] = "school"
            item["sync_pending"] = False
            merged.append(item)
            continue
        source = str(prev_item.get("signed_source") or "")
        confirmed_at = float(prev_item.get("signed_confirmed_at") or 0)
        local_confirmed = bool(prev_item.get("signed")) and source in {
            "plan_success", "plan_already_signed"}
        within_window = (now is None or not confirmed_at
                         or now - confirmed_at <= window_s)
        if local_confirmed and within_window:
            item["signed"] = True
            item["signed_source"] = source
            item["signed_confirmed_at"] = confirmed_at
            item["sync_pending"] = True
        merged.append(item)
    return merged


def task_stop_reason_binding(spec, supervised_fired: bool, epoch_of) -> str:
    """父层收走且无终态行时的停止原因：只看「截止还是窗口」绑住这一场。

    epoch_of：学校时间文本 → epoch 秒（由 composition root 注入）。
    """
    if not supervised_fired:
        return ""
    return ("sign_end" if spec.sign_end and task_deadline(spec, epoch_of) == epoch_of(spec.sign_end)
            else "retry_duration")


def classify_task_stop(spec, code, tail: list, canceled: bool,
                       supervised_fired: bool, stop: dict, attempts_seen: int,
                       epoch_of) -> dict:
    """任务终态分类（纯函数）：已获得的证据优先于本地怎么结束的（FR03）。

    停止只终止本地执行：撤不回已发出的请求，也推翻不了已收到的学校答复。
    先认已确认结果，再认确定失败，最后才落到取消/待核对。
    """
    if stop.get("stop_reason") == "already_signed" \
            or any("你已经报过这个活动" in line for line in tail):
        return {"status": "already_signed", "code": code, "reason": "你已经报过这一场",
                **retry_payload(with_attempts(stop, attempts_seen), spec,
                                stop_reason="already_signed")}

    # 成功不要求退出码为 0：强杀会给出非 0，但杀之前的成功终态已经收到
    if stop.get("stop_reason") == "success" \
            or any("[OK] 报名成功" in line for line in tail):
        return {"status": "success", "code": code, "reason": "报名成功",
                **retry_payload(with_attempts(stop, attempts_seen), spec,
                                stop_reason="success")}
    if canceled:
        definite = str(stop.get("stop_reason") or "")
        if definite:
            # 取消晚到：worker 已经给出确定终态，按那个结论记，不降级成取消
            return {"status": "failed", "code": code,
                    "reason": "已按你的要求停止；停止前已收到确定结果",
                    "locally_stopped": True,
                    **retry_payload(with_attempts(stop, attempts_seen), spec,
                                    stop_reason=definite)}
        if attempts_seen > 0:
            # 本地确实停了，但已发出的提交没有确定回应：两层事实都要保留，
            # 次数不能归零，也不能说成「没抢到」（FR03）。
            return {"status": "failed", "code": code,
                    "reason": "已按你的要求停止；最后一次提交没有拿到确定回应",
                    "locally_stopped": True,
                    **retry_payload(dict(stop, attempts=attempts_seen), spec,
                                    stop_reason="unconfirmed")}
        return {"status": "canceled", "code": code, "reason": "用户取消了这一场",
                **retry_payload(stop, spec, stop_reason="user_stopped")}
    reason = ""
    for line in reversed(tail):
        if line.startswith("[X] "):
            reason = line[4:].strip()
            break
    if supervised_fired and not stop.get("stop_reason"):
        # 父层收走时没有终态行：保留已观察到的提交次数；
        # 发过请求但没结论的，必须说「未确认」，不能冒充学校明确失败
        stop = {"attempts": attempts_seen,
                "stop_reason": "unconfirmed" if attempts_seen else ""}
        if not attempts_seen:
            stop_reason = task_stop_reason_binding(spec, True, epoch_of)
        else:
            stop_reason = "unconfirmed"
        return {"status": "failed", "code": code,
                "reason": "本机监督到期，已收走本场进程",
                **retry_payload(stop, spec, stop_reason=stop_reason)}
    stop_reason = stop.get("stop_reason") or (
        "login_invalid" if code == 3 and "登录失效" in "".join(tail) else "process_error")
    return {"status": "failed", "code": code,
            "reason": reason or "退出码 %s" % code,
            **retry_payload(stop, spec, stop_reason=stop_reason)}
