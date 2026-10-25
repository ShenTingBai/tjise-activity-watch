# 登录态机制

站点是 uni-app 单页应用，登录成功后 token 存在 `localStorage.xgUniToken`，后续接口都带 `token` 头。
浏览器（Playwright）只在**登录**这一步出场，抓取全走纯 HTTP。

## 0. 本机版（local/）与服务器版的登录态文件对照

| 运行形态 | 登录态目录 | 说明 |
|------|------|------|
| 本机版开发/绿色版（bat、python） | `local/data/`（绿色版为便携目录 `data/`） | 与历史行为一致，关掉重开直接复用 |
| 本机版 EXE（onedir/onefile） | `%LOCALAPPDATA%\TianSoftActivityPreRegistration\data` | 同一台机器共享：关闭重开、替换或重建 EXE 后仍复用；旧版 EXE 旁 `data/` 的文件在首次启动时自动原子迁移（只补缺失，不覆盖新文件） |
| 环境变量覆盖 | `TJISE_DATA_DIR` | 开发与 EXE 版都认；相对路径按当前工作目录 resolve |
| 服务器版 | `/root/tjise-activity-watch/data` + `/root/tjise-login/` | 见下文守护与重登章节 |

本机版的登录有效性**以界面真实校验为准**：启动先显示「正在检查登录态」，由
`grab.py check_token` 真实访问学校列表接口——成功显示「登录有效 / 已复用本机
登录态」，返回 4001 显示「登录态失效 / 重新登录」，网络异常显示「暂时无法确认
登录态」并可重试（不会清掉已有会话）。`saved_at` 只用于「约剩 N 小时」提醒，
不作为放行或拦截依据。会话文件里只有 cookie/token，**不含账号密码**。

### 应用生命周期与登录估算（本机版）

- 生命周期元数据（`last_started_at` / `last_heartbeat_at` / `last_closed_at` / `run_open`）
  只写在 `ui.json`，**不写进 session.json / storage_state.json**；登录凭证文件永远只存会话信息。
- 运行周期用 `run_open` 标记区分：启动的同一原子写入里记「本次开始 + 首个心跳 +
  `run_open=true`」，正常关闭原子写「关闭时刻 + `run_open=false`」；运行中每分钟写一次心跳，
  停止后的迟到心跳会被拦下（关闭记录不被覆写）。重开时显示「上次关闭后离线 N
  （从上次关闭到本次启动）」：上一周期正常关闭是精确值；上一周期没走正常关闭
  （崩溃/断电/强杀，或刚启动几秒就崩）只有存活时刻可依，显示「约」——**绝不拿更早
  周期的关闭时间冒充本周期正常退出**；首次运行、时间对不上或系统时钟回拨显示「未知」。
- 登录剩余时间按 `saved_at + 约 17.5 小时` 的**绝对墙钟**估算——工具关着的这段时间
  照常计算，不需要后台进程；启动、每次「刷新」和重新登录后界面都拿**同一份锚点**，
  剩余显示随每秒走字递减。窗口内重新登录成功后旧锚点立即作废，等新校验带新锚点回来，
  不会出现胶囊和浮层各说各话；反复「刷新」同一凭证不会把估算续期。估算到点但学校
  校验仍返回有效时，界面继续显示「登录有效」，估算只降级为提醒文案；**任何情况下
  不得因旧估算强制过期、清理会话或刷新 `saved_at`**。锚点缺失（会话没存 `saved_at`
  等情况）显示「未知（以学校校验为准）」，不编造 17 小时。

## 1. 寿命

实测样本（单次观测，2026-10-21）：`00:09:50` 签发 → `17:30:01` 仍 ok → `17:45:01` 转 fail，
即 **约 17.5 小时**。历史文档里写的「3 小时」是早期小样本，已作废。

结论：不要写死寿命，**服务器版以 `data/token_probe.log` 为准**（每 15 分钟一行，能定位到 15 分钟窗口）；
本机版没有探针，以界面每次「刷新」的真实校验结果为准。
`token_report.py` 会把每个指纹的存活时间汇总到 `reports/token-lifetime.md`。

## 2. 两条续期路径

```
                       ┌─ 每 2 小时：server_refresh.js 校验 → 成功就回写会话文件（延长 saved_at）
登录态 ────────────────┤
                       └─ 失效时：auto_login.py 无人值守重登（验证码 OCR）
```

### 2.1 守护进程（`tjise-login.service`）

`server/deploy/server_refresh.js --daemon`：

- 每 2 小时用当前 token 调一次 `listActivity` 校验；
- 校验通过 → 原子回写 `storage_state.json` 与 `data/session.json`（`saved_at` 刷新）；
- 校验失败（4001）→ 只记日志，**不自己登录**，交给 `auto_login.py`；
- `storage_state.json` 被替换（mtime 变化）会被感知并重载，所以手工传新 state 后不必重启守护。

### 2.2 无人值守重登（`auto_login.py` + `tjise-autologin.timer`）

触发判定（每 5 分钟跑一次，绝大多数时候什么都不做）：

| 条件 | 动作 |
|------|------|
| `token_probe.log` 最后一行 `ok` 且 token 首次见到未满 8h | 跳过（零请求） |
| 最后一行 `fail` / `no-session` | 触发登录 |
| 最后一行 `http-error` / `bad-json` | 判定「网络问题」，现查一次接口再决定，不盲试密码 |
| token 用满 8h（即使探针仍 ok） | `--preempt` 主动换新，避开失效悬崖 |

