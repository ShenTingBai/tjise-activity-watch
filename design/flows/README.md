# 用户流程（flows/）

> 这里描述用户**现在就能走到**的流程：从首次打开到拿到报名结果。
> 每个流程同时标注四件事：**用户动作、系统状态、可见反馈、失败后的下一步**。
> 业务规则本身（为什么这样定）不在这里重复——回 [../decisions/](../decisions/) 和 `docs/` 查阅。

## 目录

| 流程 | 覆盖 |
|---|---|
| [first-use-and-login.md](first-use-and-login.md) | 首次打开、登录态四态、登录窗口、重开复用 |
| [activity-selection-and-plan.md](activity-selection-and-plan.md) | 列表、筛选、模式标签、勾选、冲突检查、开始计划 |
| [mixed-plan-runtime.md](mixed-plan-runtime.md) | 混合计划运行、五站时间线、telemetry、重载恢复 |
| [stop-and-result.md](stop-and-result.md) | 停止、单场取消、各终态结论 |

## 写作约定

1. 流程按用户真实顺序编号；每个分支都要写失败后的下一步，不用笼统的「失败」。
2. 界面文案引用现行实现的说法；改文案时同步更新对应流程页。
3. 流程页不放字段表和接口定义（那是 `docs/UI_SPEC.md` 的事），只放「用户看到什么、能做什么」。
