# 交互规范（specs/）

> specs 回答「长什么样、怎么反应」；decisions 回答「为什么这样定」；docs 回答「字段是什么、怎么验收」。
> 三层各管一段，规则本体只在 decisions / docs，specs 不复制第二套。

| 目录 | 回答的问题 | 例子 |
|---|---|---|
| [../decisions/](../decisions/) | 为什么这样定 | 为什么只有 4001 才要求重登 |
| **specs/**（本目录） | 组件、状态、文案、响应式行为 | 按钮在什么状态下显示什么字 |
| `docs/UI_SPEC.md` 等 | 技术字段与验收标准 | `check` 事件的 `result` 四态取值 |

## 目录

| 规范 | 覆盖 |
|---|---|
| [copywriting.md](copywriting.md) | 全界面短文案规则与现行用词表 |

## 写作约定

1. specs 引用现行 `local/ui/` 实现与 `design/prototype/` 视觉原型，不复制全部代码；
   视觉细节（色彩、字号、点阵）以 [prototype/components.html](../prototype/components.html) 为活样张。
2. 规范描述的是**现行**行为；新方案的视觉探索放 [../proposals/](../proposals/)。
3. 改交互不改规则时只动 specs；动规则必须先回 decisions 记录，再同步 specs。
