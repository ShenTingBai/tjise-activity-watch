# 013 · 分发形态、隐私边界与条款同意策略

- **状态**：implemented（源码与文档随计划 20261027-distribution-contract-release 落地，
  20261027-distribution-contract-repair 返修后端门禁、卸载安全与条款 r1）；
  v1.1.1 的三资产已完成同源构建并发布；真实学校窗口仍属于用户验收范围。
- **日期**：2026-10-27（当日返修）
- **上游**：[决策 008](008-lifecycle-version-release.md)（发布门禁与资产绑定）、
  [决策 010](010-standalone-exe-and-settings.md)（独立 EXE）、
  [specs/versioning-and-release](../specs/versioning-and-release.md)、
  [specs/copywriting §15](../specs/copywriting.md)

## 问题

分发形态口头上有四种（绿色版 / 独立 EXE / 完整 ZIP / 安装版），实际只发布两种，
README 却让用户任选三种——其中一种在最新 Release 里不存在；「便携」一词跨形态混用
（冻结 EXE 的数据写 `%LOCALAPPDATA%`，根本不便携）；对外没有任何条款与隐私告知，
也没有程序内的同意入口；群机器人的认证边界（谁登录、谁能做什么）散在各文档、
口径不一。

## 已确认的规则

### 1. 分发形态（用户视角）

| 形态 | 地位 | 数据位置 |
|---|---|---|
| **安装版**（Inno Setup，中文向导） | **已随 v1.1.1 正式发布**：与独立 EXE、完整 ZIP 来自同一冻结 EXE | `%LOCALAPPDATA%\TianSoftActivityPreRegistration\data` |
| **独立 EXE** | 已发布正式资产 | 同上 |
| **完整 ZIP**（四成员） | 已发布正式资产，免安装 | 同上 |
| 绿色版 `tjise-grab-portable-*.zip` | **历史形态**（v1.0.2 及以前，停止新增发布） | 程序旁 `data/`（历史口径） |

- 三种现行形态由同一份冻结 EXE 派生，manifest 记录三者哈希（见决策 008 补记）；
- **不改名**：仓库、EXE 文件名、安装目录 `TjiseActivityWatch`、数据目录
  `TianSoftActivityPreRegistration`、Inno AppId 全部保持稳定——升级覆盖安装与登录态
  迁移不因此破坏；产品名全仓统一「天软活动预报名工具」；
- 「便携」一词从现行形态的文档里清除；只有历史绿色版的数据才随程序走。

### 2. 数据目录稳定性

`%LOCALAPPDATA%\TianSoftActivityPreRegistration\data` 是冻结 EXE 的唯一数据根
（`TJISE_DATA_DIR` 可覆盖用于测试）。改这个目录名 = 已发布用户的登录态"消失"，
必须有显式迁移方案才允许动，本轮明确**不动**。文档按「帮助 → 打开数据目录」引导，
不要求用户记路径。

### 3. 安装器升级路径

同 AppId 覆盖安装：`PrivilegesRequired=lowest` 默认装 `%LOCALAPPDATA%\Programs`，
免 UAC。卸载删数据必须同时满足两条（2026-10-27 返修，缺一条都不行）：

- **交互卸载**：`SuppressibleMsgBox(..., MB_YESNO or MB_DEFBUTTON2, IDNO)`——默认按钮
  是「否」，被抑制时返回 IDNO；只有用户明确点「是」才 `DelTree` 数据目录；
- **静默卸载**：先判 `UninstallSilent` 直接跳过删除分支。被抑制的返回值不是用户的选择，
  不能拿来当删除依据（`/VERYSILENT` 曾因此整块删掉登录态）。

安装目录白名单四文件（EXE、README.md、遇到问题先看我.txt、LICENSE），
CHANGELOG 与配图不入包。

### 4. 群机器人认证边界

服务器版的学校会话**只有部署者本人一个**：群成员不登录学校系统、不向机器人提供凭证；
群入口（webhook 推送、@ 查询）只读；私聊动作只认 `PRIVATE_OWNER_OPEN_ID` 对应的
部署者本人。完整表格见 [ARCHITECTURE.md](../../docs/ARCHITECTURE.md) 与
[FEISHU_BOT.md](../../docs/FEISHU_BOT.md)。

