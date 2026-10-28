# 文档索引

| 文档 | 适合谁看 | 内容 |
|------|----------|------|
| [../README.md](../README.md) | 第一次接触项目 | 功能、快速开始、两版怎么选、常见问题 |
| [FEISHU_BOT.md](FEISHU_BOT.md) | 第一次配飞书 | 群机器人 vs 自建应用、权限、事件订阅、拿 chat_id/open_id、错误码对照 |
| [CONFIG.md](CONFIG.md) | 要填配置的人 | config.json 全字段、13 个环境变量、三套可抄示例、配置类故障 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 要改代码的人 | 数据流、模块职责、接口清单、去重与推送节奏、告警链路 |
| [OPERATIONS.md](OPERATIONS.md) | 日常运维 | 服务器布局、systemd、crontab、日志、部署更新、回滚、故障处理、迁移记录 |
| [LOGIN.md](LOGIN.md) | 处理登录态问题 | token 寿命实测、守护进程续期、验证码 OCR 无人值守登录、凭据管理、人工兜底 |
| [GRAB.md](GRAB.md) | 预报名/抢报名 | 状态机、时间线、卡片快照、边界 |
| [RETRY_POLICY_GUIDE.md](RETRY_POLICY_GUIDE.md) | 普通使用者（图解） | 报名截止硬上限、重试时长/次数怎么选、「第 N 次提交」计数、八种终态，Mermaid 时间线一看就懂 |
| [TIMING_AND_DEDUP_ANALYSIS.md](TIMING_AND_DEDUP_ANALYSIS.md) | 想理解"为什么是这个时点/这套去重" | 完整推导与请求量账本 |

## archive/

历史文档，保留备查，**内容可能已过时**：

| 文档 | 说明 |
|------|------|
| `README-full-20261022.md` | 重构前的完整 README（752 行，含大量实现细节与平台配置） |
| `BUILD_FROM_SCRATCH.md` | 从零搭建指南（旧流程，服务器路径已变） |
| `WORKBUDDY_LOGIN_PROMPT.md` | 早期交给云端助手的登录恢复提示词 |
| `ASK_CODEX_REVIEW.md` / `REVIEW_PROMPT.md` | 代码评审用的提问清单 |
