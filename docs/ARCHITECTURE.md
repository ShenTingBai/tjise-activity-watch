# 架构与数据流

服务器上一共三个 systemd 服务、一个 timer、九条 cron。所有脚本都跑在 `/root/tjise-activity-watch`，
只有登录态文件放在 `/root/tjise-login`，机器人跑在 `/root/tjise-bot`。

## 数据流

```
                 ┌──────────────────────────── cron ────────────────────────────┐
                 │ 07:00/10:30/16:30/21:30 run.py   */5 scan.py  */15 token_probe │
                 └───────────────┬──────────────────────────┬───────────────────┘
                                 │                          │
        ┌────────────────────────▼───────────┐   ┌──────────▼──────────┐
        │ data/session.json (token, 600)     │◄──┤ tjise-login daemon  │  每 2h 校验
        └────────────────────────┬───────────┘   │ server_refresh.js   │  storage_state.json
                                 │               └──────────┬──────────┘
                     fetch.py ───┤                          │ 失效时
                                 ▼                          ▼
             POST /dr/api/rjyHd/listActivity        auto_login.py（验证码 OCR，≤3 次）
                                 │                          │
                    ┌────────────┴────────────┐             │
                    │ 数据 + 去重（state.json）│             │
                    └────────────┬────────────┘             │
                                 ▼                          │
            run.py ──► notify.py ──► 飞书群卡片 / owner 私聊卡片
                                 │
            scan.py（报名冲刺 / 晚归签到）┘
                                 │
            bot.js（长连接）── @查询 / 私聊按钮 ──► watchlist.py ──► watchlist_runner.py
                                                                          │
                                                            到点 systemd-run grab_signup.py
                                                                          │
                                                                    结果私聊回报
```

## 模块职责

| 文件 | 职责 | 谁调用 |
|------|------|--------|
| `scripts/common.py` | 学校地址唯一来源、会话读写、原子写 | 所有脚本 |
| `scripts/fetch.py` | 取 token → 分页拉活动 → 归一化 → 去重标记 | run/scan/remind/probe |
| `scripts/run.py` | 一轮推送：抓取 → 卡片 → 群推送 → 私聊预报名卡 | cron |
| `scripts/scan.py` | 每 5 分钟：报名冲刺、晚归签到提醒 | cron |
| `scripts/remind.py` | 07:05 当天活动私聊提醒 | cron |
| `scripts/card.py` | 飞书卡片 JSON 构造（含按钮 value 编码） | run/scan/remind/bot |
| `scripts/render.py` | 文案排版（纯文本与卡片共用） | card/run/scan |
| `scripts/notify.py` | 推送通道：webhook / 飞书应用、卡片、私聊、更新卡片 | 全部发送方 |
| `scripts/token_probe.py` | token 有效性探针 + 连续失败告警 | cron |
| `scripts/token_report.py` | token 寿命汇总 | cron |
| `scripts/net_probe.py` | 出网自检（静态页，不带 token） | cron |
| `scripts/grab_card.py` | 预报名卡片的发送快照与整卡刷新 | run / bot / watchlist |
| `scripts/watchlist.py` | 预报名清单状态机（queued/running/done/failed/missed） | bot / runner |
| `scripts/watchlist_runner.py` | 每分钟对齐清单，到点 `systemd-run` 拉起抓取 | cron |
| `scripts/trigger_server.py` | HTTP 一键触发（`/run/<token>`） | 手机浏览器 |
| `deploy/server_refresh.js` | 常驻守护：每 2h 校验并回写会话 | systemd |
| `deploy/auto_login.py` | 失效时无人值守重登：验证码 OCR + 页面提交 + 原子写会话 | timer（5 分钟） |
| `deploy/grab_signup.py` | 到点进详情页点「立即报名」，结果私聊回报 | watchlist_runner |
| `deploy/set_credentials.py` | 交互写入 `credentials.json`（唯一放账号密码的地方） | 人工一次 |
| `bot/bot.js` | 飞书长连接：@ 查询、私聊按钮回调 | systemd |

## 接口清单

| 接口 | 方法 | 用途 |
|------|------|------|
| `/dr/api/rjyHd/listActivity` | POST | 活动列表（分页，`pageSize` 最大 500） |
| `/dr/api/rjyHd/getInfo/<id>` | GET | 活动详情 |
| `/dr/api/rjyHdCyr/getJoinInfo/<id>` | POST | 我是否报名（`data` 非 null 即已报） |
| `/dr/api/rjyHdCyr/signUp/<hash>` | GET | 报名（hash 由前端计算，必须走页面点击） |
| `/dr/api/auth/captcha` | POST | 登录验证码（`{client,userType}` → `{key,image}`） |
| `/dr/api/auth/login` | POST | 登录（带签名头，只能用真实页面提交） |

失效响应统一是 `{"code":4001,"message":"身份验证失败！"}`，`fetch.py` 见到即抛 `PermissionError`、退出码 2。

## 去重机制

`data/state.json` 里三份状态，各有明确语义：

| 键 | 语义 | 写入时机 |
|----|------|----------|
| `announced` | 已推送过的活动（id → 首次推送时间） | **推送成功后**才写 |
| `last_push_ts` | 上一次成功推送的时间戳 | 同上 |
| `reminded_signup` | 已提醒过「报名开始」的活动 | 提醒成功后 |

`run.py` 用 `data/push.lock`（`LOCK_NONBLOCK`）在锁内先占位、推送成功再落盘；
进程崩溃或推送失败会回滚占位，所以「失败」不会被误记成「已推送」。
`GUARD_MINUTES=60` 防同一时点重复跑。

## 推送节奏

| 时间 | 内容 | 通道 |
|------|------|------|
| 07:00 | 今日/待开放活动日报 + 私聊预报名卡 | 群 + owner 私聊 |
| 07:05 | 「今天要参加的活动」 | 仅 owner 私聊 |
| 10:30 / 16:30 | 增量日报 | 群 |
| 21:30 | 晚归签到提醒 + 已开放清单 | 群 |
| 每 5 分钟 | 报名冲刺（≤10 分钟开放的活动） | 群 |
| 每 15 分钟 | token 探针（不推送，仅告警） | 私聊 |

请求量账本见 [TIMING_AND_DEDUP_ANALYSIS.md](TIMING_AND_DEDUP_ANALYSIS.md) §3.1。

## 告警链路

| 探针 | 频率 | 判定 | 文案 |
|------|------|------|------|
| `token_probe.py` | 15 分钟 | 连续 2 次 `fail`（`ok` 清零） | 登录态失效，需要重新登录 |
| `token_probe.py` | 15 分钟 | 连续 2 次 `http-error`/`bad-json` | 学校接口探测不通，多半是出网/带宽问题 |
| `net_probe.py` | 5 分钟 | 连续 2 次取不到学校静态页 | 区分「整机断网」与「仅学校站点不通」 |
| `auto_login.py` | 5 分钟（timer） | 3 次登录失败 | 自动登录失败，需人工介入 |

告警一律优先私聊 owner，失败回落群通道；同一失效周期只发一次，恢复后自动清零。
