# 预报名与到点抢报名

## 1. 为什么做成「清单 + 每分钟对齐」

一次性 `systemd-run --on-calendar` 定时器在**报名前很久**就被创建时，页面结构、活动时间、
甚至 token 都可能已经变了，而且用户无法在中间反悔。改成清单后：点按钮 → 写入清单 →
每分钟的 runner 在临近开放时才发现"该动了"，中途点「取消预报名」立即生效。

## 2. 链路

```
07:00 run.py（群里推完日报后）
   └─► 私聊卡片：每条待开放活动右侧一个「预报名」按钮（value = "g:" + base64url(JSON)）
          │  用户点按钮
          ▼
        bot.js handleGrabAction
          ├─ 校验 operator.open_id == PRIVATE_OWNER_OPEN_ID
          ├─ 校验 chatId == owner 的 p2p chat_id
          ├─ "g:" → watchlist.py add   "u:" → watchlist.py remove
          └─ 回调响应直接带新卡片（按钮变「取消预报名」）
                    │
                    ▼
        watchlist_runner.py（每分钟，flock 防重叠）
          ├─ now ≥ open_at − lead − 20s 且未超窗口 → systemd-run 拉起 grab_signup.py
          ├─ 运行中但 unit 已退出 → 读退出码标 done/failed
          └─ 超过 open_at + window + 300s → 标 missed
                    │
                    ▼
        grab_signup.py：点「立即报名」→ 弹窗「确定」→ 读 /rjyHdCyr/signUp 的 code
                    └─► 私聊卡片回报结果
```

## 3. 状态机（`data/watchlist.json`）

| 状态 | 含义 | 谁写 |
|------|------|------|
| `queued` | 已点按钮，等开放 | bot / CLI |
| `running` | runner 已拉起抢报名进程 | runner |
| `done` | 报名成功（exit 0） | runner |
| `failed` | 抢报名失败（非 0 退出） | runner |
| `missed` | 过了允许窗口还没跑成 | runner |

`ACTIVE = ("queued", "running")`——只有这两种状态会出现在卡片的"已排队"里。

命令行：

```bash
python3 scripts/watchlist.py list
python3 scripts/watchlist.py add --id 2ae75dd103654543b1a8b97053adf4a6 --name "活动名" --at "2026-10-30 12:00:00"
python3 scripts/watchlist.py remove --id 2ae75dd103654543b1a8b97053adf4a6
```

`add` 对同一个 id 幂等（重复 add 覆盖，不会出现两条）。

## 4. 抢报名时间线（`grab_signup.py`）

```
open_at − lead(默认 100s)   打开详情页
open_at − 5s                进入轮询：每 300ms 找「立即报名」按钮
                            （按钮还没出现时，最多每 2s reload 一次——一次 reload 打两个接口，不能更密）
open_at ± 0                 点按钮 → 等弹窗 → 点「确定」→ expect_response(/rjyHdCyr/signUp/)
失败                        每 1.5s 重试，直到 open_at + window(默认 120s)
结束                        getJoinInfo 复核（data 非 null 即已报名），私聊回报
```

`lead=100s` 是踩坑值：早期用 35s，headless Chromium 冷启动 + 页面加载花了 36s，
开放后 2s 才进页面、17s 才提交（150 人的场次差点满）。现在提前 100s 开页，留足冷启动时间。

## 5. 卡片快照（`data/grab_card.json`）

飞书卡片一旦发出，服务端不保留内容，要刷新只能拿 `message_id` 去 PATCH。所以发卡时把
`{message_id, sent_at, items}` 存成快照，`grab_card.py` 据此重建整张卡：

- 点击按钮 → 回调响应里**直接**带新卡片（不额外 PATCH，否则飞书客户端会用旧卡片盖回去）；
- runner 抢完 → `grab_card.refresh()` 用快照 + 当前清单 PATCH 一次；
- 已报名（`bmzt == 1`）、报名已截止、活动已开始的条目在重建时自动剔除。

## 6. 边界

- **只服务本人账号**：不代他人报名、不做多账号批量抢占；`watchlist` 里的条目只能由 owner 在私聊里点出来。
- 预报名卡片**只发 owner 私聊**；群里永远不出现个人化信息。
- 群消息里的活动列表是所有人的公共视角（不按"我是否报名"过滤），私聊卡片才做个人过滤。
