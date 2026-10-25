# 008 · 应用生命周期、版本单一来源与 EXE/ZIP 发布资产

- **状态**：pending —— 产品规则已确认（2026-10-25）；实现已随计划 20261025-lifecycle-tour-release 落地，
  并按计划 20261025-lifecycle-tour-release-repair 返修（运行周期标记、估算同源、发布资产绑定），
  再按计划 20261025-release-final-gate 收尾（确认后来源锁定、远端 tag 核验、时间矛盾退未知）。
  发布资产形态后被 [决策 010](010-standalone-exe-and-settings.md) 部分取代（独立单 EXE + 四成员 ZIP），
  相关源码随计划 20261025-standalone-exe-settings 落地。
  各轮「已运行」证据分列于下表（153→179→197 均属对应轮次的历史报告，不互相沿用）；
  FG/收尾/独立 EXE 各轮新增回归未运行，发布/真机验收未执行，不得提前标 implemented。
- **日期**：2026-10-25

## 问题

三个各自为政的问题：① EXE 关掉再开，用户不知道上次关了多久、登录还剩多久；
② 版本号在 GUI 写死、构建与发布各自命名，发布出去对不上号；③ Release 只有绿色版 zip，
没有「下载即用」的 EXE 入口，且发布工具任何调用都会打 tag / 上传，无法在 feature 分支安全演练。

## 用户确认的规则

1. **生命周期元数据只写 `ui.json`**：启动在同一原子写入里完成「冻结上一运行周期 →
   记录本次 `last_started_at` + 首个 `last_heartbeat_at` + `run_open=true`」；
   运行中每分钟原子写 `last_heartbeat_at`；窗口正常关闭先在锁内核验停止标记（迟到心跳放弃）、
   再原子写 `last_closed_at` + `run_open=false`，不持锁 join 心跳线程。
   绝不写入 `session.json` / `storage_state.json`；与界面偏好共用同一把持久化锁。
2. **关闭时长按运行周期口径**：周期以 `run_open` 标记区分——上一周期正常关闭（标记 false、
   关闭不早于该周期启动、心跳不晚于关闭）给精确值；`run_open=true`（没走正常关闭，
   含刚启动几秒就崩）只拿最近存活时刻（心跳，退该周期启动）给「约」，**绝不拿更早周期的
   关闭时间冒充本周期正常退出**；标记 false 但时间自相矛盾（关闭早于可解析的本周期启动，
   如系统回拨后关机）退「未知」——正常关闭标记不能覆盖时间顺序矛盾，同一秒等号仍算正常；
   旧格式 `ui.json`（无标记）时间戳兼容，但关闭记录早于上一周期启动时降级「约」；
   首次运行 / 时钟回拨给「未知」。
3. **登录估算是提醒不是判据，且全界面同源**：按 `session.saved_at + 约 17.5 小时` 绝对墙钟
   计算，关闭期间照算；`bootstrap.login` 与 `check` 事件携带同一份锚点，胶囊/时钟浮层/计划
   检查条按当前时刻现算并随秒递减；窗口内重新登录 → 旧锚点立即作废换新锚点；反复「刷新」
   同一凭证不重置寿命。估算到点但学校校验 valid 时界面仍显示「登录有效」。估算不得参与
   `start_plan` 放行/拦截，不得强制过期、清理会话或刷新 `saved_at`。锚点缺失显示「未知」，
   不编 17 小时。四态校验（valid/expired/unknown/none）口径不变。
4. **版本单一来源与版本线**：仓库根 `VERSION` 文件（major.minor.patch）是 GUI、帮助、构建脚本、
   Release 工具的唯一版本来源；缺失或格式非法直接启动失败，不静默回退旧版本。
   读取顺序：冻结资源根 → 脚本旁（绿色版）→ 仓库根，不依赖启动 cwd。
   版本线（[specs/versioning-and-release](../specs/versioning-and-release.md)）：实际已发布基线
   v1.0.2；当前候选 **1.1.0**；内部 3.x 是未发布草案，不构成对外历史；同一候选的返修/验收
   轮次不升版；发布后 bug 用 patch、累计功能用 minor、兼容性破坏才评估 major。
5. **版本链接**：说明弹窗显示当前版本并提供「GitHub 最新版本」按钮——用户点击时用系统
   默认浏览器打开固定 `releases/latest` 地址；启动时不自动联网、不在 WebView 内跳转。
