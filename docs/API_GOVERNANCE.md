# service-hub 功能与 API 治理

本文档用于约束 service-hub 的职责边界、公开 API 面、后续扩展规则，避免功能和接口继续无序增长。

## 设计目标

- 把 service-hub 限定为控制平面，而不是业务平台本体
- 把公开 HTTP API 控制在少量稳定资源上
- 把 Agent 协议、平台对接 API、未来规划能力明确分层
- 任何新增接口都必须先回答“为什么不能复用现有资源”

## Hub 的职责边界

service-hub 当前只负责 4 类事情：

1. Agent 身份与接入

- 创建 Agent
- 签发和轮换 Agent key
- 维护 WebSocket 接入认证

2. Agent 最新状态视图

- 维护当前连接状态和在线状态
- 提供 Agent 最新快照查询
- 暴露与命令执行相关的轻量汇总视图，例如 queued 和 processing 数

3. 命令控制面

- 创建命令记录
- 向在线 Agent 下发命令
- 接收 Agent ACK 和结果回报
- 支持失败命令重试

4. 审计与查询

- 持久化命令主记录
- 持久化命令事件流
- 提供分页查询和事件回放

## 明确不属于 Hub 的职责

以下内容不要直接塞进 service-hub，除非架构决策明确变更：

- 业务应用部署编排逻辑
- 前端页面专属聚合接口
- 审批流、工单流、发布流程引擎
- 定时调度中心
- 批量任务编排器
- Agent 本地执行细节和锁调度策略
- 长期日志检索系统
- 多步骤工作流 DSL

这些能力如果需要，应优先放在平台层或单独服务中，通过调用 hub 的稳定 API 完成。

## API 分层

### 1. Agent 协议面

仅供 service-agent 使用，不属于第三方开放接口：

- `GET /ws/agent/{agentId}`

规则：

- 只承载连接、心跳、ACK、结果回传
- 不在 WebSocket 协议里叠加平台业务语义
- 新增消息类型前，必须先确认是否真的不能通过现有 `command` / `ack` / `result` / `heartbeat` / `pong` 表达

### 2. 平台公开 HTTP API

对平台、控制台、调度器开放的接口，只允许围绕两个资源展开：

- `agent`
- `command`

当前公开面：

- Agent 管理
  - `GET /api/agents`
  - `GET /api/agents/{agentId}`
  - `POST /api/agents`
  - `POST /api/agents/{agentId}/credentials/rotate`
- 命令管理
  - `GET /api/commands`
  - `GET /api/commands/{requestId}`
  - `GET /api/commands/{requestId}/events`
  - `POST /api/agents/{agentId}/commands`
  - `POST /api/commands/{requestId}/retry`

### 3. 系统运维面

- `GET /health`

规则：

- 只用于健康检查和基础连通性
- 不扩展成运维大杂烩接口

## API 扩展原则

新增 API 前，必须按顺序检查：

1. 能否通过给现有资源补字段解决

- 例如 Agent 新增观测指标，应优先补到 `AgentSnapshot`
- 例如命令新状态或审计字段，应优先补到 `CommandSnapshot` 或 event payload

2. 能否通过现有列表接口的筛选、排序、分页解决

- 如果只是“看某类命令”，优先加 query filter，而不是新开一个 `/api/foo-commands`

3. 如果必须加新接口，是否仍属于 `agent` 或 `command` 资源

- 优先新增子资源，例如 `/api/commands/{id}/events`
- 避免新增语义模糊的顶级路径，例如 `/api/dashboard`、`/api/overview`、`/api/actions`

4. 如果已经超出 `agent` / `command` 两个资源模型，就不应该继续堆在 hub 里

## URL 与资源规范

- 顶级公开资源只保留 `agents` 和 `commands`
- 路径名使用资源名，不使用 UI 视角名词
- 查询类能力优先用 `GET + query params`
- 动作类能力只允许用于“天然不是资源更新”的少数场景，目前仅保留：
  - `POST /api/agents/{agentId}/credentials/rotate`
  - `POST /api/commands/{requestId}/retry`

如果未来新增动作型端点，必须证明它不能自然表达为：

- 创建资源
- 更新资源状态
- 查询资源子集

## 数据模型演进规则

- Agent 侧只保留“最新状态快照”，不要在主接口里混入完整历史
- 命令主记录表示“当前事实”
- 命令事件表示“历史轨迹”，append-only
- 需要历史分析时，优先新增事件或单独的历史资源，不要把 `AgentSnapshot` 变成时间序列接口

## 文档同步要求

只要涉及 hub 功能或 API 变化，至少同步以下文档：

- `README.md`
- `docs/THIRD_PARTY_API.md`
- `docs/apifox-openapi.json`
- 如果属于能力边界变化，还要同步本文档

## 当前建议的下一步边界

可以继续做：

- 在 `GET /api/commands` 上补更清晰的筛选能力
- 在 `AgentSnapshot` 上补少量只读观测字段
- 增加 `GET /api/agents/{agentId}/history`，前提是它只表达状态历史，不混入命令审计

当前明确不做：

- 面向前端页面的 dashboard 聚合接口
- 把批量下发、审批、调度策略直接塞进 hub 主进程
- 把 agent 本地执行队列细节直接暴露成控制命令

## 新增功能检查清单

后续每次准备给 hub 加功能，先过一遍这 6 条：

1. 这是控制平面能力，还是平台业务能力。
2. 能否复用现有 `agent` / `command` 资源。
3. 能否只加字段或 filter，而不是新增 endpoint。
4. 是否会让 `GET /health` 承担非健康职责。
5. 是否同步了 README、第三方 API 文档和 OpenAPI 导出。
6. 是否补了回归测试，且覆盖率不被拉低。
