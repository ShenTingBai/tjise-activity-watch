# 010 · 独立可用单 EXE 与设置入口

- **状态**：confirmed —— 产品规则已确认（2026-10-25 用户明确选择「改成独立可用的单 EXE」）。
  证据按「历史轮 / 当前轮 / 用户验收」三层分列（不混总）：
  - **历史轮实物证据**（20261025-shutdown-feedback-closeout 授权轮，2026-10-25）：仓库根统一
    入口 251 例两连绿（每轮 248 通过 + 3 授权门控跳过；Git fixture 3/3 单列运行）；onefile
    重建后 CArchive **181 条**（180 个文件/脚本条目 + 1 个运行选项，不误报数量矛盾），
    `grab-worker.exe` 为精确文件条目、`VERSION`=1.1.0、`ui/*`×4 与源码一致；主 EXE 与
    `only-exe2` 副本同为 SHA-256 `8e75c2c4…`；仅含单 EXE 的空目录 runtime-check 实跑
    `ok=true`、退出 0、probe-data 为空；mock 桥截图 3 张目检 + DOM 计数（组件说明/恢复
    入口全页恰一次）。——只证明**该轮源码**。
  - **边界轮实物证据**（20261025-runtime-final-boundaries **授权轮**，2026-10-25）：仓库根统一
    入口 **266 例两连绿**（每轮 263 通过 + 3 授权门控跳过；Git fixture 3 项另跑 3/3）；
    onefile 重建后主 EXE 与 `only-exe3` 副本同为 SHA-256 `a2c641e2…`，CArchive 180 个
    文件/脚本条目（引导口径 181=180+1 运行选项），四个内置 UI 与工作树逐字节一致
    （app.js 含 FB4 标志）；空目录 runtime-check `ok=true`、退出 0、probe-data 为空；
    四入口五图（check/list 两到达顺序、登录/开始同步失败、下一轮恢复）目检 + DOM 计数。
    FB1-FB3 经独立复审
    （[runtime-final-boundaries 复审](../../.claude/results/20261025-runtime-final-boundaries-codex-review.md)）
    闭合；**FB4 普通顺序已验证**。
  - **当前轮（20261025-component-action-round-closure）**：FB4-R1 交错微修补（登录/开始的
    同步组件失败复用 `noteRoundError` 进入本轮累计，迟到成功不得撤新横幅）与两组交错行为
    回归定义已落地，**未运行**——不能沿用 `a2c641e2…` EXE 或上一轮日志证明新 `app.js`；
    本轮 result 见
    [component-action-round-closure](../../.claude/results/20261025-component-action-round-closure-result.yaml)。
  - **待确认**：用户真机验收（辅助入口/独立单 EXE/缺组件提示 + 各轮新增行为）与发布
    均未完成。
  - **本轮（20261025-feedback-assistance-repair）**：辅助入口按用户授权重做——名称由「设置」改为
    「更多」、齿轮换省略号、三组同形卡片改为一层引导区 + 版本行 + 数据行，说明弹窗回补重播快捷入口；
    本轮只改 UI 与文案，未升版本、未重建 EXE、未跑测试，**不构成新的实包通过**。现行规范见
    [../specs/assistance-panels.md](../specs/assistance-panels.md)。
- **日期**：2026-10-25

## 问题

1. 发行目录里的单 EXE 不是完整程序：冻结主程序固定到「EXE 旁」找 `grab-worker.exe`，
   而发行目录只有 EXE/ZIP/manifest——用户直接双击 EXE 登录报 `[WinError 2]`，
   看起来像登录或网络问题。旧定位（单 EXE 仅是外置 worker 入口）被用户本轮决定取代。
2. 列表请求的启动异常没有终态事件：前端骨架永远加载，失败被吞。
3. 「重看引导 / 版本 / 最新版本」全堆在说明弹窗里，没有明确的「设置」入口。

## 用户确认的规则