6. **发布资产**（历史口径，已被 [决策 010](010-standalone-exe-and-settings.md) 取代）：完整便携包
   `天软活动预报名工具-vX.Y.Z-win-x64.zip`（白名单成员：主 EXE、`grab-worker.exe`、README、
   启动说明、LICENSE）+ 单文件入口 `天软活动预报名工具-vX.Y.Z-win-x64.exe`；ZIP 是推荐下载项，
   单独 EXE 必须与 `grab-worker.exe` 同目录使用（文档与启动说明里写清）。
   **2026-10-25 起现行口径**：单 EXE 独立可用（worker 内置进资源根），ZIP 收敛为
   EXE + README + 启动说明 + LICENSE 四成员；本条保留为被取代的历史记录。
7. **发布三阶段互斥 + 资产绑定 + 确认后来源锁定**：`--check`（默认，只读零副作用）→
   `--prepare`（本地构建组装资产 + manifest：版本/tag/分支/**完整 HEAD**/`source_dirty`/每文件
   SHA-256；构建前后来源必须一致，脏构建只能预览；可在 feature 分支）→ `--publish`（干净 main →
   tag 只由 VERSION 推导、位置 tag 仅作校验 → **逐项核验候选资产**（manifest 一致且为对象、
   `source_dirty` 必须显式布尔 false——缺失/null/0/字符串都是来源未知，拒绝、来源=当前完整 HEAD、
   EXE/ZIP 齐全、哈希/大小一致、ZIP 成员恰为白名单）→ 人工确认 → **确认后复核** → tag → 上传）。
   人工确认可能等待很久：确认返回后、第一次 Git 写入前重验「同一干净 main/完整 HEAD/VERSION/
   资产指纹」，期间任何变化中止并要求重新确认；上传前再比对一次指纹。新 tag 用
   `git tag -a <tag> <已核验提交>` 显式绑定；origin 同名 tag 用只读 `git ls-remote` 独立核验
   （peeled 优先，查询失败按错误拒绝而非当作不存在），本地或远端指向不一致都在写入前拒绝、
   不 force；远端已正确则复用、远端缺失且本地正确才 push、两边都缺才创建并 push，push 后再查
   落地。`gh release create --verify-tag` 禁止 gh 自动补 tag；已有 Release 默认不覆盖附件
   （显式 `--clobber` 才行）；绿色版/服务器版打包与分发分支同步是另行授权的兼容流程，
   publish 不隐式触发。
8. **发行包隔离**：ZIP 按白名单组装，`OUT/data` 里的旧登录态/任何 data、config、log、缓存
   都进不了发行包；启动说明生成器输入统一 LF 再输出 CRLF（防 CRLF 检出环境产生 CR CR LF）。

## 用户可见行为

- 重开 EXE：顶栏时钟浮层显示「上次关闭后离线」（正常关闭精确、异常退出带「约」）与
  「登录预计还剩」（与胶囊同源、随秒递减）。
- 说明弹窗左下角显示当前版本（来自 VERSION，当前候选 1.1.0），旁边「GitHub 最新版本」按钮。
- Release 页：完整 ZIP 解压即用；单独 EXE 缺 worker 时启动失败，启动说明与 README 均提示改下 ZIP。

## 工程契约链接

- [docs/UI_SPEC.md](../../docs/UI_SPEC.md) §5.11/§5.12；[docs/LOGIN.md](../../docs/LOGIN.md) 生命周期小节
- `local/app.py`（`read_version` / `AppLifecycle` / `compute_lifecycle`）、`local/build_exe.py`、
  `local/make_portable.py`、`tools/release.py`
- 静态契约：`local/tests/test_lifecycle_release.py`、`local/tests/test_session_persistence.py` 的
  LifecycleContractTests、`local/tests/test_ui_contract.py` 的行为测试（node vm 装置）

## 验收与发布状态（分项记录，不得合并成一个「完成」）

