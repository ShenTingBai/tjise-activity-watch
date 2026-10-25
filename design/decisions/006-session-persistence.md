# 006 · 登录态持久化与启动校验

- **状态**：implemented（规则 confirmed 2026-10-24，EXE 迁移 2026-10-25 收口）
- **日期**：2026-10-24 敲定统一数据根与四态校验；2026-10-25 旧 EXE 数据迁移与重建保护验收

## 问题

登录态（cookie/token）是唯一凭证，但存放位置、过期判定和「要不要重新登录」的口径曾經混乱：
重开工具被误要求重登、网络异常被当成失效、重装 EXE 把登录态弄丢——每一次都让用户多输一遍账号密码。

## 用户确认的规则

1. **统一数据根**：开发 / 绿色版在脚本旁 `local/data/`；冻结 EXE（主程序与 grab-worker）默认
   `%LOCALAPPDATA%\TianSoftActivityPreRegistration\data`，环境变量 `TJISE_DATA_DIR` 可覆盖；
   主程序与 worker 读写同一份 `session.json` / `storage_state.json`。
2. **启动真实校验、四态结论**：无 session → 「未登录」；有 session 先显示「正在检查登录态」，
   真实访问学校接口后落 **valid / expired / unknown / none** 四态之一——
   不得提前写成「登录有效」，也不得把网络异常说成失效。
3. **只有学校接口明确返回失效（4001 / 明确身份验证失败）才要求重新登录**；
   网络异常显示「暂时无法确认登录态」，保留已有会话，点「刷新」重试，不清登录、不逼重登。
4. 首次使用显示「登录」；**只有真实失效才显示「重新登录」**——两词不混用。
5. 「约 10–17 小时」是实测估算区间，只用于「约剩 N 小时」提醒，**不是保证值，也不作放行 / 拦截依据**；
   登录有效性只由学校接口真实校验决定。
6. **旧数据一次性原子迁移**：冻结版首次使用新数据根时，旧 EXE 旁 `data/` 中缺失的目标文件
   以「临时文件 + `os.replace`」搬入，不覆盖已有新文件；迁移失败不阻断启动。
7. **构建不删登录态**：`build_exe.py` 清理输出目录时保留 `OUT/data`。
8. 会话文件不含账号密码；`start_plan` 是最终防线：非 demo 且最近一次 check 结论不是 `valid` 一律拒绝。
9. （2026-10-25 补充）**应用生命周期元数据只进 `ui.json`**：`last_started_at` /
   `last_heartbeat_at` / `last_closed_at` 由主进程每分钟心跳与正常关闭写入，
   与界面偏好共用同一把持久化锁做原子读-合并-写；`session.json` / `storage_state.json`
   永远只存会话信息。重开时的「上次关闭时长」（正常关闭精确、异常退出只给「约」）与
   「登录预计还剩」都只是提示：估算按 `saved_at` 绝对墙钟跨关闭区间继续计算，
   **不得因旧估算强制过期、清理会话或刷新 `saved_at`**，登录有效性仍只由真实校验决定
   （见 [008](008-lifecycle-version-release.md)）。

## 用户可见行为

- 重开、替换、甚至重建 EXE 都不弹登录窗；界面提示「已复用本机登录态」。
- 断网时胶囊显示「暂时无法确认登录态」，网络恢复点「刷新」即回正常，登录还在。
- 「开始报名计划」在 checking / unknown / none / expired 下都禁用，刷新与登录按钮始终可用。

## 工程契约链接

- [docs/UI_SPEC.md](../../docs/UI_SPEC.md) §5.9「登录态持久化与启动校验（验收条款）」
- [local/README.md](../../local/README.md)「登录态存在哪、重开还要再登一次吗」

## 已知限制

- 真实 4001 重登、真实断网恢复、EXE 重开复用这三条的真机复核仍留给用户（自动化只覆盖了模拟断网与单测）。

## 变更方式

数据根或四态语义改动需同步 `local/app.py`（`_env()`、`login_state()`、`_do_check`）、
`docs/UI_SPEC.md` §5.9、`local/README.md` 与 `local/tests/test_session_persistence.py`；
`result` 四态值与 `session_present` 等字段名是界面契约，不得重命名。