1. **独立可用单 EXE**：主程序先把现有 grab-worker 构建为 onefile，再把它作为**普通数据资源**
   与 VERSION 一起打进主程序资源根（不用 `--add-binary` 重分析依赖、不自解压、不自动下载、
   不引入安装器）；冻结态从 `RESOURCE_ROOT/grab-worker.exe` 解析，源码态仍跑 `grab.py`；
   **不依赖 cwd、不静默借用 EXE 旁旧 worker**；主程序内执行完整既有 worker，不改 grab.py。
2. **ZIP 继续提供，收敛为四成员**：EXE、README.md、启动说明.txt、LICENSE——同一份独立 EXE
   加说明与协议；不再携带外置 worker；publish 核验「ZIP 内主 EXE 与单独 EXE 字节一致」。
3. **设置入口**：顶栏独立「设置」按钮（齿轮 + 文字），任何状态可打开；三组已确认功能——
   使用（重看六步引导；计划运行中禁用并说明，不打断计划）、版本（`bootstrap.version` 唯一显示位
   + GitHub 最新版本外链）、本机数据（打开数据目录）；**不新增**清凭证/重置/自动更新；
   说明弹窗只留三步快速帮助。
4. **组件失败闭环**：worker 缺失/权限/启动异常给结构化错误码
   （`worker_missing`/`worker_permission`/`spawn_failed`），主文案「程序组件缺失，请重新下载
   完整版本」，技术细节只进日志；check 失败强制 unknown + `runtime_error` 字段（与登录四态
   分离，不清会话、不指导重登）；check/list 恰一次终态事件、finally 清标记；login 同步失败
   返回 `ok=false`；`start_plan` 保留登录门禁并拒绝组件不齐；前端一条横幅 + 「重新检查」，
   缺组件禁用登录/开始，刷新可复查恢复。
5. **关闭收尾**：正常关闭先停本实例协调器，再按树终止（taskkill /F /T /PID）结束**仅本 Api
   登记**的 worker（含 check/list/login/plan），之后才 finalize 生命周期；绝不按进程名扫杀。
6. **离线探针**：`app.py --runtime-check --report <绝对JSON路径>` 先于数据根/GUI 分发；
   缺 `TJISE_DATA_DIR` 拒绝；只按真实解析路径启动内置 `worker --help`（限时 60 秒）；
   写脱敏 JSON，退出码 0/1/2；**实包验收必须包含「仅含单 EXE 的空目录实际启动内置 worker
   --help 且退出 0」**——单测与主窗口截图不能替代。
7. **边界不变**：版本仍 1.1.0、TOUR_VERSION 仍 1；「独立」指无需外置 worker，仍需要系统
   WebView2 与 Chrome/Edge，不打包浏览器/运行时；数据根仍按 `TJISE_DATA_DIR`/LOCALAPPDATA
   现有规则，绝不把凭证写进解包资源目录或发行包；停止树终止、双报名模式、冲突、重试和
   签到/签退三态契约不改变。

## 后续取代范围

辅助入口、视觉和手动版本比较已由 [011](011-unified-help-and-manual-update.md) 取代。
下述双入口记录是该轮历史决定；独立EXE、四成员ZIP、组件诊断和关闭收尾继续有效。

## 历史返修（2026-10-25，只取代当时辅助入口的名称与视觉）

- **入口名**：顶栏第二个入口由「设置」改为「更多」；图标由齿轮改为同源内联 SVG 省略号；
  `aria-label` 写全「更多：重看引导、版本与本机数据」。内部 id（`#settings`/`btn-settings-*`）保留，
  避免为改名做大规模重构。
- **布局**：只有一块浅琥珀引导区（左 3px 琥珀短线）承载「重看引导」；版本行与「本机数据」行改为
  普通行 + 细分隔；固定头尾、只有正文滚动；窄窗（≤720）动作落到说明下方并左对齐。
