# 设计知识库 · 天软活动预报名工具

> 状态：**导航入口**（2026-10-24 重建）。本页只做索引和边界说明，不再承载一份完整且容易过时的业务契约。
> 「准点」是 2026-10-22 视觉原型的历史称呼，只出现在历史说明与设计资料里，不是产品名（见
> [决策 001 · 产品命名](decisions/001-product-naming.md)）。

## 目录导航

| 目录 | 放什么 |
|---|---|
| [decisions/](decisions/) | **已敲定的产品决策**：为什么这样定、用户看到什么（10 份记录） |
| [flows/](flows/) | **当前用户流程**：首次使用 → 选活动 → 混合计划运行 → 停止与结果（4 份流程） |
| [specs/](specs/) | **交互规范**：组件、状态、文案、响应式行为（辅助入口见 [specs/assistance-panels.md](specs/assistance-panels.md)） |
| [proposals/](proposals/) | **新方案提案**：尚未实现、未进入实现计划的想法 |
| [archive/](archive/) | **历史原型说明**：prototype/ 与 shots/ 的定位与边界 |
| [prototype/](prototype/) · [shots/](shots/) | 2026-10-22 视觉原型与截图资产（原路径未动，链接不失效） |
| [current-ui/](current-ui/) | **当前实现的真实渲染截图**（mock 数据，2026-10-24 生成，待用户验收） |
| [check_refs.py](check_refs.py) | 校验本页历史基线章节里的代码引用（用法见文末） |

## 三层职责：design / docs / local

产品决策集中在 design/decisions/，用户流程在 design/flows/，交互规范在 design/specs/；
它们回答「为什么、怎么用、长什么样」，字段与验收标准仍在 docs/。

| 层 | 回答的问题 | 谁维护 |
|---|---|---|
| `design/`（本目录） | 产品规则为什么这样定、用户流程怎么走、文案怎么写 | 产品设计 |
| `docs/`（工程真源） | 字段定义、接口契约、验收标准（[UI_SPEC.md](../docs/UI_SPEC.md)、[RETRY_POLICY_GUIDE.md](../docs/RETRY_POLICY_GUIDE.md) 等） | 工程 |
| `local/`（代码） | 实际行为；`local/README.md` 是使用者手册 | 工程 |

规则冲突时以 `docs/` 契约与代码为准，并回 design/ 修正记录——不在这里复制第二套契约。

## Git 分支边界

设计文档在评审期间可以放在短期 `design` 分支；确认后合并回 `main`，再删除临时分支。
`main` 是代码、docs 和 design 的唯一开发真源；`local` / `server` 是发布时由 `main` 自动生成的只读快照，
不在快照分支上继续编辑设计或业务规则。分支职责和快照刷新规则见根目录 [README.md](../README.md) 的“Git 分支与发布边界”。

## 已确认功能索引

| 功能 | 状态 | 决策记录 |
|---|---|---|
| 产品命名（天软活动预报名工具） | implemented | [001](decisions/001-product-naming.md) |
| 双报名模式与已截止（预报名 / 立即报名·抢名额 / 已截止） | implemented | [002](decisions/002-signup-modes.md) |
| 可配置重试策略与报名截止硬上限 | implemented | [003](decisions/003-retry-policy.md) |
| 多活动计划与举办时段冲突检查 | implemented | [004](decisions/004-multi-activity-and-conflict.md) |
| 签到 / 签退三态筛选 | implemented | [005](decisions/005-attendance-filters.md) |
| 登录态持久化与启动校验（四态 / 4001 / 迁移） | implemented | [006](decisions/006-session-persistence.md) |
| 运行时间线（真实事件 + 原定参考 + 统一截止） | implemented（真机验收待用户） | [007](decisions/007-runtime-timeline.md) |
| 应用生命周期、版本单一来源与发布资产 | implemented（构建/上传待授权，真机验收待用户） | [008](decisions/008-lifecycle-version-release.md) |
| 六步可重播功能引导 | implemented（mock 截图见 [current-ui/](current-ui/)，真机验收待用户） | [009](decisions/009-replayable-product-tour.md) |
| 独立可用单EXE | implemented（离线实包通过，用户真机待验收） | [010](decisions/010-standalone-exe-and-settings.md) |
| 统一帮助、主题对齐与手动检查更新 | confirmed（代码已落地，技术与用户验收分列） | [011](decisions/011-unified-help-and-manual-update.md) · [规范](specs/assistance-panels.md) |

状态口径见 [decisions/README.md](decisions/README.md)：confirmed 只表示规则已敲定，
implemented 另要求代码落地并有证据，proposed 一律放 [proposals/](proposals/)。

## 未闭合问题索引

