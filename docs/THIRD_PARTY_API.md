# service-hub 第三方对接 API 文档

本文档面向需要集成 `service-hub` 的平台、运维控制台、调度系统和自动化脚本。

## 适用范围

- 第三方系统应只使用 HTTP API
- Agent 与 Hub 的 WebSocket 协议不属于第三方开放接口范围
- 本文档覆盖 Agent 管理、查询、下发命令、重试失败命令和审计查询
- Hub 的职责边界与 API 扩展规则见 `docs/API_GOVERNANCE.md`

## 基础信息

- Base URL：`http://<service-hub-host>:8080`
- Content-Type：`application/json`
- 在线文档：`GET /docs`
- OpenAPI：`GET /openapi.json`

## 认证与请求头

当前 HTTP API 分为两类：

- Agent 管理接口：必须携带 `X-Admin-Token`
- 查询与命令接口：当前不要求管理令牌，但建议携带审计头

建议第三方调用时始终补齐以下审计头：

- `X-Requested-By`：调用方身份，例如 `ops-console`、`platform-api`
- `X-Requested-Source`：调用来源，例如 `manual-operation`、`scheduler-job`

这两个头会被持久化到命令记录中，用于后续审计。

以下 Agent 管理接口额外要求：

- `X-Admin-Token`：Hub 管理令牌，用于创建 agent 和签发/轮换 agent key

### `X-Admin-Token` 怎么传

通过 HTTP Header 传递，不放在 URL、QueryString 或请求体里。

请求示例：

```bash
curl -X POST "http://<service-hub-host>:8080/api/agents" \
  -H "Content-Type: application/json" \
  -H "X-Admin-Token: <ADMIN_TOKEN>" \
  -d '{"agentId":"prod-server-01"}'
```

如果 `X-Admin-Token` 缺失或不正确，管理接口会返回：

```json
{
  "detail": "Invalid admin token"
}
```

## 返回约定

### 成功状态码

- `200 OK`：普通查询成功
- `202 Accepted`：命令已接收并进入队列

### 常见错误状态码

- `404 Not Found`：Agent 或命令不存在
- `409 Conflict`：Agent 离线，或当前命令不允许重试
- `422 Unprocessable Entity`：请求参数或请求体不合法
- `403 Forbidden`：管理接口缺少或传错 `X-Admin-Token`
- `502 Bad Gateway`：Hub 已接收请求，但向 Agent 下发失败

### 通用错误体

```json
{
  "detail": "Agent not found"
}
```

## 数据模型

### AgentSnapshot

```json
{
  "agentId": "prod-server-01",
  "connected": true,
  "online": true,
  "credentialConfigured": true,
  "remote": "10.0.0.8:51234",
  "keyIssuedAt": "2026-03-06T17:59:00+08:00",
  "connectedAt": "2026-03-06T18:00:00+08:00",
  "disconnectedAt": null,
  "lastSeenAt": "2026-03-06T18:01:00+08:00",
  "lastHeartbeatAt": "2026-03-06T18:01:00+08:00",
  "lastPongAt": null,
  "staleAfterSeconds": 90,
  "queuedCommands": 1,
  "processingCommands": 2,
  "lastCommandCreatedAt": "2026-03-06T18:02:00+08:00"
}
```

说明：本文档中的时间字段默认使用中国时区（`+08:00`）。

补充说明：

- `queuedCommands` 表示 hub 已受理但尚未收到 Agent ACK 的命令数。这通常意味着命令仍在传输中，或 Agent 因同目录互斥而尚未开始执行。
- `processingCommands` 表示 Agent 已 ACK、当前正在执行的命令数。
- `lastCommandCreatedAt` 表示最近一次给该 Agent 创建命令记录的时间。

### CommandSnapshot

```json
{
  "requestId": "c7d99f80-b88e-45fc-a6df-7fe1d9eab1f5",
  "agentId": "prod-server-01",
  "status": "success",
  "action": "restart",
  "dir": "/data/dev/admin",
  "image": null,
  "originalRequestId": null,
  "retryCount": 0,
  "requestedBy": "platform-api",
  "requestSource": "ops-console",
  "payload": {
    "type": "command",
    "requestId": "c7d99f80-b88e-45fc-a6df-7fe1d9eab1f5",
    "action": "restart",
    "dir": "/data/dev/admin"
  },
  "output": null,
  "message": null,
  "error": null,
  "createdAt": "2026-03-06T18:02:00+08:00",
  "updatedAt": "2026-03-06T18:02:03+08:00",
  "ackAt": "2026-03-06T18:02:01+08:00",
  "resultAt": "2026-03-06T18:02:03+08:00"
}
```

### CommandListResponse

```json
{
  "items": [],
  "total": 0,
  "limit": 50,
  "offset": 0,
  "hasMore": false,
  "sortBy": "updatedAt",
  "order": "desc"
}
```

## API 清单

### 1. 健康检查

```http
GET /health
```

返回：

```json
{
  "status": "ok"
}
```

### 2. 查询全部 Agent

```http
GET /api/agents
```

返回：`AgentSnapshot[]`

### 3. 创建 Agent 并签发初始 key

