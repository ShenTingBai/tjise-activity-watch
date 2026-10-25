# 版本与发布口径

> 回答「下一个版本号是什么、什么时候升、资产怎么和版本绑定」。
> 规则来源：2026-10-25 版本线纠正（用户指出 3.x 跳版过快）与
> [决策 008 · 应用生命周期、版本单一来源与发布资产](../decisions/008-lifecycle-version-release.md)。

## 基线与当前候选

| 项 | 值 | 依据 |
|---|---|---|
| 实际已发布基线 | **v1.0.2** | GitHub `releases/latest` 与本地 tag（v1.0.0/1.0.1/1.0.2）实查 |
| 当前待发布候选 | **v1.1.0** | 基于已发布 1.0.2 的累计功能增强（minor） |
| 内部 3.x | **未发布草案编号，不构成对外版本历史** | 曾把 bootstrap 写死的内部 3.0.0 当对外基线，规划假设有误，已纠正 |

从未发布过 2.x/3.x；文档与 CHANGELOG 不得编造这段历史。

## 升版规则（语义化，来源唯一：仓库根 `VERSION`）

| 场景 | 动作 |
|---|---|
| 同一候选的多次计划、返修、测试与验收轮次 | **不升版**（开发证据用 commit/时间/哈希区分，不用版本号区分） |
| 正式发布后的 bug 修复 | patch +1（1.1.0 → 1.1.1） |
| 累计功能增强的下一批 | minor +1（1.1.0 → 1.2.0） |
| 需要用户迁移的兼容性破坏 | 才评估 major（谨慎，先回 decisions 记录） |

- `VERSION` 是当前软件版本唯一来源：GUI、`build_exe.py`、`make_portable.py`、`tools/release.py`
  都读它；**禁止**改文件名冒充新版本构建，禁止绕过它自定版本。
- Release tag 只能由 `VERSION` 推导（`v` + 版本）；新 tag 用 `git tag -a <tag> <已核验完整提交>`
  **显式绑定**已核验提交，不靠执行当时的 HEAD；origin 同名 tag 用只读 `git ls-remote` 独立核验
  最终提交（附注 tag 取 peeled 值优先；查询失败按错误拒绝，不当成不存在）。**ls-remote 必须同时
  请求 `<tag>` 与 `<tag>{}` 两个 pattern**（EXTRA-1：只查精确 pattern 拿不到附注 tag 的 peeled 行，
  会被对象 SHA 误判指向不符）。本地或远端指向
  不一致都在写入前拒绝、不 force 改写；远端已正确则复用，远端缺失且本地正确才 push，两边都缺
  才创建并 push，push 后再查一次远端落地。`gh release create --verify-tag` 禁止 gh 自动补 tag。
- 发布前逐项核对 manifest/版本/tag/来源一致（见决策 008 的资产绑定规则）；**人工确认之后、
  第一次 Git 写入之前**必须复核「同一干净 main、同一完整 HEAD、同一 VERSION、同一批资产指纹」
  ——确认等待期间任何变化都中止并要求重新确认；上传前再比对一次指纹。
- 引导内容版本 `TOUR_VERSION` 单独管理：软件升版**不**提升它，只有引导内容实质变化才 +1
  （见 [决策 009](../decisions/009-replayable-product-tour.md)）。

## 交付形态（2026-10-25 起现行，详见决策 010）

- **独立可用单 EXE**：主 EXE 把 grab-worker 作为普通数据资源内置进资源根，拷到空目录即可用；
  `build_exe.py` 不再在 OUT 根目录放置外置 worker。
- **完整 ZIP 四成员**：EXE、README.md、启动说明.txt、LICENSE——同一份独立 EXE 加说明与协议，
  不再携带外置 worker（旧五成员口径被决策 010 取代）；publish 核验「ZIP 内主 EXE 与单独 EXE
  字节一致」。
- **发布验收必须包含实包探针**：在仅含单 EXE 的空目录（cwd 同目录、`TJISE_DATA_DIR` 指临时目录）
  运行 `app.py --runtime-check --report <绝对JSON路径>`，实际由主程序启动内置 `worker --help`
  且两者退出 0；mock 单测与主窗口截图不能替代该证据。
- 版本仍为 **1.1.0**、来源口径不变：正式候选须在用户验收、提交合并后从干净 main 重新构建并
  逐项核验；下述旧预览照旧不能改名复用。

## 历史证据与当前候选的边界

- `dist/release-v3.1.0/` 与 `dist/release-v1.1.0/`（脏预览，manifest `source_dirty=true`）都是
  **旧候选**：前者是草案期产物，后者明确标注来源脏、只能供常用流程人工验收——两者都不能改名
  当正式候选用；正式资产必须在验收、提交合并后从对应干净来源重新构建并逐项核验。
- 截图等交付物保留其真实生成版本口径（见 [current-ui/README](../current-ui/README.md)），
  软件候选收敛到 1.1.0 不改写旧证据的生成事实。