| 项 | 状态 | 证据 |
|---|---|---|
| 产品规则确认 | 已确认（2026-10-25） | 本文档 |
| 首轮源码实现 + 静态契约 | 已落地（2026-10-25） | 计划 20261025-lifecycle-tour-release；AST/node/门禁 grep 全过 |
| 首轮单测（授权轮） | 已运行（2026-10-25） | 153 例全绿（含修 compute_lifecycle 崩溃）；属返修前代码 |
| 首轮构建（授权轮） | 已运行（2026-10-25） | dist/release-v3.1.0 资产 + manifest；旧草案候选，不得发布或改名 |
| 返修源码（R1–R6 + 版本线 1.1.0） | 已落地（2026-10-25） | 计划 20261025-lifecycle-tour-release-repair；AST/node 静态检查过 |
| 返修后单测 + 编译 + 预览构建（授权轮） | 已运行（2026-10-25） | **179 例全绿**（含 run_open/估算同源/资产核验/JS 行为 26 例新增）、14 文件 py_compile、dist/release-v1.1.0 预览（manifest `source_dirty=true`）；属发布收尾前的代码 |
| 发布收尾源码（FG1–FG3：确认后复核/远端 tag 绑定/时间矛盾退未知） | 已落地（2026-10-25） | 计划 20261025-release-final-gate；AST+结构契约静态检查过 |
| 收尾新增回归（确认期间变化矩阵/远端 tag 矩阵/source_dirty 显式 false/时间矛盾） | **pending_authorization** | 已定义未运行（40+ 用例定义在 test_lifecycle_release/test_session_persistence）；不得沿用 179 旧数字 |
| 独立 EXE + 设置入口源码（决策 010：worker 内置/四成员 ZIP/组件闭环/runtime-check/EXTRA-1 ls-remote 参数） | 已落地（2026-10-25），**回归未运行** | 计划 20261025-standalone-exe-settings；AST/node --check 静态过；新增回归定义在 test_runtime_bundle/test_ui_contract/test_lifecycle_release，待授权执行 |
| 1.1.0 正式候选构建 | **pending_authorization** | 须在干净 main 提交合并后重新 --prepare；当前 dist/release-v1.1.0 是脏预览，publish 会拒绝（且其五成员 ZIP 在新白名单下会被拒） |
| GitHub Release 上传（tag + 资产） | **blocked（未申请）** | 需用户另行批准；publish 有「干净 main + 资产核验 + 确认后复核 + 远端 tag 核验 + 人工确认」门禁 |
| 有效登录真机验收 | **pending_user_review** | 见下方真机验收清单 |

### 真机验收清单（用户执行）

1. 有效登录后关闭 EXE，隔一段再开：时钟浮层显示「上次关闭后离线」与「登录预计还剩」，数值符合实际间隔（绝对墙钟、随秒递减）。
2. 任务管理器强杀 EXE（模拟崩溃）后重开：显示「约」，不伪造精确时间；刚启动几秒就强杀再开，同样只给「约」（不冒用更早周期的精确值）。
3. 窗口内重新登录一次：胶囊与浮层的「预计还剩」立即按新登录时间起算，两个位置一致；连点「刷新」数值不回弹、不续期。
4. 估算到点（登录约 17 小时前保存）但学校校验有效：胶囊显示「登录有效」+「估算已到点，以学校校验为准」。
5. 首次打开自动进入六步引导；引导内 Tab 只在卡片按钮间循环、Ctrl+F 不再跳搜索框；跳过后重开不自动弹；说明页「重新看一遍引导」可重播；窄窗口下退底部说明条。
6. 说明弹窗版本与 Release 资产名一致（同一 VERSION）；「GitHub 最新版本」按钮用系统浏览器打开发布页。
7. EXE/ZIP 分发：独立单 EXE 拷到空目录双击可用（决策 010 实包探针验收）；组件缺失时得到
   「程序组件缺失，请重新下载完整版本」的可读提示而非 WinError 2；「设置」三组功能可用。

## 已知限制

- 异常退出的关闭时长只能「约」（心跳粒度 60 秒）；系统时钟回拨时退「未知」。
- ~~单独 EXE 入口对「解压到任意目录双击」的场景不完整——这是有意保留的定位（入口文件）~~
  **已被决策 010 取代**：单 EXE 现内置 worker 独立可用；完整分发包仍是 ZIP。
- 登录估算的 17.5 小时是单次观测样本（见 docs/LOGIN.md §1），只作提醒；探针数据仅服务器版有。

## 变更方式

- 版本变更：只改 `VERSION`（一处），GUI/构建/发布资产名全部跟随；升版口径见
  [specs/versioning-and-release](../specs/versioning-and-release.md)。
- 生命周期字段或口径变更：`local/app.py`（`AppLifecycle`/`compute_lifecycle`）、
  `docs/UI_SPEC.md` §5.11、`docs/LOGIN.md`、`test_session_persistence.py` 同步。
- 资产清单或核验规则变更：`tools/release.py` 的 `ZIP_MEMBERS`/`verify_release_assets` +
  `test_lifecycle_release.py` 同步。