```http
POST /api/agents
X-Admin-Token: <ADMIN_TOKEN>
Content-Type: application/json

{
  "agentId": "prod-server-01"
}
```

返回示例：

```json
{
  "agent": {
    "agentId": "prod-server-01",
    "connected": false,
    "online": false,
    "credentialConfigured": true,
    "remote": null,
    "keyIssuedAt": "2026-03-06T17:59:00+08:00",
    "connectedAt": null,
    "disconnectedAt": null,
    "lastSeenAt": null,
    "lastHeartbeatAt": null,
    "lastPongAt": null,
    "staleAfterSeconds": 90
  },
  "agentKey": "generated-once-only",
  "issuedAt": "2026-03-06T17:59:00+08:00"
}
```

已存在的 agent 会返回 `409 Agent already exists`。

说明：`agentKey` 默认不会按时间自动过期，会一直有效，直到你主动调用轮换接口生成新 key。

注意：这里的 `X-Admin-Token` 就是 hub 进程环境变量 `ADMIN_TOKEN` 的值。

### 4. 为已有 Agent 轮换 key

```http
POST /api/agents/{agentId}/credentials/rotate
X-Admin-Token: <ADMIN_TOKEN>
```

返回：包含新的 `agentKey`。该明文只会在响应里出现一次。

说明：轮换后，新 key 立即生效，旧 key 立即失效；新 key 同样不会按时间自动过期。

调用示例：

```bash
curl -X POST "http://<service-hub-host>:8080/api/agents/prod-server-01/credentials/rotate" \
  -H "X-Admin-Token: <ADMIN_TOKEN>"
```

### 5. 查询单个 Agent

```http
GET /api/agents/{agentId}
```

### 6. 查询命令列表

```http
GET /api/commands?agentId=prod-server-01&status=success&action=restart&requestedBy=platform-api&requestSource=ops-console&createdAfter=2026-03-01T00:00:00%2B08:00&createdBefore=2026-03-06T23:59:59%2B08:00&sortBy=updatedAt&order=desc&limit=50&offset=0
```

返回：`CommandListResponse`

说明：如果只查询某个 Agent 的命令历史，统一使用 `agentId` 查询参数过滤，不再提供单独的 `/api/agents/{agentId}/commands` 接口。

### 7. 查询单条命令

```http
GET /api/commands/{requestId}
```

返回：`CommandSnapshot`

### 8. 查询命令审计事件

```http
GET /api/commands/{requestId}/events
```

返回：事件数组，按时间升序。

事件类型目前包括：

- `created`
- `ack`
- `result`
- `retry`

### 9. 下发命令

```http
POST /api/agents/{agentId}/commands
X-Requested-By: platform-api
X-Requested-Source: ops-console
Content-Type: application/json

{
  "requestId": "manual-20260306-0001",
  "action": "restart",
  "dir": "/data/dev/admin"
}
```

`update` 示例：

```json
{
  "requestId": "manual-20260306-0002",
  "action": "update",
  "dir": "/data/dev/admin",
  "image": "nginx:1.27-alpine"
}
```

返回：

```json
{
  "accepted": true,
  "command": {
    "requestId": "manual-20260306-0001",
    "agentId": "prod-server-01",
    "status": "queued",
    "action": "restart",
    "dir": "/data/dev/admin",
    "image": null,
    "originalRequestId": null,
    "retryCount": 0,
    "requestedBy": "platform-api",
    "requestSource": "ops-console",
    "payload": {
      "type": "command",
      "requestId": "manual-20260306-0001",
      "action": "restart",
      "dir": "/data/dev/admin"
    },
    "output": null,
    "message": null,
    "error": null,
    "createdAt": "2026-03-06T10:02:00Z",
    "updatedAt": "2026-03-06T10:02:00Z",
    "ackAt": null,
    "resultAt": null
  }
}
```

### 10. 重试失败命令

```http
POST /api/commands/{requestId}/retry
X-Requested-By: platform-api
X-Requested-Source: ops-console
```

约束：

- 只有 `failed` 状态命令允许重试
- 重试后会生成新的 `requestId`
- 新命令的 `originalRequestId` 指向原失败命令
- 新命令的 `retryCount` 为原命令 `retryCount + 1`

## 推荐对接流程

### 查询 Agent 并下发命令

1. 调用 `GET /api/agents/{agentId}` 确认 `online=true`
2. 调用 `POST /api/agents/{agentId}/commands` 下发命令
3. 记录返回的 `requestId`
4. 轮询 `GET /api/commands/{requestId}` 或读取 `GET /api/commands/{requestId}/events`
5. 如结果为 `failed`，可调用 `POST /api/commands/{requestId}/retry`

### 轮询建议

- 初始轮询间隔建议 `2s`
- 命令执行超过 `queued/processing` 阶段后即可停止轮询
- 需要完整审计时，优先读取事件接口而不是只读单命令状态

## 非兼容变更约定

- 新增字段默认视为向后兼容
- 删除字段、修改字段类型、修改状态语义属于非兼容变更
- 如果后续需要对第三方开放鉴权，会优先在本文档新增说明
