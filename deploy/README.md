# deploy/ —— 装到服务器的文件

这里的文件不在本机运行，全部对应服务器上的一个固定落点。改动后要手动同步（见根目录 `README.md` 的「部署」一节）。

| 本机文件 | 服务器落点 | 由谁拉起 | 说明 |
|---|---|---|---|
| `server_refresh.js` | `/root/tjise-login/server_refresh.js` | `tjise-login.service`（常驻 daemon） | 每 2h 校验 token 并回写会话；不登录，登录态失效就只记日志 |
| `atomic_json.js` | `/root/tjise-login/atomic_json.js` | 被 `server_refresh.js` require | 原子写 JSON，daemon 与抓取脚本并发读写会话文件时防半截文件 |
| `test_atomic_json.js` | 只在开发时跑 | — | 原子写的行为测试 |
| `tjise-login.service` | `/etc/systemd/system/` | systemd | 续期 daemon 的单元 |
| `auto_login.py` | `/root/tjise-login/auto_login.py` | `tjise-autologin.timer`（每 5 分钟） | 失效时无人值守重登：取验证码 → OCR → 页面提交 → API 校验 → 原子写 `storage_state.json` / `session.json` |
| `tjise-autologin.service` / `.timer` | `/etc/systemd/system/` | systemd | 上面那个脚本的 oneshot + 定时器 |
| `grab_signup.py` | `/root/tjise-login/grab_signup.py` | `scripts/watchlist_runner.py` 用 `systemd-run` 拉起 | 到点进详情页点「立即报名」；结果私聊回报 |
| `set_credentials.py` | `/root/tjise-login/set_credentials.py` | 人工跑一次 | 交互写 `credentials.json`（0600），账号密码只存在这里 |
| `make_template.sh`、`remote_install.sh`、`deploy.sh`、`sync_token.sh` | 本机执行 | — | 打包 / 安装 / 同步脚本，不落服务器 |
| `legacy/` | 历史方案（nginx 反代、旧安装脚本） | — | 新机已不再使用，仅留档 |

## 为什么登录脚本放在这里而不是 `scripts/`

`scripts/` 下的东西跑在 `/root/tjise-activity-watch/`，只负责「取数据、排版、推送」；`deploy/` 下的东西跑在 `/root/tjise-login/`，负责「维持登录态、到点报名」。两边目录不同、依赖不同（后者要 Chromium 和 Tesseract），混在一起容易装错机器。

## 同步方式

```bash
scp deploy/auto_login.py deploy/grab_signup.py deploy/set_credentials.py root@<服务器>:/root/tjise-login/
scp deploy/server_refresh.js deploy/atomic_json.js root@<服务器>:/root/tjise-login/
scp deploy/tjise-autologin.service deploy/tjise-autologin.timer deploy/tjise-login.service root@<服务器>:/etc/systemd/system/
ssh root@<服务器> systemctl daemon-reload
```

`credentials.json` 和 `data/` 永远不随仓库走。