- **说明弹窗**：保留三步快速帮助与「知道了」，回补「重看引导」快捷入口；版本与外链只在「更多」。
- **三步文案**：改为「确认登录 / 选活动并开始 / 看进度与结果」，并写明「再点「开始报名计划」」。
- **引导内容版本**：`TOUR_VERSION` 1→2（第 2/5/6 步文案实质变化）；软件版本仍 1.1.0。
- **不变**：独立单 EXE、四成员 ZIP、worker 唯一来源、组件失败闭环、关闭收尾、离线探针
  及其全部历史实包证据都按本节原文保留；本 UI 返修轮未重建、未复跑、未发布。

## 用户可见行为

- 直接下载单 EXE：拷到任意空目录双击即可用；缺组件时得到明确提示与「重新检查」，
  不再看到 WinError 2 或「正在加载」卡死。
- 顶栏「更多」（返修轮前的「设置」）：重看引导 / 看版本与去发布页 / 打开数据目录；
  「说明」是三步快速帮助，底部也有「重看引导」。
- 列表加载失败必有结论，不会一直转骨架；数据没加载时不显示「近期没有要开放的活动」。

## 工程契约链接

- `docs/UI_SPEC.md` §5.13（设置入口/组件闭环/探针验收条款）、§5.12.6（四成员资产形态）
- `local/app.py`（`resolve_worker_path`/`worker_problem`/`_component_error`/`Api.shutdown`/
  `_runtime_check_main`）、`local/build_exe.py`（worker 先构建、作为 extra_data 进资源根）、
  `tools/release.py`（`ZIP_MEMBERS` 四成员、内外 EXE 一致性核验、`remote_tag_commit` 双 pattern）
- `local/tests/test_runtime_bundle.py`（worker 解析/失败收尾/关闭生命周期/探针/构建 mock）、
  `local/tests/test_ui_contract.py`（设置入口静态与 node vm 行为）、
  `local/tests/test_lifecycle_release.py`（四成员 fixture、EXTRA-1 参数断言与授权门控 Git fixture）
- `local/README.md`（新交付形态与旧口径历史标注）、`CHANGELOG.md` 1.1.0 草案

## 已知限制

- 历史轮实物证据只证明该轮源码；本轮 FB4-R1 微修补的新回归**未运行**，当前
  `dist/tjise-grab-exe` 的 EXE（`a2c641e2…`）**不含该修补**——不能沿用其哈希当本轮通过证据；
  正式二进制需在后续授权轮重建并复跑空目录实包探针与「刷新未收尾时登录/开始失败」的 UI 复验。
  另：历史轮证据脚本的 DOM 计数只打印到 stdout（复审指出未单独落盘 JSON），后续证据脚本
  应把断言状态与计数同时保存 JSON。
- **预览路径不混淆**：新版预览（内置 worker 的独立 EXE）在
  `dist/tjise-grab-exe/tjise-activity-watch.exe`；`dist/release-v1.1.0/` 仍是**旧的外置
  worker 预览包**——两个路径不得当作同一轮产物，正式候选须在最终验收、提交合并后从
  干净 main 重新 `--prepare`。
- ls-remote 真实过滤回归（`RealGitFixtureTests`）需要显式授权（`TJISE_GIT_FIXTURE_AUTH=1`）
  才会在临时本地 Git 仓库执行。
- 「独立」不覆盖系统依赖：WebView2 与 Chrome/Edge 缺失仍是用户侧前置条件。
- 「设置/更多入口」与应用内更新属**产品讨论输入**，未确认为规则，不进入实现与契约；
  本决策不把它们记成已确认功能，不重做设置布局、不升版本。

## 变更方式

- worker 打包方式调整：`local/build_exe.py` 的 `assemble()` 与 `local/app.py` 的
  `resolve_worker_path` 必须同改同验（资源落点与解析是同一契约）。
- 辅助入口增删：`local/ui/index.html` 的 `#settings` + `local/ui/app.js` 的 `replayTourFrom`/`paintReplayButtons`
  + `docs/UI_SPEC.md` §5.13 + `design/specs/assistance-panels.md`；未确认功能不得进入。
- ZIP 成员调整：`tools/release.py` 的 `ZIP_MEMBERS` + `verify_release_assets` +
  `local/tests/test_lifecycle_release.py` fixture 同步。
