# 产品决策记录（decisions/）

> 这里只记录**已经敲定的产品规则**：为什么这样定、用户能看到什么。
> 字段定义、接口载荷和验收标准在 `docs/`（工程真源），本目录不复制第二套契约，
> 只在每份记录末尾链接对应的技术文档。

## 目录

| 编号 | 决策 | 状态 |
|---|---|---|
| [001](001-product-naming.md) | 产品命名：天软活动预报名工具 | implemented |
| [002](002-signup-modes.md) | 双报名模式与已截止（预报名 / 立即报名·抢名额 / 已截止） | implemented |
| [003](003-retry-policy.md) | 可配置重试策略与报名截止硬上限 | implemented |
| [004](004-multi-activity-and-conflict.md) | 多活动计划与举办时段冲突检查 | implemented |
| [005](005-attendance-filters.md) | 签到 / 签退三态筛选 | implemented |
| [006](006-session-persistence.md) | 登录态持久化与启动校验 | implemented |
| [007](007-runtime-timeline.md) | 运行时间线：真实事件 + 原定参考 + 统一截止 | implemented（真机验收待用户） |
| [008](008-lifecycle-version-release.md) | 应用生命周期、版本单一来源与 EXE/ZIP 发布资产 | pending（实现待验收） |
| [009](009-replayable-product-tour.md) | 六步可重播功能引导（说明/更多两处重播，内容版本 2） | pending（实现待验收；现行入口见 [../specs/assistance-panels.md](../specs/assistance-panels.md)） |
| [010](010-standalone-exe-and-settings.md) | 独立可用单 EXE 与辅助入口 | pending（源码已落地，运行验证/实包验收待授权；入口名称与视觉已被 2026-10-25 返修取代，见 [../specs/assistance-panels.md](../specs/assistance-panels.md)） |

> 008/009/010 的产品规则已确认（confirmed）；实现分轮落地、均尚无有效登录真机验收证据，
> 按状态口径标 pending。规则确认与实现验收分列，
> 不把「accepted 的提案」或「代码已写」当成「用户验收通过」。
> 010 部分取代 008 的旧资产口径（外置 worker 定位与五成员 ZIP 已标历史）。
> 2026-10-25 返修轮只取代 010 的**辅助入口名称与视觉**（设置 → 更多），资产形态与组件闭环不变。

## 每份记录必须包含

1. **状态**：confirmed / implemented / pending / proposed（口径见下）
2. **日期**：规则敲定的时间
3. **问题**：这个决策回答什么问题
4. **用户确认的规则**：定下来的规则本身
5. **用户可见行为**：界面上实际表现成什么样
6. **工程契约链接**：`docs/UI_SPEC.md`、`docs/RETRY_POLICY_GUIDE.md` 或 `local/README.md` 的对应章节
7. **已知限制**：当前没做到或故意不做的
8. **变更方式**：以后要改这条规则，需要动哪些地方

## 状态口径

- **confirmed**：产品规则已敲定。**只表示规则定了，不等于代码已经验收通过。**
- **implemented**：confirmed，且当前代码已落地并有回归测试或验收证据。
- **pending**：规则已确认但代码尚未闭合（或只闭合了一部分），记录里必须写清缺什么。
- **proposed**：还没敲定的新想法，不放本目录，放 [../proposals/](../proposals/)。

规则与实现可能短暂脱节：文档先行标 confirmed、代码随后追上是正常节奏；
反过来（代码已实现、规则未记录）不允许——发现时先补记录。
