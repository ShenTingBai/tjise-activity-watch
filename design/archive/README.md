# 历史原型档案（archive 视角）

> 本页说明 `design/prototype/` 与 `design/shots/` 两份历史资产的定位。
> 它们**不是现行业务规则的来源**——当前规则一律跳转
> [design/decisions/](../decisions/)（产品决策）与 `docs/`（工程契约）。

## 这两份资产是什么

| 资产 | 内容 | 时间 |
|---|---|---|
| [prototype/](../prototype/) | 2026-10-22 的高保真交互原型：总览 `index.html`、可操作原型 `play.html`、组件规范 `components.html`、以及只给原型用的假后端 | 基线提交 `main@a66016a` |
| [shots/](../prototype/proto/shoot.py)（目录 [shots/](../shots/)） | 27 张原型状态截图（`board/` 下是大屏特写） | 同上 |

## 为什么保留

1. **视觉来源**：现行界面的候车厅布局、LED 大屏、色彩字体体系全部脱胎于这套原型，
   以后改视觉时拿它对照「当初为什么长这样」。
2. **活样张**：`prototype/components.html` 用界面自己的 CSS 渲染组件，仍是视觉规范的活参考。
3. **可复现**：`python design/prototype/proto/shoot.py` 能重截全部状态图，不依赖运行时数据。

## 边界（重要）

- 历史原型是**单活动状态机**：一次只等一场、无多选、固定 120 秒重试、单任务位——
  这些业务边界**已被多活动计划、双报名模式、可配置重试取代**，不再作为当前规则。
- 原型里的假后端（`proto/mock-backend.js`）模拟的是基线代码行为，与现行 `app.py` / `grab.py` /
  `signup_plan.py` 的事件契约已不完全一致；对照接口请看 `docs/UI_SPEC.md` §5。
- 原型继续用「准点」这个历史视觉名——它是设计稿的称呼，不是产品名（见
  [决策 001](../decisions/001-product-naming.md)）。

## 怎么用

- 想看视觉：双击 `prototype/index.html`（总览）或 `prototype/play.html`（交互原型）。
- 想核对历史代码引用：`python design/check_refs.py`（校验 `design/README.md` 历史基线章节里的
  `路径:行号`「原文」引用是否仍在基线 `a66016a` 上对得上）。
- 想改业务规则：**不要改原型**——先在 [decisions/](../decisions/) 立记录，再动 `local/` 代码。