### 4.1 纠正：三个业务入口 ≠ 三个飞书应用（2026-10-28，计划 20261028-feishu-single-app-dual-entry）

- **单一自建应用承载群查询与 owner 私聊**：群 @ 查询与私聊动作同属一个飞书自建应用，
  `BOT_ROLE=combined` 一个长连接进程按 `chat_type` 分流；Webhook 自定义机器人仍只是
  单向推送通道，无入站事件。三个业务入口由两条通道承载，不是三个应用。
- **同一 App 只能一条长连接**：同 App ID/Secret 开两个长连接进程会互相抢事件（同一件事
  被各答一次，或一边永远收不到），所以拆分期的 `group_query` / `private_owner` 双实例是
  错误形态；旧值现在拒绝启动并提示改用 `combined`。
- **安全边界不靠进程隔离**：群事件必须被 @ 且 `chat_type=group` 才回公开查询、不写
  `users.json`、卡片无个人视图；owner p2p 才过 `isPrivateOwner` /
  `ownerCardActionAllowed` / `handleGrabAction` 三重校验；非 owner p2p 固定拒绝。
  数据边界与披露不变。

### 5. 条款同意策略（本机版）

- 首次需要学校动作（登录/校验/列表/报名）且未同意时，弹《使用条款与隐私告知》；
  **拒绝后不退出程序、不重复强弹**：帮助、设置、条款全文与离线演示仍可用，
  登录、活动列表、报名计划保持停用，并通过横幅与帮助保留重新同意入口；
- 同意状态写 `ui.json`（`terms_version` + `terms_accepted_at`，原子偏好事务），
  **不写** `session.json`/`storage_state.json`/日志；「本版本已拒绝过」另存
  `terms_declined_version`（重启后据此恢复会话状态，避免刷新重复弹窗）；
- **后端是最终防线**：`check` / `list_activities` / `login` / `grab` / `start_plan`
  都在起线程、worker 或计划协调器之前做 `_terms_gate` 判定，未授权返回稳定的
  `terms_required`；`start_plan` 的重放结论优先于门禁，被拒请求收成 `rejected`
  而不留悬空 pending；`demo` 的 `grab` 只给离线结论，不起真实 worker；
- 前端在 bootstrap 失败、返回 null、缺 terms、版本为空或结构非法时进入 **unknown**
  的 fail-closed 态：拦住学校动作、挂可重试横幅，但不自动弹窗；
- 条款版本 **2026-10-27-r2**（日期或日期加修订号；首版 2026-10-27-r1，修订记录见 §5.1）：
  版本变化必须由用户**再次点击同意**，
  学校动作才恢复；旧版本的同意记录不会自动延伸；
- 条款全文 = `docs/TERMS_AND_PRIVACY.md`（规范来源）与界面内嵌全文（冻结 EXE 无 docs/
  时仍可读）同源；**docs 不作为第五个 ZIP 成员**；
- 许可边界：`LICENSE`（MIT）是代码授权真源，**条款不新增 LICENSE 之外的禁止事项**；
- 隐私披露覆盖实际会发生的三类通信：学校访问（同意后含周期刷新）、网络授时、
  用户主动的 GitHub 更新检查；服务器版另披露飞书事件里的 `open_id`/`chat_id`/
  消息与卡片字段由部署者保存；不承诺运行环境证明不了的「绝对不传输」。

### 5.1 条款 r2 修订记录（2026-10-27）

- **当前版本 2026-10-27-r2；首版 2026-10-27-r1 保留为历史决策**，§5 的策略条目
  （拒绝后不退出、后端 `_terms_gate` 为最终防线、unknown fail-closed、原子偏好事务）
  在 r2 未作改动，只是披露文字补全。
- **修订原因**：r1 的 §6.1 只写了默认数据目录与「删除/卸载」，没说清三件事——
  `TJISE_DATA_DIR` 对源码态与冻结 EXE 的覆盖；旧 EXE 旁数据迁移是**复制且保留源文件**
  （只补缺失、不覆盖，原处副本仍在）；卸载的可选删除**只针对默认 LOCALAPPDATA 数据目录**，
  自定义覆盖目录与旧迁移源副本不在其删除范围内，需逐一确认后自行清理。