> 为什么要「探测不到就跳过」：2026-10-22 早间 ECS 断网，探针全是 `http-error`，
> 旧逻辑把它当成「登录态失效」，白试了 3 次密码。现在网络类失败一律不试密码。

登录流程：

1. Playwright **headless** 打开 `https://xg.tjise.edu.cn/stu/#/mainPages/login?userType=2`；
2. 拦截页面自己发出的 `/dr/api/auth/captcha` 响应，拿到验证码图片（200×44 PNG）；
3. OCR 识别 4 位字符（见下节），填入账号/密码/验证码，勾选隐私协议，点「立即登录」；
4. 成功 → 读 `localStorage.xgUniToken` → 调 `listActivity` 复核 → 原子写
   `storage_state.json`（Playwright 完整 state）与 `data/session.json`
   （`{token, saved_at, name, student_no, gender}`）；
5. 失败（验证码错 / 密码错）→ 重载换一张验证码，最多 **5 次**、两次之间隔 **30 秒**；
   仍失败私聊告警，30 分钟内不重复告警（5 分钟后下一轮继续试，不会一次运气不好就断整晚）。

日志只写结论（`token fp=xxxxxxxx`、`saved_at`、耗时、错误类型），**不写**账号、密码、验证码、token 全文、姓名。

## 3. 验证码 OCR

服务器：`ddddocr 1.6.1`（主）+ `tesseract 4.1.1`（兜底）+ `pillow` + `numpy`。
ddddocr 是可选依赖（`pip install ddddocr`），没装时整条链路自动退回纯 Tesseract。

`auto_login.py::recognize` 的取值顺序：

1. ddddocr `beta=True`（`common.onnx`）：20 张标注样本 19/20，要求输出正好 4 位；
2. 长度不对时换默认模型（`common_old.onnx`）：16/20，同样要求 4 位；
3. 再不行走 Tesseract：饱和度为 0 的灰度（`255 - (max-min)`）→ 干扰线是 1~2px 斜线，
   按列做游程过滤（`minrun=4` 且 `sat>55` 才保留，线被吃掉、粗笔画还在）→ 放大 6 倍、
   `psm 6/7/8`、`oem 1`、白名单 `0-9A-Z` → 7 个变体逐位投票（14/20），凑不齐就换验证码。

为什么必须卡长度：干扰圆环常被认成 `O`（`M483` 读成 `M4083`）、也会漏字（`DPJS` 读成
`DJS`），多于 4 位或少于 4 位一律不信，交给下一级重读。三源串联在同一批样本上 20/20，
`--dry-run` 实测 30/30 都能凑齐。

别把 `set_ranges()` 当字符集过滤器：1.6.1 上它会截断结果（实测输出 `N6`、`2`、`-`），
字符集清洗在 `_clean_code()` 里自己做。

单次登录耗时约 5s。`--dry-run N` 会打印融合与单引擎的「凑齐 4 位」比例——凑齐 ≠ 认对，
正确率只能用人工标注样本算。按单次 95% 算，5 次重试（间隔 30 秒）后仍失败的概率约 `0.05⁵`。

失败过的路线（别再走）：形态学开运算（笔画只有 3~4px，直接毁字）、按颜色抠线（3/20）、
渲染字体模板匹配（4~6/20）、真实字形 kNN（留一法 1/17）。这些脚本留在 `server/scripts/dev/` 与服务器的 `probe/` 里备查。

## 4. 凭据管理

| 文件 | 内容 | 权限 |
|------|------|------|
| `/root/tjise-login/credentials.json` | `{"username": "...", "password": "..."}` | 600，root |
| `/root/tjise-login/storage_state.json` | Playwright 登录态（cookies + localStorage） | 600 |
| `/root/tjise-activity-watch/data/session.json` | `{token, saved_at, name, student_no, gender}` | 600 |

更新账号密码用交互脚本（直接 `python3 - <<'PY'` 会让 `input()` 读到脚本自身，报 `EOFError`）：

```bash
ssh root@<你的服务器IP>
cd /root/tjise-login && python3 set_credentials.py
```

## 5. 失效了怎么办（人工兜底）

自动重登连续失败时，走人工：

1. 本机 `python server/scripts/login.py`，在弹出的 Chrome 里登录一次；
2. 导出 `data/storage_state.json`（扁平 `{"xgUniToken": "...", ...}` 或 Playwright 完整 state 都支持）；
3. `scp` 到服务器 `/root/tjise-login/`，权限 600；
4. `cd /root/tjise-login && node server_refresh.js`（单跑一次，立刻校验并回写会话）；
5. `cd /root/tjise-activity-watch && python3 scripts/fetch.py` 确认退出码 0。

## 6. 监控口径

- `token_probe.py`：15 分钟一次，只打接口（不带页面）。连续 2 次 `fail` 才告警，`ok` 清零。
- `net_probe.py`：5 分钟一次，只 GET 静态页，区分「整机断网」与「仅学校站点不通」。
- 两个探针互不依赖：token 探针失败时，先看 `net_probe.log` 排除网络问题，再看 `auto_login.log`。
