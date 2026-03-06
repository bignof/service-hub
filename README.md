# service-hub

service-hub 是面向平台侧的控制服务，负责接收 service-agent 的 WebSocket 连接，维护 agent 在线状态，并向 agent 下发 Docker Compose 操作指令。

## 能力

- 提供 `/ws/agent/{agentId}` WebSocket 接入点，兼容现有 agent 协议
- 记录 agent 的连接时间、最后心跳时间、最后一次 pong 时间和在线状态
- 提供 HTTP API 给其他服务查询 agent 存活情况
- 提供 HTTP API 给其他服务向指定 agent 下发 `update` / `restart` 指令
- 跟踪每个 `requestId` 的处理状态：`queued`、`processing`、`success`、`failed`
- V2 第一阶段已支持命令、审计事件和 Agent 最新状态持久化

## 启动方式

### Docker Compose

1. 复制配置文件

```bash
cp .env.example .env
```

2. 修改 `.env` 中的 `ADMIN_TOKEN`
3. 修改 `.env` 中的 `SERVICE_HUB_IMAGE`
4. 按需修改 `.env` 中的 `DATABASE_URL`
5. 拉取镜像并启动

```bash
docker compose pull
docker compose up -d
```

### 本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

### 数据库迁移

服务启动时会自动执行 Alembic 迁移到最新 schema。需要手动执行时可直接运行：

```bash
alembic -c alembic.ini upgrade head
```

如果数据库是旧版本通过自动建表初始化、但还没有 `alembic_version`，当前版本会在首次启动时自动补齐基线，不会重复建表。

## 环境变量

| 变量                    | 说明                            | 默认值                                       |
| ----------------------- | ------------------------------- | -------------------------------------------- |
| `HOST`                  | 服务监听地址                    | `0.0.0.0`                                    |
| `PORT`                  | 服务监听端口                    | `8080`                                       |
| `SERVICE_HUB_IMAGE`     | 运行时拉取的镜像地址            | `service-hub:latest`                         |
| `SERVICE_HUB_BIND_PORT` | 宿主机暴露端口                  | `8080`                                       |
| `SERVICE_HUB_DATA_DIR`  | SQLite 持久化目录挂载点         | `./data`                                     |
| `ADMIN_TOKEN`           | Agent 管理接口的管理令牌        | 无默认值，必须显式配置                       |
| `HEARTBEAT_TIMEOUT`     | 超过该秒数未收到消息则视为离线  | `90`                                         |
| `COMMAND_HISTORY_LIMIT` | 每个 agent 保留的命令历史条数   | `200`                                        |
| `DATABASE_URL`          | 数据库连接串，支持 SQLite/MySQL | `sqlite:////data/service-hub/service-hub.db` |

## Agent 接入地址

先由平台侧创建 agent 并签发首个独立 key：

```http
POST /api/agents
X-Admin-Token: <ADMIN_TOKEN>
Content-Type: application/json

{
  "agentId": "prod-server-01"
}
```

这里的 `X-Admin-Token` 通过 HTTP Header 传递，值就是 hub 的环境变量 `ADMIN_TOKEN`。

如果是已有 agent 需要换 key，再调用：

```http
POST /api/agents/{agentId}/credentials/rotate
X-Admin-Token: <ADMIN_TOKEN>
```

返回体中的 `agentKey` 只会在签发/轮换时返回一次，应写入 agent 侧环境变量 `AGENT_KEY`。

Agent 使用如下地址连接：

```text
ws://<SERVICE_HUB_HOST>:8080/ws/agent/<AGENT_ID>?key=<AGENT_KEY>
```

agent 侧可以将 `WS_URL` 配置为：

```text
ws://<SERVICE_HUB_HOST>:8080/ws/agent
```

## API

- FastAPI 在线文档：`/docs`
- OpenAPI JSON：`/openapi.json`
- 第三方对接说明：`docs/THIRD_PARTY_API.md`

### 健康检查

```http
GET /health
```

### 查询全部 agent

```http
GET /api/agents
```

返回示例：

