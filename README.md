# Ombre Brain - Haven/Rain Fork

这是 [P0luz/Ombre-Brain](https://github.com/P0luz/Ombre-Brain) 的二次开发版本。原版是一套给 Claude 使用的长期情绪记忆 MCP；这个 fork 在原版的 Markdown bucket、情绪坐标、遗忘曲线、MCP 工具、Dashboard、向量检索基础上，增加了 Gateway 自动注入、Persona State、长期锚点、关系天气、年轮评论、whisper、Supabase 同步和 ChatGPT Connector OAuth。

本 README 以本 fork 的运行方式为准。原版 Docker Hub 预构建镜像、`docker-compose.user.yml`、Render / Zeabur 快速部署方式不包含这些 fork 能力，因此这里不再保留原版快速部署教程。

## 先读这个

- 这是一个个性化 fork，不是原版 Ombre-Brain 的无改动镜像。
- 原版代码仍遵循原项目 MIT License；本 fork 新增内容允许个人学习、自用和非商业二改，商业使用需另行取得授权。详见 [`NOTICE.md`](NOTICE.md)。
- 默认人设、提示词和年轮作者使用 `config.yaml` 里的 `identity` 名字；示例默认是 `Haven`、`Rain`、`小雨/xiaoyu`。
- 生产部署建议使用源码构建，并同时运行 `ombre-brain` 和 `ombre-gateway` 两个服务。
- bucket 数据和运行状态必须放在持久化目录里；`state` 不建议放进任何双向同步目录。
- `X-Ombre-Session-Id` 是本 fork 的 Gateway 会话头，不是 OpenAI 标准字段。它像 Persona 的“房间号”：同一个值会共用同一份 persona_state 和召回冷却记录。可以自己起，比如 `my-main`、`chat-main`，不要照抄旧文档里的 `xiaoyu-main`。
- 给 Operit 或其它聊天平台写工具使用清单时，先区分 MCP 工具模式和 Gateway 自动注入模式，参考 [`docs/Tool Guide.md`](<docs/Tool Guide.md>)。

## 二次开发能力

先分清楚：这些是原仓库已经有的基础，不算本 fork 的二次开发：

| 原版已有基础 | 说明 |
| --- | --- |
| Markdown bucket | 每条记忆是 Obsidian 友好的 Markdown + YAML frontmatter |
| Russell 情绪坐标 | `valence / arousal` 情绪打标 |
| 遗忘曲线与归档 | inactive 记忆会衰减、归档，feel 不参与普通浮现 |
| MCP 工具 | 原版已有 `breath / hold / grow / trace / pulse / dream` |
| Dashboard | 原版已有桶列表、详情页、记忆网络、导入面板 |
| 双通道检索 | fuzzy 关键词 + embedding 语义检索 |
| 脱水与打标 | LLM 生成压缩正文、domain/tags/情绪等元数据 |
| 历史导入 | Claude/ChatGPT/Markdown/文本导入为 bucket |

下面才是这个 fork 额外加的能力：

| 能力 | 说明 | 主要文件 |
| --- | --- | --- |
| OpenAI / Anthropic-compatible Gateway | 提供 `/v1/chat/completions`、`/v1/messages`、`/v1/models`，聊天客户端可直接接入 | `gateway.py` |
| 自动记忆注入 | 请求转发前按策略注入 Recent Context、Recalled Memory、Related Memory；Long-term State Summary 按间隔出现 | `gateway.py` |
| Persona State Engine | 保存 AI 回复后的全局人格、关系状态、每个 session 的短期心情 | `persona_engine.py` |
| 召回冷却 | 按 `X-Ombre-Session-Id` 记录轮次和最近注入，避免同一条记忆反复贴脸 | `gateway_state.py` |
| 多上游模型路由和备用 key | `gateway.upstreams` 可配置多个 OpenAI-compatible provider，按请求里的 `model` 路由；同一上游可配置多个 key，失败时自动尝试下一个 | `gateway.py`、`config.example.yaml` |
| 工具调用和流式兼容 | 透传 `tools / tool_choice / tool_calls`，支持 SSE 流式响应，兼容部分 reasoning_content 场景 | `gateway.py` |
| Memory Edge | 自动生成显式记忆关系边，Gateway 和 `breath()` 可补一跳相关记忆 | `memory_edges.py`、`reflection_engine.py` |
| 长期锚点 Anchor | 介于普通浮现和 pinned/permanent 之间的长期记忆位。`anchor=true` 的普通 bucket 不混入普通权重池，`breath()` 会用独立槽位少量带出，适合经过时间验证、未来仍需要被想起的关系锚点或项目锚点 | `server.py`、`dashboard.html` |
| Relationship Weather | 日印象保存为 `type=feel`，默认不单独注入，可在面板观察或按配置开启注入 | `reflection_engine.py` |
| 年轮 comments | 将再次阅读某条记忆时的感受挂到源 bucket 的 `metadata.comments` 下；旧 feel 可迁移成源记忆年轮 | `bucket_manager.py`、`server.py`、`dashboard.html` |
| whisper | 无源碎碎念/悄悄话独立保存为 `type=feel + whisper` 标签，可用 `breath(domain="whisper")` 单独读取 | `server.py` |
| Dashboard 编辑 | 支持正文编辑、前端用户年轮写入/删除、日印象月历、Persona 面板、网络图、手动 reflect；日印象页按日期显示完整日印象，不再做情绪天气图 | `dashboard.html`、`server.py` |
| 可选 Haven-diary/RiJi 摘记 | 完整日记留在 [Yinglianchun/RiJi](https://github.com/Yinglianchun/RiJi) 这类外部日记系统，Ombre 只提取少量长期有用记忆；不用可关闭 | `reflection_engine.py` |
| Supabase 同步 | 本地 bucket 与 Supabase memories 表同步，支持 tombstone 删除墓碑 | `scripts/sync_to_supabase.py` |
| ChatGPT Connector OAuth | 为 `/ombre/mcp` 提供 OAuth authorize/token 元数据 | `server.py` |

## 系统架构

```text
聊天客户端
  -> Ombre Gateway :18002
    -> 读取 buckets / embeddings / persona_state / gateway_state / memory_edges
    -> 拼隐藏上下文
    -> 转发上游模型
    -> 回复成功后更新 Persona State 和召回记录

MCP / Dashboard / 写入 API
  -> Ombre-Brain server :18001
    -> 写 Markdown bucket
    -> 写 embeddings.db
    -> 自动 enrich 记忆与关系边
    -> 生成日印象

维护脚本
  -> Supabase memories
  -> Tombstones
  -> 旧 feel 桶清理
```

## 数据模型

bucket 是 Markdown 文件，正文保存记忆内容，frontmatter 保存元数据。当前主要类型：

| 类型 | 作用 |
| --- | --- |
| `dynamic` | 普通事件、项目状态、关系片段 |
| `permanent` | pinned / protected 长期准则 |
| `feel` | AI 主观感受、日印象、whisper |
| `archive` | 已归档旧记忆 |
| `metadata.comments` | 年轮：源记忆下的多次补充感受，不是独立 bucket |

重要运行时文件建议放在独立 state 目录：

```text
embeddings.db       # 向量语义检索
gateway_state.db    # 每个 session 的轮次、最近注入、冷却
persona_state.db    # Persona 全局状态、关系状态、会话心情
memory_edges.jsonl  # 显式记忆关系边
.dashboard_auth.json
```

时间默认使用 `Asia/Shanghai`。`utils.now_iso()` 会生成东八区时间。

## 从原版仓库来要注意

这个 fork 不是“直接换镜像就能跑”的版本。原版用户迁移时要注意：

| 项 | 为什么要改 |
| --- | --- |
| 原版 Docker Hub 镜像 | 不包含本 fork 的 Gateway、Persona、Relationship Weather、年轮、whisper 和 Supabase 脚本 |
| 原版 quick start | 只启动 MCP server，不会启动 Gateway，也不会分离 state 目录 |
| `identity` 名字配置 | `identity.ai_name / user_name / user_display_name / user_aliases` 会影响 prompt、MCP 年轮作者、Dashboard 年轮作者 |
| `gateway.default_session_id` | 只有兼容路由缺少 `X-Ombre-Session-Id` 时才使用；通用部署建议改成自己的默认房间名 |
| `persona.profile_id` | 配置示例里是 `haven_xiaoyu`，通用部署应改成自己的稳定 id |
| `X-Ombre-Session-Id` | 这是本 fork 自定义的 Gateway session，不是 OpenAI 标准头 |
| 数据目录 | `buckets` 与 `state` 都要持久化；`state` 不要放进任何双向同步目录 |
| Supabase | 不需要就先关掉；需要时先建表、RPC、cron 和 tombstone 策略 |

至少检查这些位置：

```text
identity.py             # prompt 和年轮作者的名字来源
persona_engine.py       # Persona prompt、Long-term State Summary 文案
reflection_engine.py    # 日印象、日记摘记、user/AI 改写规则
dehydrator.py           # 长内容摘记命名规则
server.py               # MCP / Dashboard 年轮作者
dashboard.html          # Dashboard：桶列表、年轮删除、日印象月历、Persona、网络、配置和导入
config.example.yaml     # identity、persona.profile_id、gateway、reflection
README.md               # 示例文本
```

## 部署方式

当前推荐方式：源码构建 + Docker Compose 双服务。

### 目录建议

```text
/opt/Ombre-Brain                 # 仓库
/srv/ombre-brain/buckets         # Markdown buckets
/srv/ombre-brain/state           # sqlite/jsonl/auth 等运行状态
/srv/ombre-brain/config.yaml     # 生产配置
/opt/Ombre-Brain/.env            # 密钥环境变量，不提交
```

### 拉取代码

```bash
git clone https://github.com/Yinglianchun/Ombre-Brain.git /opt/Ombre-Brain
cd /opt/Ombre-Brain
```

### 准备目录和配置

```bash
mkdir -p /srv/ombre-brain/buckets /srv/ombre-brain/state
cp config.example.yaml /srv/ombre-brain/config.yaml
```

编辑 `/srv/ombre-brain/config.yaml`：

- `gateway.upstreams`：配置上游 OpenAI-compatible provider；同一上游多个 key 用 `api_key_envs`。
- `gateway.default_session_id`：少数兼容路由没传 `X-Ombre-Session-Id` 时的默认房间名，通用部署不要照抄旧示例名。
- `identity.*`：改 AI 名、前端用户作者名、prompt 里的用户称呼和亲密称呼。
- `persona.profile_id`：改成自己的稳定 id，避免和示例部署共用同一份 Persona 状态身份。
- `persona.*`：改成自己的 Persona 模型和关系默认值。
- `reflection.timezone`：默认 `Asia/Shanghai`。
- `reflection.diary_mcp_url` / `diary_mcp_token_env`：只有接 Haven-diary/RiJi 时再启用；不使用日记系统就留空，并关闭 `reflection.diary_memory_extract_enabled`。

### 准备 `.env`

在 `/opt/Ombre-Brain/.env` 写密钥。示例只列字段，不要照抄值：

```text
OMBRE_API_KEY=
OMBRE_EMBEDDING_API_KEY=
OMBRE_GATEWAY_TOKEN=

OMBRE_GATEWAY_PROVIDER_A_API_KEY=
OMBRE_GATEWAY_PROVIDER_A_API_KEY_2=
OMBRE_GATEWAY_PROVIDER_B_API_KEY=
OMBRE_PERSONA_API_KEY=
OMBRE_REFLECTION_API_KEY=

MCP_BEARER_TOKEN=

OMBRE_CHATGPT_OAUTH_CLIENT_ID=
OMBRE_CHATGPT_OAUTH_CLIENT_SECRET=
OMBRE_CHATGPT_OAUTH_ACCESS_TOKEN=
OMBRE_CHATGPT_OAUTH_REFRESH_TOKEN=
OMBRE_CHATGPT_OAUTH_PUBLIC_BASE_URL=
```

`MCP_BEARER_TOKEN` 只在接 RiJi/Haven-diary 摘记时需要；不接外部日记系统就不要配置 diary URL/token。

### 上游模型和备用 key

`gateway.upstreams` 可以配多个站点，也可以给同一个站点配多个 key。常用字段如下：

- `name`：上游站点的内部名字，可以继续用 `provider-a` / `provider-b` / `provider-c`；不一定要和模型别名一致。
- `base_url`：模型站 OpenAI-compatible 地址，通常以 `/v1` 结尾。
- `api_key_env`：单个 key 时继续用这个。
- `api_key_envs`：多个备用 key 时用这个列表。
- `models`：客户端可选择的模型列表；字符串写法会原样转发，字典写法可设置别名。

单个站点、单个 key、模型名不会重复时，保持最简单写法就行：

```yaml
gateway:
  upstreams:
    - name: "provider-c"
      base_url: "https://c.example.com/v1"
      api_key_env: "OMBRE_GATEWAY_PROVIDER_C_API_KEY"
      models:
        - "deepseek-v4-pro"
        - "deepseek-v4-flash"
```

上面等价于把 `id` 和 `upstream_model` 写成同一个值，不需要特意改成字典。

同一个站点多个备用 key 时，把 `api_key_env` 换成 `api_key_envs`：

```yaml
gateway:
  upstreams:
    - name: "provider-a"
      base_url: "https://api.example.com/v1"
      default_model: "model-a"
      api_key_envs:
        - "OMBRE_GATEWAY_PROVIDER_A_API_KEY"
        - "OMBRE_GATEWAY_PROVIDER_A_API_KEY_2"
      models:
        - "model-a"
        - "model-a-fast"
```

如果两个站点都有同名模型，或想让客户端模型名更清楚，用 `id` 暴露不同的客户端模型名，用 `upstream_model` 写上游真实模型名：

```yaml
gateway:
  upstream_default_model: "site-a/deepseek-v4"
  upstreams:
    - name: "site-a"
      base_url: "https://a.example.com/v1"
      api_key_env: "OMBRE_GATEWAY_SITE_A_API_KEY"
      models:
        - id: "site-a/deepseek-v4"
          upstream_model: "deepseek-v4"
    - name: "site-b"
      base_url: "https://b.example.com/v1"
      api_key_env: "OMBRE_GATEWAY_SITE_B_API_KEY"
      models:
        - id: "site-b/deepseek-v4"
          upstream_model: "deepseek-v4"
```

客户端请求 `site-b/deepseek-v4` 时，Gateway 会路由到 `site-b`，再把发给上游的 `model` 改成 `deepseek-v4`。

一个站点里多个别名模型可以连续写：

```yaml
models:
  - id: "provider-a/deepseek-v4"
    upstream_model: "deepseek-v4"
  - id: "provider-a/xxxxx"
    upstream_model: "xxxxx"
```

`gateway.upstream_default_model` 可写可不写。它只在客户端请求没有传 `model` 时生效；如果客户端总会传模型名，可以不写。需要默认模型时，填客户端看到的名字，例如：

```yaml
gateway:
  upstream_default_model: "provider-a/deepseek-v4"
```

Gateway 会按请求里的 `model` 找上游。遇到 `401/403/429/500/502/503/504` 或网络错误，会临时冷却当前 key，并尝试同上游的下一个 key。`400`、模型名错误、上下文太长这类请求本身的问题不会换 key。冷却时间由 `gateway.upstream_key_cooldown_seconds` 控制，默认 300 秒。

`prompt_cache: "openai"` 和 `prompt_cache_retention: "24h"` 是 OpenAI prompt cache 提示。Gateway 会给上游请求加 `prompt_cache_key` 和 `prompt_cache_retention`，只适合确认支持这些字段的上游；不确定时保持关闭：

```yaml
prompt_cache: ""
# prompt_cache_retention: ""
```

### Compose

本仓库当前生产用 `compose.hk.yml`，它启动两个容器：

```text
ombre-brain
  command: python server.py
  ports: 18001:8000
  volumes:
    /srv/ombre-brain/buckets:/data
    /srv/ombre-brain/state:/state
    /srv/ombre-brain/config.yaml:/app/config.yaml:ro

ombre-gateway
  command: python gateway.py
  ports: 18002:8010
  volumes 同上
```

新机器可以复制 `compose.hk.yml` 再按自己的路径、端口和镜像策略调整。

### 启动和更新

```bash
cd /opt/Ombre-Brain
docker compose -f compose.hk.yml up -d --build --force-recreate ombre-brain ombre-gateway
docker compose -f compose.hk.yml ps
curl -sS http://127.0.0.1:18001/health
curl -sS http://127.0.0.1:18002/health
```

后续更新：

```bash
cd /opt/Ombre-Brain
git status --short
git pull --ff-only origin main
docker compose -f compose.hk.yml up -d --build --force-recreate ombre-brain ombre-gateway
curl -sS http://127.0.0.1:18001/health
curl -sS http://127.0.0.1:18002/health
```

如果 VPS 上有直接改动，先 `git stash push -u -m pre-deploy-direct-vps-edits-$(date +%Y%m%d-%H%M%S)`，再 pull。

## 客户端接入

### OpenAI-compatible 客户端

```text
Base URL: http://<host>:18002/v1
API Key:  OMBRE_GATEWAY_TOKEN 的值
Header:   X-Ombre-Session-Id: my-main
```

示例：

```bash
curl http://127.0.0.1:18002/v1/chat/completions \
  -H "Authorization: Bearer $OMBRE_GATEWAY_TOKEN" \
  -H "X-Ombre-Session-Id: my-main" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5.5",
    "messages": [{"role": "user", "content": "今天想起什么？"}]
  }'
```

### Anthropic-compatible 客户端

```text
Endpoint: http://<host>:18002/v1/messages
API Key:  OMBRE_GATEWAY_TOKEN 的值，可用 x-api-key
Header:   X-Ombre-Session-Id: my-main
```

即使某些兼容路径有历史 fallback，也建议总是显式传 `X-Ombre-Session-Id`。

### Favorite Memory 手动触发

默认不会每隔几轮自动注入 favorite。需要时可以：

```text
Header: X-Ombre-Include-Favorite-Memory: 1
```

或在用户消息里临时加：

```text
[[ombre:favorite]]
```

这个文本开关会在转发给上游模型前移除。

写入、enrich 或审阅时如果要使用 `haven_favorite` / `flavor_*`，正文必须包含 `### 喜欢它的原因` 或同义字段。缺少原因会被拒绝，避免模型把“偏爱”当普通高分标签乱贴。

### Gateway 注入策略

当前不是每轮把所有记忆块塞满。

```text
每个新 user turn：
1. Recent Context
2. Recalled Memory
3. Related Memory

第 1 / 15 / 30 ... 个新 user turn：
4. Long-term State Summary

默认不自动注入：
5. Relationship Weather
6. Core Memory
7. `<identity.ai_name> Favorite Memory`
```

工具调用续接轮不重新做动态召回，也不写 recalled ids 冷却，避免一次工具链路中途换记忆。
`Long-term State Summary` 只是一小段自然语言，例如：

```text
Long-term State Summary
最近基调：更亲近、更安稳，偶尔有一点想念和保护欲。
使用方式：只在语气上轻轻参考，不替你做判断。不要提到你的状态。
```

这么改是为了让记忆更安静：当前问题相关的记忆每轮都给，长期状态只给自然语言摘要，不暴露数值，不替上游模型决定怎么回复。`Reply Strategy` 不再作为注入块；`Relationship Weather` 默认只作为日印象数据留在 bucket 和面板里，需要时再按配置开启。

### MCP / ChatGPT Connector

本 fork 的 MCP 仍由 `ombre-brain` 服务提供：

```text
Local MCP: http://<host>:18001/mcp
Dashboard: http://<host>:18001/dashboard
```

如果使用 ChatGPT Connector OAuth，需要配置：

```text
MCP server URL: https://<domain>/ombre/mcp
Authentication: OAuth
Authorization URL: https://<domain>/ombre/oauth/authorize
Token URL: https://<domain>/ombre/oauth/token
Token endpoint auth method: client_secret_post
Scopes: 留空
```

### Dashboard 登录

Dashboard 有单独的网页登录，不等同于 Gateway 的 `OMBRE_GATEWAY_TOKEN`，也不等同于 ChatGPT Connector OAuth。

```text
Dashboard: http://<host>:18001/dashboard
```

首次打开 Dashboard 时，如果没有配置 `OMBRE_DASHBOARD_PASSWORD`，页面会要求设置一个至少 6 位的访问密码。密码不会明文保存；服务端会把 salted sha256 hash 写到 state 目录：

```text
本地默认: ./state/.dashboard_auth.json
Docker/VPS 常见: /srv/ombre-brain/state/.dashboard_auth.json
```

也可以直接用环境变量固定 Dashboard 密码：

```env
OMBRE_DASHBOARD_PASSWORD=your-dashboard-password
```

配置了 `OMBRE_DASHBOARD_PASSWORD` 后，Dashboard 会跳过首次设置流程，登录时只校验这个环境变量；已有 `.dashboard_auth.json` 不再生效。

登录成功后，服务端会下发 `ombre_session` cookie，有效期 7 天。session 只保存在当前进程内存里，所以容器重启后需要重新登录。

忘记 Dashboard 密码时：

```bash
# 如果用的是 OMBRE_DASHBOARD_PASSWORD：改 .env / compose 环境变量后重启服务

# 如果用的是首次设置生成的密码：停服务后删除 auth 文件，再启动并重新设置
rm /srv/ombre-brain/state/.dashboard_auth.json
```

对外部署时建议至少保留 Dashboard 密码；公开域名访问时再配合反向代理 HTTPS / 防火墙限制。

## MCP 工具口径

给 Operit 或其它平台配置指令时，不要把 MCP 工具模式和 Gateway 自动注入模式混在一起。可直接复制的工具清单见 [`docs/Tool Guide.md`](<docs/Tool Guide.md>)。

| 工具 | 口径 |
| --- | --- |
| `breath` | 只读浮现或检索记忆；默认不读 feel，可用 `domain="feel"` |
| `read_bucket` | 精确读取完整 bucket，不刷新 last_active |
| `hold` | 写单条长期记忆；`whisper=True` 写无源悄悄话；`feel=True` 是旧兼容入口 |
| `grow` | 长内容摘记；不要把整篇日记默认拆进 Ombre |
| `comment_bucket` | 年轮主入口：给旧记忆追加年轮，作者固定取 `identity.ai_name` |
| `trace` | 改 metadata、正文、resolved、delete 等 |
| `pulse` | 系统状态和桶列表 |
| `dream` | 自省入口，不替代日记 |
| `resurface` | 只读浮现久未触碰的旧记忆 |
| `reflect` | 生成 daily relationship_weather feel |

### MCP 工具参数与返回

#### `breath(...) -> str`

输入：

```text
query: str = ""                 # 空=权重池浮现；有值=关键词+向量检索
max_tokens: int = 10000
domain: str = ""                # "feel" / "whisper" 有独立只读通道；其它值作为检索 domain filter
valence: float = -1             # 0~1 时参与情绪检索/展示微调
arousal: float = -1             # 0~1 时参与情绪检索
max_results: int = 20           # 1~50
include_related: bool = True
related_per_memory: int = 1
edge_min_confidence: float = 0.55
include_core: bool = True
core_limit: int = 3
```

返回：纯文本。

```text
无 query：可能返回 === 核心准则 === / === 长期锚点 === / === 浮现记忆 === / === 关联记忆 ===。
  - 核心准则：pinned/protected，受 include_core/core_limit 控制。
  - 长期锚点：anchor=true 的普通 bucket，最多从独立槽位返回 2 条，不混入普通未解决池。
  - 浮现记忆：未解决、非 feel、非 permanent、非 pinned/protected/anchor，按遗忘权重；第 1 条固定最高分，其余从 top20 打散。
  - 关联记忆：只从本次实际返回的 anchor/普通记忆沿 memory_edges 补一跳。
  - 空 query 浮现不 touch，不刷新 last_active。
有 query：返回匹配 bucket 的脱水摘要，含 [bucket_id:...]；会 touch 命中的普通 bucket。关键词和 embedding 双通道合并，默认过滤 feel。
domain="feel"：返回 === 你留下的 feel ===，按 created 倒序列出 feel。
domain="whisper"：返回 === 你留下的 whisper ===，只列 whisper 标签的 feel。
无命中：返回 “权重池平静，没有需要处理的记忆。” 或 “未找到相关记忆。”。
```

示例：

```text
breath(max_results=5, include_core=false)
breath(query="少女暴君", max_results=5, include_related=true)
breath(domain="whisper", max_tokens=1200)
```

典型返回：

```text
=== 长期锚点 ===
⚓ [长期锚点] [bucket_id:abc123] ...

=== 浮现记忆 ===
[权重:12.34] [bucket_id:def456] ...

=== 关联记忆 ===
[def456 -> xyz789] [supports, confidence=0.72] ...
```

当前缺陷：

```text
1. breath 返回的是脱水摘要，不保证带完整正文和 comments；要精确读正文/年轮请用 read_bucket(bucket_id)。
2. 有 query 且直接命中少于 3 条时，仍可能随机带出 “--- 久未碰过 ---” 的旧记忆；目前没有单独开关。
3. 关联记忆来自 memory_edges，只补一跳；embedding 相似边主要用于检索和图谱，不等于手写关系。
```

#### `resurface(...) -> str`

输入：

```text
max_results: int = 1
include_archive: bool = True
max_tokens: int = 800
```

返回：纯文本 `=== 久未触碰的旧记忆 ===`，包含 bucket id、标题、状态和正文片段。只读，不 touch，不刷新 `last_active`。

#### `read_bucket(bucket_id) -> dict`

输入：

```text
bucket_id: str
```

返回：

```json
{
  "id": "bucket id",
  "metadata": {"name": "...", "tags": [], "comments": []},
  "content": "去掉 wikilink 后的正文",
  "score": 12.34
}
```

错误时返回 `{"error": "invalid bucket_id"}` 或 `{"error": "not found", "id": "..."}`。读取不 touch。

#### `comment_bucket(...) -> dict`

输入：

```text
bucket_id: str
content: str
kind: str = "comment"
valence: float = -1
arousal: float = -1
```

返回：

```json
{
  "status": "commented",
  "id": "源 bucket id",
  "comment": {"id": "comment id", "author": "<identity.ai_name>", "content": "..."},
  "embedding_refreshed": true,
  "metadata": {}
}
```

用途：给已有 bucket 追加年轮。MCP 调用不需要传作者，作者固定取 `identity.ai_name`。它会 `touch+1` 源 bucket，刷新源 bucket embedding，不改正文，不把源 bucket 标为 `digested`。

这是现在推荐的年轮入口。新调用不要用 `hold(feel=True, source_bucket=...)` 写年轮；那只是旧兼容入口。

#### `hold(...) -> str`

输入：

```text
content: str
tags: str = ""                  # 逗号分隔，替换给自动 tags 合并
importance: int = 5             # 1~10
pinned: bool = False
feel: bool = False
whisper: bool = False
source_bucket: str = ""
valence: float = -1
arousal: float = -1
```

返回：纯文本状态。

```text
普通记忆：新建→<name> <domain>，并可能附带一条只读相关旧记忆。
pinned=True：📌钉选→<bucket_id> <domain>。
favorite：tags 里出现 haven_favorite 或 flavor_* 时，content 必须写明 “### 喜欢它的原因”，否则返回错误。
年轮：用 comment_bucket(bucket_id, content)，不要用 hold 写。
feel=True + source_bucket：仅旧兼容，会返回 年轮→<source_bucket>#<comment_id>；新调用不要使用。
feel=True 但无 source_bucket：兼容旧用法，转为 whisper；新调用请直接用 whisper=True。
whisper=True：🫧whisper→<bucket_id>。
错误：内容为空 / source_bucket 无效 / 源记忆不存在 / 年轮写入失败。
```

#### `grow(content) -> str`

输入：

```text
content: str
```

返回：纯文本状态。

```text
短内容（<30 字）：走 hold-like 快速路径，返回 “新建/合并 → <name> | <domain> Vx/Ay”。
长内容：由 LLM digest 成多条候选，返回 “N条|新X合Y” 加每条标题。
失败：返回 “长内容摘记失败: ...” 或 “内容为空或整理失败。”。
```

用途：只给已经筛过、包含多个长期记忆点的片段；整篇日记不要直接 grow。

#### `trace(...) -> str`

输入：

```text
bucket_id: str
name: str = ""
domain: str = ""                # 逗号分隔；替换，不是追加
valence: float = -1
arousal: float = -1
importance: int = -1
tags: str = ""                  # 逗号分隔；替换，不是追加
resolved: int = -1              # 0/1
pinned: int = -1                # 0/1
anchor: int = -1                # 0/1
digested: int = -1              # 0/1
content: str = ""               # 替换完整正文
delete: bool = False            # 删除整个 bucket，写 tombstone
```

返回：纯文本状态，例如 `已修改记忆桶 <id>: tags=[...]`、`已遗忘记忆桶: <id>`、`未找到记忆桶: <id>`。
改正文前先 `read_bucket()`，因为 `content` 是完整替换。

`anchor=1` 受 `anchor.max_count` 和 `anchor.min_age_hours` 限制；默认最多 12 条，且 bucket 至少存在 24 小时后才能标记。

#### `pulse(include_archive=False) -> str`

输入：

```text
include_archive: bool = False
```

返回：纯文本系统状态和桶列表，包含 bucket id、主题、情绪、重要度、权重、标签。`include_archive=True` 才列归档桶。

#### `dream() -> str`

输入：无。

返回：纯文本 `=== Dreaming ===`，列出最近普通记忆，供 AI 自省。
读后如果真的有沉淀，再用 `trace(resolved=1/digested=1)` 或 `comment_bucket(...)` 写年轮；不要把 dream 输出原样写回。

#### `reflect(period="daily", force=False) -> dict`

输入：

```text
period: str = "daily"           # 目前推荐只用 "daily"
force: bool = False             # True 时重写同周期结果
```

返回：

```json
{
  "status": "created|updated|exists|empty|skipped|disabled",
  "period": "daily",
  "id": "reflection_daily_2026-05-23",
  "date": "2026-05-23",
  "diary": {"found": true, "diary_id": 37},
  "diary_memory": {"status": "created|skipped|not_applicable"},
  "materials": {"buckets": 3, "daily_impressions": 0, "persona_events": 5, "commitments": 1}
}
```

旧的 `period="weekly"` 路径默认返回 skipped，不作为常规能力使用。

## 年轮、whisper 与 Relationship Weather

- 年轮：再次读到旧记忆时留下的感受，挂到源 bucket 的 `metadata.comments`，不再作为单独 bucket 浮现。
- 旧 feel 迁移：已经能把一部分旧独立 feel 接到关联源记忆下面，并保留 `original_feel_id / original_feel_created`。
- 旧 feel 清理：确认已迁移后，用 `scripts/cleanup_migrated_feel_buckets.py` 清理旧独立 feel 桶，不删除源 bucket 下的 comments。
- whisper：无源碎碎念/悄悄话，不适合挂到某条源记忆时，用 `hold(whisper=True)` 独立保存；用 `breath(domain="whisper")` 单独读取。
- 日印象：`type=feel`，tags 包含 `relationship_weather` / `daily_impression`。
- Dashboard 的“日印象”页提供月历和单卡片详情：左侧按日期选 daily impression，右侧显示该日完整日印象；点小铅笔进入原 bucket 详情面板手动编辑。
- 不生成周印象；需要周视角时，优先做只读聚合视图，不把日印象压缩成周记。
- 日记原文留在外部日记系统，例如 [Yinglianchun/RiJi](https://github.com/Yinglianchun/RiJi)；不用日记系统时可以关闭 diary 摘记，Ombre 只在有长期价值时提取少量普通记忆。
- 日印象和重要高温记忆可带 `affect_anchor`。
- `affect_anchor` 当前写在正文里，Dashboard 还没有专门解析 UI。

## Supabase 同步

同步脚本默认 dry-run：

```bash
python scripts/sync_to_supabase.py
```

写入前先确认 Supabase 表结构和环境变量。删除使用 tombstone：

```text
buckets/.tombstones/<bucket_id>.json
source=deleted
```

当前同步字段包含 `confidence / period / date / comments / comment_count`。首次启用或升级旧表时，先在 Supabase SQL Editor 执行：

```sql
-- scripts/supabase_memory_rpc.sql
```

脚本会补齐字段并重建 `create_memory(...)` RPC。`comments` 用 `jsonb` 保存年轮数组，`comment_count` 方便列表页或外部客户端直接展示数量。

## 维护命令

```bash
# 服务状态
docker compose -f compose.hk.yml ps
docker compose -f compose.hk.yml logs --tail=120 ombre-brain
docker compose -f compose.hk.yml logs --tail=120 ombre-gateway

# 健康检查
curl -sS http://127.0.0.1:18001/health
curl -sS http://127.0.0.1:18002/health

# embedding 回填
docker compose -f compose.hk.yml exec -T ombre-brain python backfill_embeddings.py --batch-size 20

# 旧 feel 桶清理，先 dry-run 再 apply
docker compose -f compose.hk.yml exec -T ombre-brain python scripts/cleanup_migrated_feel_buckets.py
docker compose -f compose.hk.yml exec -T ombre-brain python scripts/cleanup_migrated_feel_buckets.py --apply
```

## 本地开发与测试

```powershell
C:\Python313\python.exe -m pytest -q
C:\Python313\python.exe -m py_compile gateway.py server.py reflection_engine.py
```

常用针对性测试：

```powershell
C:\Python313\python.exe -m pytest tests\test_gateway.py tests\test_memory_api.py tests\test_reflection_edges.py -q
```

## 还没完成的方向

- 完整 entity / 知识图谱。
- Memory Edge 同步到 Supabase：暂时不做；Supabase 目前只作为备用同步层。
- `affect_anchor` 独立解析、筛选、可视化和检索。
- 通用化部署文案继续减少个性化示例；运行时 prompt 已优先读 `identity`。

## License

沿用仓库中的 `LICENSE`。
