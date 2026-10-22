# 登录态机制

站点是 uni-app 单页应用，登录成功后 token 存在 `localStorage.xgUniToken`，后续接口都带 `token` 头。
浏览器（Playwright）只在**登录**这一步出场，抓取全走纯 HTTP。

## 1. 寿命

实测样本（单次观测，2026-10-21）：`00:09:50` 签发 → `17:30:01` 仍 ok → `17:45:01` 转 fail，
即 **约 17.5 小时**。历史文档里写的「3 小时」是早期小样本，已作废。

结论：不要写死寿命，**以 `data/token_probe.log` 为准**（每 15 分钟一行，能定位到 15 分钟窗口）。
`token_report.py` 会把每个指纹的存活时间汇总到 `reports/token-lifetime.md`。

## 2. 两条续期路径

```
                       ┌─ 每 2 小时：server_refresh.js 校验 → 成功就回写会话文件（延长 saved_at）
登录态 ────────────────┤
                       └─ 失效时：auto_login.py 无人值守重登（验证码 OCR）
```

### 2.1 守护进程（`tjise-login.service`）

`deploy/server_refresh.js --daemon`：

- 每 2 小时用当前 token 调一次 `listActivity` 校验；
- 校验通过 → 原子回写 `storage_state.json` 与 `data/session.json`（`saved_at` 刷新）；
- 校验失败（4001）→ 只记日志，**不自己登录**，交给 `auto_login.py`；
- `storage_state.json` 被替换（mtime 变化）会被感知并重载，所以手工传新 state 后不必重启守护。

### 2.2 无人值守重登（`auto_login.py` + `tjise-autologin.timer`）

触发判定（每 5 分钟跑一次，绝大多数时候什么都不做）：

| 条件 | 动作 |
|------|------|
| `token_probe.log` 最后一行 `ok` 且 `saved_at` 未超 16h | 跳过（零请求） |
| 最后一行 `fail` / `no-session` | 触发登录 |
| 最后一行 `http-error` / `bad-json` | 判定「网络问题」，现查一次接口再决定，不盲试密码 |
| `saved_at` 超过 16h（即使探针仍 ok） | `--preempt` 主动换新，避开 17.5h 的悬崖 |

> 为什么要「探测不到就跳过」：2026-10-22 早间 ECS 断网，探针全是 `http-error`，
> 旧逻辑把它当成「登录态失效」，白试了 3 次密码。现在网络类失败一律不试密码。

登录流程：

1. Playwright **headless** 打开 `https://xg.tjise.edu.cn/stu/#/mainPages/login?userType=2`；
2. 拦截页面自己发出的 `/dr/api/auth/captcha` 响应，拿到验证码图片（200×44 PNG）；
3. OCR 识别 4 位字符（见下节），填入账号/密码/验证码，勾选隐私协议，点「立即登录」；
4. 成功 → 读 `localStorage.xgUniToken` → 调 `listActivity` 复核 → 原子写
   `storage_state.json`（Playwright 完整 state）与 `data/session.json`
   （`{token, saved_at, name, student_no, gender}`）；
5. 失败（验证码错 / 密码错）→ 重载换一张验证码，最多 **3 次**；仍失败私聊告警，6 小时内不再试密码。

日志只写结论（`token fp=xxxxxxxx`、`saved_at`、耗时、错误类型），**不写**账号、密码、验证码、token 全文、姓名。

## 3. 验证码 OCR

服务器：`tesseract 4.1.1` + `pillow` + `numpy`。

预处理（`auto_login.py::recognize`）：

1. 饱和度为 0 的灰度（`255 - (max-min)`）——验证码字符是高饱和彩色，背景与字符描边接近白色；
2. 干扰线是 1~2px 斜线，按列做游程过滤，`minrun=4` 且 `sat>55` 才保留（线被吃掉，粗笔画还在）；
3. 放大 6 倍、`psm 7/6`、`oem 1`、白名单 `0-9A-Z`；
4. 7 个预处理变体（sat/lum × 线过滤 × 缩放 × psm）逐位投票，凑不齐 4 位就换验证码重试。

实测识别率：**单张约 60%**（20 张人工标注样本，12 张全对），单次登录耗时约 5.5s。
按 3 次重试算，一次自动登录的成功率约 `1 - 0.4³ ≈ 93.6%`，剩下的交给告警和人工兜底。

失败过的路线（别再走）：形态学开运算（笔画只有 3~4px，直接毁字）、按颜色抠线（3/20）、
渲染字体模板匹配（4~6/20）、真实字形 kNN（留一法 1/17）。这些脚本留在 `scripts/dev/` 与服务器的 `probe/` 里备查。

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

1. 本机 `python scripts/login.py`，在弹出的 Chrome 里登录一次；
2. 导出 `data/storage_state.json`（扁平 `{"xgUniToken": "...", ...}` 或 Playwright 完整 state 都支持）；
3. `scp` 到服务器 `/root/tjise-login/`，权限 600；
4. `cd /root/tjise-login && node server_refresh.js`（单跑一次，立刻校验并回写会话）；
5. `cd /root/tjise-activity-watch && python3 scripts/fetch.py` 确认退出码 0。

## 6. 监控口径

- `token_probe.py`：15 分钟一次，只打接口（不带页面）。连续 2 次 `fail` 才告警，`ok` 清零。
- `net_probe.py`：5 分钟一次，只 GET 静态页，区分「整机断网」与「仅学校站点不通」。
- 两个探针互不依赖：token 探针失败时，先看 `net_probe.log` 排除网络问题，再看 `auto_login.log`。