```json
[
  {
    "agentId": "prod-server-01",
    "connected": true,
    "online": true,
    "credentialConfigured": true,
    "remote": "10.0.0.8:51234",
    "keyIssuedAt": "2026-03-06T09:59:00Z",
    "connectedAt": "2026-03-06T10:00:00Z",
    "disconnectedAt": null,
    "lastSeenAt": "2026-03-06T10:01:00Z",
    "lastHeartbeatAt": "2026-03-06T10:01:00Z",
    "lastPongAt": null,
    "staleAfterSeconds": 90
  }
]
```

### 查询单个 agent 状态

```http
GET /api/agents/{agentId}
```

### 查询指定 agent 的命令历史

```http
GET /api/agents/{agentId}/commands?status=failed&requestedBy=platform-api&sortBy=updatedAt&order=desc&limit=20&offset=0
```

返回结构与 `GET /api/commands` 一致，只是额外固定了 `agentId` 维度。

### 查询单条命令状态

```http
GET /api/commands/{requestId}
```

### 查询命令列表

```http
GET /api/commands?agentId=prod-server-01&status=success&action=update&requestedBy=platform-api&requestSource=ops-console&createdAfter=2026-03-01T00:00:00Z&createdBefore=2026-03-06T23:59:59Z&sortBy=updatedAt&order=desc&limit=50&offset=0
```

返回示例：

```json
{
  "items": [
    {
      "requestId": "c7d99f80-b88e-45fc-a6df-7fe1d9eab1f5",
      "agentId": "prod-server-01",
      "status": "success",
      "action": "restart",
      "dir": "/data/dev/admin",
      "image": null,
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
      "createdAt": "2026-03-06T10:02:00Z",
      "updatedAt": "2026-03-06T10:02:03Z",
      "ackAt": "2026-03-06T10:02:01Z",
      "resultAt": "2026-03-06T10:02:03Z"
    }
  ],
  "total": 1,
  "limit": 50,
  "offset": 0,
  "hasMore": false,
  "sortBy": "updatedAt",
  "order": "desc"
}
```

### 重试失败命令

```http
POST /api/commands/{requestId}/retry
X-Requested-By: platform-api
X-Requested-Source: ops-console
```

只有失败命令允许重试。重试会生成新的 `requestId`，原命令保留不变，并在审计事件中追加 `retry` 记录。

### 查询命令审计事件

```http
GET /api/commands/{requestId}/events
```

### 下发命令

```http
POST /api/agents/{agentId}/commands
Content-Type: application/json

{
  "action": "update",
  "dir": "/data/dev/admin",
  "image": "hello-world:latest"
}
```

`restart` 示例：

```http
POST /api/agents/{agentId}/commands
Content-Type: application/json

{
  "action": "restart",
  "dir": "/data/dev/admin"
}
```

返回示例：

```json
{
  "accepted": true,
  "command": {
    "requestId": "c7d99f80-b88e-45fc-a6df-7fe1d9eab1f5",
    "agentId": "prod-server-01",
    "status": "queued",
    "action": "update",
    "dir": "/data/dev/admin",
    "image": "hello-world:latest",
    "payload": {
      "type": "command",
      "requestId": "c7d99f80-b88e-45fc-a6df-7fe1d9eab1f5",
      "action": "update",
      "dir": "/data/dev/admin",
      "image": "hello-world:latest"
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

## 说明

- V2 第一阶段会把 Agent 最新状态、命令记录和命令事件写入数据库
- Schema 现在通过 Alembic 管理，后续结构变更需要新增迁移脚本
- WebSocket 连接对象仍然只保存在内存中，因此 Hub 重启后会等待 Agent 自动重连
- 默认推荐本地开发使用 SQLite，生产环境使用 MySQL
- Agent 认证已改为“每个 agentId 对应一个独立 key”，不再依赖所有 agent 共用一个固定连接 token
- Agent 的在线判定依据仍然是连接未断开且最近一次消息时间未超过 `HEARTBEAT_TIMEOUT`
- 命令查询支持按 `createdAt` / `updatedAt` 排序，并支持失败命令的重试下发
- `docker-compose.yml` 已改为拉取镜像部署，并内置 `/health` 容器健康检查

## V2 规划

- 持久化、审计和数据库支持的规划见 `docs/V2_PLAN.md`
- 第三方服务对接 HTTP API 见 `docs/THIRD_PARTY_API.md`