| 问题 | 性质 | 跟踪 |
|---|---|---|
| 有效登录下的真机验收（开放前 100 秒真实开页 / 前 5 秒盯按钮 / 混合计划 / 重启恢复 / 停止链路） | 只有用户能做 | [007 已知限制](decisions/007-runtime-timeline.md) |
| 真实已截止活动视觉（demo 无样本）、真实断网确认 | 只有用户能做 | 同上；断网另有模拟证据 |
| EXE 重开复用登录态真机确认 | 只有用户能做 | [006 已知限制](decisions/006-session-persistence.md) |
| 可重播分步功能引导 | 已实现（mock 截图 ≠ 有效登录验收），待用户真机过一遍 | [009 已知限制](decisions/009-replayable-product-tour.md) |
| 生命周期提示 / 版本查看 / Release 入口真机确认 | 只有用户能做 | [008 已知限制](decisions/008-lifecycle-version-release.md) |
| 五项体验缺陷 UX-01～05（空态误导 / 三步缺开始 / 已知零显示破折号 / 运行目标读旧选择 / 单场成功染整屏） | 隔离运行已通过，**用户与学校实测仍分列** | [UI_SPEC §5.14](../docs/UI_SPEC.md) · [辅助入口规范](specs/assistance-panels.md) |

## 历史原型说明

`prototype/` 与 `shots/` 是 **2026-10-22 的视觉原型与截图资产**（基线 `main@a66016a`），
保留用于比较视觉来源；其旧业务边界（单活动、无多选、固定 120 秒重试）**不再是当前规则**。
当前规则跳转 [decisions/](decisions/) 与 `docs/` 技术真源，详见 [archive/README.md](archive/README.md)。

## 历史基线（2026-10-22 视觉基线摘要）

本章保留旧设计文档的核心视觉事实，供追溯「界面为什么长这样」；
其中与现行功能冲突的业务表述已逐条标注为「已被取代」。完整交互细节在
[prototype/](prototype/)（总览 `index.html`、组件规范 `components.html`）与 [shots/](shots/)。

### 设计概念：候车厅里的一块大屏

工具的本质是「几点几分开放，我要准时赶上」，与赶火车同构。借车站三样东西：

| 车站 | 界面 | 承担的信息 |
|---|---|---|
| 时刻表 | 按开放时间排、按天分组的活动列表 | 哪些能报、几点开放、还有多久 |
| 发车大屏 | 顶部 LED 屏 | 正在等的那一场、倒计时、进度、结论 |
| 「现在」 | 时刻表里的「现在」线、顶栏时钟 | 已开放的在线上，未开放的在线下 |

### 布局与视觉要点（仍有效的视觉事实）

- 默认窗口 `local/app.py:310`「width=1120, height=800, min_size=(980, 700)」，网页视口约 1120×768；
  顶栏 56 / 大屏 212（窄 190）/ 时刻表占剩余高度 / 运行记录收起 38。
- 大屏只在等开放转红前的最后阶段用红、成功用绿；琥珀色只属于「正在等的那一场」。
  色彩、字号（微软雅黑 UI + Bahnschrift 时刻数字）、8px 栅格等完整规范见
  [prototype/components.html](prototype/components.html)。
- 每秒只更新倒计时文字，不重排整表；唯一的刻意动效是点「开始」时点阵点亮。
- 三个源自基线代码的时序常量，现行实现沿用同值：
  开放前 `local/grab.py:57`「POLL_LEAD = 5.0」秒开始盯按钮；
  按钮未出现时刷新间隔从 `local/grab.py:645`「reload_gap = 2.0」秒起退避到最多 8 秒；
  本机时钟与学校相差 `local/grab.py:588`「if skew is not None and abs(skew) > 3:」秒以上才提醒；
  旧登录寿命估算常量 `local/app.py:32`「TOKEN_LIFETIME_H = 17.5」（现行口径为「实测约 10–17 小时，仅提醒」，
  见 [决策 006](decisions/006-session-persistence.md)）。

### 已被取代的业务边界（不要当现行规则）

| 历史原型说什么 | 现行规则 |
|---|---|
| 「大屏一次只亮一场」「只对一个活动动作，不批量」 | 多活动计划：多选、跨批次等待、同批非冲突并发（[决策 004](decisions/004-multi-activity-and-conflict.md)） |
| 「同一时间只跑一个子进程」单任务位 | 每场一个 worker，按各自准备时刻独立调度、互不阻塞 |
| 「多选报名计划」曾被评估为越界而否决 | 双模式已是正式产品口径（[决策 002](decisions/002-signup-modes.md)） |
| 「只试到开放时刻 + 120 秒」固定窗口 | 可配置重试策略，`sign_end` 硬上限（[决策 003](decisions/003-retry-policy.md)） |
| 已开放场次 `at` 原样传历史时间 | 已开放一律从现在起算，CLI 同时打「原定开放 / 本次执行」双时间 |
| 单活动 `grab()` 接口对照 | 现行为 `start_plan()` 体系，契约见 [docs/UI_SPEC.md](../docs/UI_SPEC.md) §5 |

### 附：引用校验

```bash
python design/check_refs.py
```

逐条核对本章 `路径:行号`「原文片段」是否原样出现在基线 `a66016a` 的那一行。
新增内容不要再写行号引用——现行代码以 `docs/` 与 `local/` 为准，行号易漂移。
