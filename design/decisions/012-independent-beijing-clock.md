# 决策 012：独立北京时间贯穿界面、计划、worker 与登录估算

状态：confirmed（用户确认）/ implemented / verified（隔离验收 2026-10-26 返修轮 + 最终边界轮）/ real-school pending
日期：2026-10-26（最终边界轮同日更新）
关联：006（登录态持久化）、007（运行时间线）、008（生命周期与发布）

## 用户确认的规则（已实现）

1. **本机时间不权威**：用户的电脑时间不可作为业务依据，本工具不读取系统
   墙钟作为业务时刻，也不修改系统时间/时区/授时服务。业务时刻 = 网络授时锚点
   （epoch）+ 应用内经过时长（elapsed）。
2. **单一时钟**：界面、协调器、worker、父层监督共用同一 owner/generation 的
   业务时钟；worker 经 `TJISE_CLOCK_SNAPSHOT/TJISE_CLOCK_OWNER` 读取父进程快照，
   不各自另选时间源。
3. **网络授时**：标准库 SNTP，UDP 123，公共源 ntp.aliyun.com / ntp1.aliyun.com /
   time.cloudflare.com；一轮三源共享 5 秒期限，每源至多一次；64 位随机请求标识、
   8 字节 originate 全回显校验、已解析 endpoint 的 connected UDP（peer 校验）、
   DNS 后真实发/收 tick 计 RTT、15.625ms 保守分辨率、有符号根延迟取绝对值。
   **一致组**：≥2 源先对齐到同一 tick，按「组内任意两人一致」枚举两两一致子集
   （每对差 ≤ 双方误差和+0.25 秒），去掉真子集后只比较极大组；最高支持数对应
   多个互相矛盾的极大组时拒绝发布，不按 RTT/枚举顺序/三源排列任选，取组内最小 RTT。
   **网络半程**：服务器处理耗时 = T3−T2，网络往返 = 本地 RTT − 处理耗时，
   收包中心 = T3 + (RTT − (T3−T2))/2；容差内（处理耗时超过 RTT 不超过 50ms）
   负网络时长夹到 0，超容差拒绝；误差界仍为 RTT/2 + 根延迟/2 + dispersion +
   计时分辨率，不因扣处理耗时缩小。不确定度 ≤0.5 秒才 ready。
   周期 300 秒后台重校；holdover 计 100ppm 保守漂移，
   >900 秒或超阈值 → unavailable。sync 单飞 + 轮次围栏：新轮发布后旧轮永不发布。
4. **时钟门禁**：时钟不可用时自动报名禁启（界面开始按钮禁用且原因唯一可见
   「北京时间未校准」+ 后端拒绝）；运行中失去校时 → 停止自动动作（协调器恰一次
   plan_done：canceled + 原因「北京时间暂不可用」），已提交未决保留 unconfirmed/次数，
   零提交不算失败。登录 check 与时钟完全正交——校时失败不清凭证、不改判登录失效。
5. **登录估算 v2**：`ui.json.session_timing_v2`（credential_id=token 摘要、
   saved_epoch、time_source、uncertainty_s）只在**登录 worker 成功路径**建立
   （`_anchor_login_success`）；复用/刷新不续期；换凭证换新锚点；无锚点/不匹配/
   时钟不可用 → hours_left=null（详情显示「未知（以学校校验为准）」），
   绝不拿 datetime.now 减旧 saved_at 冒充准确小时数。
6. **生命周期 v2**：`ui.json.lifecycle_v2` 以业务 epoch 记账 started/heartbeat/closed；
   正常/异常/未知三质量保留；首校时迟到用本进程经过时长倒推启动 epoch（只恢复一次）；
   初始化/心跳/正常关闭只要**本次**取不到可信 epoch，`quality=unknown`，
   不沿用旧 heartbeat/closed_epoch 凑新的可信事实；生产时钟路径缺 v2 或质量不足
   一律 unknown，旧墙钟文本不迁移（不可反推真实 UTC）。`run_id` 归属核验与写回
   在同一个跨进程短时偏好事务内完成：文件已属于其他实例时旧实例整份让位，
   不先清空再写成本实例；拿不到锁则放弃该次元数据写入。详见决策 008。
7. **提交边界**：真正发 signUp 前要求业务下界 ≥ sign_start、业务上界 < 有效截止，
   避免测量误差导致提前或截止后发起；次数只数真实请求、未决不自动重发。

## 快照合同（返修轮定稿，客户端/界面/测试夹具同形）

- `epoch_s` 永远对应 `tick_s`（授时锚点）；`sampled_tick_s` 只是快照生成时刻；
  `age_s = sampled_tick_s - tick_s`；采样时刻业务时间 = epoch_s + (sampled_tick_s - tick_s)。
- `ClockClient.now = epoch_s + elapsed_s() - tick_s`，不得改用 sampled_tick_s 作锚点；
  一次读取内使用同一份合法快照，不拼接两代字段。
- `uncertainty_s` 是**采样时**误差（已含锚点到采样的漂移）；客户端只补
  `sampled_tick_s` 之后的 100ppm 漂移，绝不按整段 age 再叠加一次采样前漂移。
  授时年龄按 `current_tick - tick_s` 累计，用于 300/900 秒门禁；
  重写快照文件不重置 900 秒持有期与误差累计。