- **同意的效力**：r1 的同意**不延伸至 r2**。`local/app.py` 的 `TERMS_VERSION` 升为 r2 后，
  `terms_state` 只把等于当前版本的 `terms_version` 视为已同意，r1 记录自然失效，
  程序会再次展示条款并要求重新点击同意；拒绝记录同样按版本失效。
- **同源与边界**：版本字段四处一致——`local/app.py` 常量、`docs/TERMS_AND_PRIVACY.md`
  版本行、界面内嵌全文（`local/ui/index.html`）、测试常量（`test_terms_consent.py` /
  `test_ui_contract.py`）；本轮只改披露文字与版本号，不改迁移、卸载、门禁与任何对外通信
  行为，不新增遥测。

### 5.2 实现契约修正记录（2026-10-28 条款同意 UI 轮，计划 20261027-terms-consent-ui-repair）

- **同意提交绑定实际文档版本**：`Api.accept_terms(document_version)` 增加版本参数，取值
  只来自界面内嵌全文的 `#dlg-terms-document-version`（用户实际读到的那份副本），前端提交前
  先做本地预检；成功回执仍须 `version / accepted_version / accepted_at` 齐全、
  `accepted_version === version` 且与本份同版才解锁（前端校验与后端校验缺一不可）。
- **写前拒绝不匹配**：类型与等值校验发生在 `time.strftime` 与 `update_prefs` **之前**——
  无参、空值、非法类型或不同版本一律 `{ok:false, error:"terms_version_mismatch"}`，
  写次数为零、不起线程。修正前是「先写后验」，旧 HTML 副本也能给新后端投出「同意当前版」；
  prefs 锁竞争沿用 `prefs_busy` 既有语义。
- **只读查看**：已同意同版从帮助打开为只读查看——显示已同意状态与同意时间，只有「关闭」，
  打开、关闭、Esc 全程零写入、零学校加载，不记拒绝标记。
- **模态顺序**：条款决定先于首次欢迎与六步引导（延后到同意后只继续一次，绝不写
  `tour_version` 伪装看过）；帮助与条款不叠开，关闭后焦点回来源入口；document 级快捷键
  第一道判断是打开的条款弹窗（先于引导与搜索处理）。
- **边界**：本记录只涉及本机 UI 与桥接契约；**法律与隐私正文未改，版本仍
  `2026-10-27-r2`**，§5.1 的 r1/r2 历史不抹除。UI 实机验收与发布仍待用户确认。

## 待用户验收 / 待授权事项

1. 条款弹窗的实机体验（首次弹、拒绝后停用、版本变化重弹、后端回绝的界面收敛）；
2. 三资产正式构建 + 安装后哈希探针（隔离临时目录静默安装，比对安装出的 EXE
   SHA-256 与独立 EXE 一致）；**当前 `dist/release-v1.1.1` 是脏工作区预览，不可发布**；
3. 本机卸载/静默卸载的实机探针（本轮只改模板与结构测试，未跑原生探针）；
4. 旧 Release 页（v1.0.x）人工标注「绿色版已停止更新」；
5. 实现侧任务卡仍显示「立即报名」加抢单字样的旧复合标签，与文案词表不一致，
   待下一 UI 轮次收敛（见 copywriting §15 记录）；
6. 返修轮（20261027-distribution-contract-repair）未运行构建与测试，
   653/78 的历史成绩只作上一轮证据，不继承为本轮通过；
7. 条款同意 UI 轮（20261027-terms-consent-ui-repair）：实现与测试代码已落盘但
   **测试未运行**，截图/缩放/原生 WebView2/真实学校验收全部待授权，不继承为通过。

## 用户可见行为

- 下载页三种现行资产并列，绿色版明确标为历史；
- 首次使用先看条款再登录；拒绝也能看帮助和设置；
- 帮助面板「打开数据目录」回显真实路径；排障文档教的是脱敏反馈，不是发原始日志。
