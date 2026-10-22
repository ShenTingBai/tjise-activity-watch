# WorkBuddy 云工作空间 · 校园活动推送机器人登录态恢复提示词

> 用法：把下面「提示词正文」整段粘到 WorkBuddy 云工作空间的会话里。
> 它在云端负责：拉起登录页 → 你手动登录 → 自动把登录态同步到服务器 → 自动验证。
> 它不负责：自动登录（登录页有图形验证码）、自动续期（站点没有续期接口）。

---

## 提示词正文

```text
# 任务：恢复校园活动推送机器人的登录态

## 背景（不要跳过，这些事实已经验证过，不要重新推翻）
- 目标站点：https://xg.tjise.edu.cn/stu/ ，登录态保存在 localStorage 的 xgUniToken 里。
- 这个站点没有 token 续期接口。token 是登录时长的固定会话，寿命不固定：当前样本约 17.5 小时，
  实际以服务器上 data/token_probe.log 的 15 分钟探测为准，唯一恢复方式是重新登录。
  任何"刷新页面就能续期""存 cookie 就能续期"的假设都已被证伪。
- 登录表单：进入 /stu/ → 点「我是学生」→ 账号 + 密码 + 图形验证码 → 勾选《隐私协议》→「立即登录」。
  图形验证码是图片，你无法自动识别，必须由人完成。
- 服务器：<旧服务器IP>，应用目录 /root/tjise-activity-watch，续期脚本目录 /root/tjise-login。
- 你的职责**不是自动登录**，而是：把登录页拉起来给人看 → 人登录完 → 你自动同步 + 验证。

## 前置检查（缺任何一项就停下来报告，不要猜、不要绕）
1. 本机能启动 chromium（python3 的 playwright 或 node 的 playwright 任一可用）。
2. 本机能 ssh 到 <旧服务器IP>，私钥路径由用户提供。
   云端建议用专用低权限密钥（如 ~/.ssh/develop），不要把本机主密钥 goal.pem 放进云租户。
3. 用户能看到并操作你拉起的浏览器画面（平台的浏览器预览/接管能力）。

## 执行步骤

第 0 步 · 先确认是否真的失效（不要无脑覆盖现有登录态）
  ssh <服务器> "cd /root/tjise-activity-watch && python3 scripts/fetch.py; echo fetch_exit=\$?"
  - 输出里有 SUMMARY_JSON_BEGIN 且 fetch_exit=0 → 当前 token 还有效。
    停下来报告"仍然有效，无需重新登录"，询问用户是否仍要强制换新。不要覆盖。
  - fetch_exit=2（或日志里出现 code 4001、身份验证失败）→ 确认已失效，进入第 1 步。

第 1 步 · 拉起登录页
  用持久化 profile 启动 chromium（有你平台的显示方式就用可视化模式），打开
  https://xg.tjise.edu.cn/stu/
  然后明确告诉用户："窗口已打开，请选『我是学生』，输入账号、密码、图形验证码并登录。"

第 2 步 · 等待登录（最长 15 分钟）
  每 2 秒读一次 localStorage 的 xgUniToken 和 user。
  拿到非空 token 就继续；超时则截图并停下报告，不要自己尝试登录。

第 3 步 · 生成两个文件（注意格式差别，弄错服务器会读不到）
  1) session.json —— 结构：
     {
       "token": "<xgUniToken>",
       "saved_at": "<本地时间 %Y-%m-%d %H:%M:%S>",
       "name": "<user.xm>",
       "student_no": "<user.xh>",
       "gender": "<user.xb>"
     }
  2) storage_state.json —— 必须是**扁平的 localStorage 键值表**，
     形如 {"xgUniToken": "...", "user": "...", "__DC_STAT_UUID": "...", ...}
     不要写成 Playwright 的 {cookies, origins} 结构，服务器上的 server_refresh.js 只认扁平表。

第 4 步 · 上传并安装（先备份，权限 600）
  scp 两个文件到服务器的 /tmp 下；
  在服务器上：
    ts=$(date +%Y%m%d-%H%M)
    cp -a /root/tjise-login/storage_state.json      /root/tjise-login/storage_state.json.bak-$ts
    cp -a /root/tjise-activity-watch/data/session.json /root/tjise-activity-watch/data/session.json.bak-$ts
    install -m 600 /tmp/...storage...json /root/tjise-login/storage_state.json
    install -m 600 /tmp/...session...json /root/tjise-activity-watch/data/session.json

第 5 步 · 服务器验证（两条都要跑，缺一条不算完成）
  cd /root/tjise-login && /usr/bin/node server_refresh.js ; echo refresh_exit=$?
    期望 exit 0，输出形如 [· 无变化] 2026-10-21 00:10:18 或 [✓ 已更新] + 时间。
    注意：输出「无变化」是正常的，页面不会签发新 token。
  cd /root/tjise-activity-watch && python3 scripts/fetch.py ; echo fetch_exit=$?
    期望 exit 0，且输出里有 SUMMARY_JSON_BEGIN。

第 6 步 · 回报（只报这些，不要贴 token）
  - 两个文件的大小、权限、写入时间
  - server_refresh.js 那一行完整输出 + 退出码
  - fetch.py 的 total_in_pool / pending_count / open_count + 退出码
  - session.json 的 saved_at 和 token 长度（应为 32）
  - 本次登录的预计失效时间 ≈ saved_at + 17.5 小时（仅为当前样本的经验值，实际失效时刻以
    data/token_probe.log 为准），并提醒用户下一次需要登录的时点

## 硬约束（违反任何一条都算失败）
- 任何输出、日志、回复里禁止出现：token 的值、姓名、学号、密码。
- 不改四个推送时点（07:00 / 10:30 / 16:30 / 21:30），不改 state.json 的结构。
- 不删 /root/tjise-bot/data/handled_messages.json（那是消息去重记录）。
- 不重启 tjise-bot 服务，除非 fetch.py 成功而机器人仍报错——那种情况先报告再动。
- 不做"补推"：不要为了让漏掉的那一轮立刻补发而手动跑 run.py，除非用户明确同意。
- 同一动作失败最多重试 2 次，超过就停下报告。

## 失败对照表
| 现象 | 含义 | 该做什么 |
|---|---|---|
| fetch exit 0 | token 仍有效 | 停，报告，不覆盖 |
| fetch exit 2 / code 4001 | token 已失效 | 正常路径，继续登录 |
| 15 分钟拿不到 token | 人没登录或页面卡住 | 截图，停下报告 |
| scp 失败 | 网络或密钥问题 | 停下报告，不要改用密码登录 |
| refresh exit 3（token 拿到但接口不认） | 登录没真正成功 | 用备份回滚，重新走第 1 步 |
| fetch 成功但 cron.log 仍报失效 | 时序或缓存问题 | 报告，不要重启服务 |

## 完成后告诉用户的一句话
"本次登录态已于 <saved_at> 写入服务器，预计 <saved_at+3h> 失效；下次失效前后需要再登录一次。"
```

---

## 这份提示词在云端成立的前提

| 前提 | 说明 | 不满足时 |
|---|---|---|
| 云端能显示浏览器画面并接收键盘输入 | 不然后人根本看不到验证码 | 只能退回本机执行 |
| 云端能 ssh 到 <旧服务器IP> | 同步动作依赖 scp/ssh | 改为只生成两个文件，由用户自行上传 |
| 私钥被放进云端 | 这是本次唯一的安全代价 | 建议用单独的低权限密钥，不要用 `goal.pem` 主密钥 |

## 它解决什么、不解决什么

- **解决**：失效后不用记三步命令；窗口在哪台机器上都一样；同步和验证不会漏跑；日志保持干净。
- **不解决**：失效后仍要偶尔人工登录一次（当前样本约 17.5 小时一次，实际以 token_probe 为准，
  一般接近一天一次）。这是站点设计（图形验证码 + 固定会话）决定的，
  换任何平台都一样。