- 界面按同一公式外推（`clockSnapshotFields`），旧代次/同代旧采样/其他实例不落地；
  同代「不可用」不抹已建立锚点；`businessNowMs`/`clockReading` 每次校验状态/年龄/
  采样后漂移/总误差。最近校准由权威 `age_s` + 接入后的经过时长推导，F5/bootstrap/
  同代轮询都不续期。
- 请求超时、对账期限、暂存终态 TTL、桥接等待等**经过时长**使用 `performance.now`
  同域单调读数；OS 墙钟前移/回拨不得提前或延长。`request_id` 唯一性与 OS 诊断
  差值仍可用 `Date.now`。

## 界面呈现（返修轮定稿：单一紧凑入口）

- 顶栏唯一时间入口 `#clock`：正常「北京时间 + HH:MM:SS + 状态图形」五态
  （已校准/校准中…/使用上次校准结果/待校准/演示时间）；340px 详情浮层承载
  最近校准、估计误差、使用记录、时间来源与「重新校准时间/重试校准」，反馈只在详情。
  浮层状态图形与顶栏同源（失败/等待警告色、已校准成功色、演示中性色）。
- 顶栏/现在线/倒计时/任务卡/详情/登录剩余全部同源 `businessNowMs`；
  未校准显示「待校准」，绝不回退本机墙钟（含每秒 tick 重绘路径）。
- 展示模型区分「学校没给开放时间」与「本机没有可信现在」：前者才写「待公布」；
  后者保留已知绝对时刻并标「待校准」，大屏写「时间待校准」+ 单一恢复入口，
  可报名/下一场等计数写「待确认」，绝不伪造 0/6；已报名等学校事实照常保留；
  恢复校准后原地重算分组/计数/倒计时，选择与重试策略不丢。
- 文案真源：[copywriting.md](../specs/copywriting.md) §10；不把某位用户的
  电脑时间设置写给公众。

## 明确不做

- 不调用 w32tm/管理员校时、不改注册表/代理/TLS 校验。
- 不把学校 HTTP Date（clock_sample）当标准授时——它只是粗测诊断；
  「按本机时间执行/建议同步系统时间」的旧指引已删除。
- 不承诺毫秒必达或准点必成功；显示测得的不确定度。

## 官方来源

- RFC 5905（NTPv4）：https://www.rfc-editor.org/rfc/rfc5905
- 阿里云公共 NTP：https://help.aliyun.com/en/ecs/user-guide/alibaba-cloud-ntp-server/
- Cloudflare Time Service：https://developers.cloudflare.com/time-services/ntp/usage/
- Windows 时间服务：https://learn.microsoft.com/windows/win32/sysinfo/windows-time
- GetTickCount64：https://learn.microsoft.com/windows/win32/api/sysinfoapi/nf-sysinfoapi-gettickcount64

## 验收边界（implemented/verified 与真实学校分开）

- 首次同步的进行/结束状态由后端快照 `sync_status` 提供，不能因已收到 bootstrap
  就把仍在获取时间误报成失败；已有可读锚点的后台重校继续显示业务时间。
- 无可信现在时，下一场查询仍返回完整空目标，LED 不把缺少推断依据说成学校待公布。
  日志使用业务北京时间或保留 worker 源时间；北京「今天」不受 OS 时区影响。
- 偏好保存超时不得绕过跨进程锁，失败不谎报成功；界面最多重试一次并保留较新设置。
  实测证据与候选哈希见本轮结果，真实学校验收边界不因此扩大。

- 返修轮隔离验收（2026-10-26）：ClockClient 完整合同红绿（延迟样本/899 秒重写/
  误差漂移/负 tick/缺 owner/真实 RetryBudget）、NTP 请求伪造与围栏红绿、
  执行链去墙钟红绿、登录 v2 与生命周期 v2 红绿、浏览器 QA 39 场景
  （boot 前/运行中 ±8h/±30d 错时、F5 代次围栏、holdover/过期、首校时延迟、
  真实点击重新校时、五视口）、公共 NTP 只读探针单列。
- 最终边界轮（2026-10-26）：CB01 两两一致组（三源 6 置换、AB/BC 同支持 AC 冲突
  在总误差仍 <0.5 时拒绝）、CB02 处理耗时扣除（20ms/0/近 RTT/负网络/超容差）、
  CB03 本次取时失败即 unknown 与真实双进程 run_id 接管（持锁 0.4 秒交错）、
  CB04 采样后漂移只计一次与 F5 旧锚点年龄、CB05 校时失败不冒充待公布
  （已知开放时间/真缺 sign_start/已报名事实/恢复后选择与策略）、CB06 暂存 TTL
  与桥接等待跨 OS ±8h/30d。浏览器 QA 在原 39 场景上新增 9 个边界场景；
  全部为**隔离回环**证据。
- 早期「OS ±30 天红绿回归 / 五视口」记录仍有效，但以最终边界轮证据为现行口径；
  上轮 480 单测 / 46 Git fixture / 26 浏览器场景是历史证据，不冒充本轮。
- 真实 NTP 只读探针单列记录；一次成功不代表永久精度，也不声称 SNTP 永远精确。
- 学校真实开放瞬间的准点性、学校与标准时间差异：pending_user_review。
