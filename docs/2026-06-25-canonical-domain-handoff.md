# Canonical Domain / 记忆字段重构交接

日期：2026-06-25
范围：Ombre-Brain 记忆元数据、写入 prompt、Dashboard 筛选、Gateway Reading Note 后续设计
状态：设计交接；本侧聊没有改代码

## 一句话结论

现在的 `domain` 太脏了，已经同时承担了内容主域、场景、状态、类型、特殊标记和目录分组。后续不要继续把旧 `domain` 当主导航。

建议新增一套更清楚的字段：

```yaml
canonical_domain: project_code
kind: event
status: active
tags: ["gateway", "relationship_tone"]
legacy_domain: ["AI", "人际", "未解决"]
```

核心原则：

```text
canonical_domain 只回答：这条主要讲什么。
kind 只回答：它是什么类型的记忆。
status 只回答：它现在处于什么生命周期。
tags 只回答：还有哪些补充线索。
scene 不写进记忆，由 Gateway 每轮根据 query/context_mode 判断。
```

## Canonical Domain 的边界：不做 admission gate

`canonical_domain` 不能决定一条记忆有没有资格进入候选或可见注入。资格必须先由 evidence 决定。

固定规则：

```text
evidence 决定有没有资格进来
scene + canonical_domain/kind/status 决定怎么读它
domain 不能否决 exact/entity/source_record/strong semantic/strong rerank
domain 只能影响 explicit_recall / background / silent_tone / ignore
```

也就是说：

```text
exact/entity/source_record/strong semantic/strong rerank -> 先按证据过门
canonical_domain/kind/status -> 之后才参与 Reading Note 的读法分流
```

例子：

```text
scene=memory_lookup
query 明确问“海边神庙那次”
即使 canonical_domain=relationship 或 inner_state，也不能因为当前场景不是恋爱就挡掉。

scene=task
query 问“今天代码改得怎么样”
relationship 记忆如果只是弱相关，可以变成 silent_tone 或 ignore，但不是靠 domain 直接判死刑。
```

## 当前问题

`domain` 在现有系统里不是严格分类，而是自动打标留下的主题标签。

已确认代码位置：

```text
D:\Ombre-Brain\dehydrator.py
D:\Ombre-Brain\import_memory.py
D:\Ombre-Brain\bucket_manager.py
D:\Ombre-Brain\dashboard.html
D:\Ombre-Brain\gateway.py
```

当前写入 prompt 允许模型从这些主题中选 1-2 个：

```text
日常 / 人际 / 成长 / 身心 / 兴趣 / 数字 / 事务 / 内心
以及饮食、恋爱、工作、心理、编程、AI、自省等细分词
```

问题是代码不会强制这些词只表示“内容主域”。结果 `domain` 现在混进了：

```text
内容主域：恋爱、编程、AI、自省
场景：写代码、亲密、安慰
类型：feel、日印象、profile、life_fact
状态：未解决、已消化、归档
标记：anchor、favorite、source_record
```

例如“第一行代码的浪漫”如果只靠旧 `domain`，会被撕成两半：

```text
它既像代码，也像关系。
如果 domain 表示场景，就不知道该归哪边。
```

更好的表达是：

```yaml
canonical_domain: project_code
kind: event
status: active
tags: ["relationship_tone", "shared_memory"]
```

或者：

```yaml
canonical_domain: relationship
kind: affect_anchor
status: active
tags: ["project_code", "shared_memory"]
```

主域只能选一个，副信息走 `tags/kind/status`。

## 字段方案

### canonical_domain

回答“这条主要讲什么”，只能一个。

建议初版枚举：

```text
project_code      代码、Ombre、Gateway、MCP、调试
ai_tools          模型、工具、客户端、API、AI 平台
relationship      恋爱、关系确认、称呼、共同短语
intimacy          亲密、身体、欲望、具身亲密
inner_state       内心、自省、情绪状态、日印象
daily_life        生活、饮食、作息、日常事件
social            人际、社交、学校、朋友、群聊
study_work        学业、论文、求职、工作
craft_body        手工、硬件、身体项目、实体制作
```

暂不建议把 `profile` 放进 `canonical_domain`。画像不是内容主域，更适合放在 `kind=profile_fact`。

### kind

回答“它是什么类型的记忆”。

建议初版枚举：

```text
event
preference
profile_fact
reflection
affect_anchor
daily_impression
source_record
raw_import
relationship_weather
```

`profile / life_fact / Feel / 日印象` 应该主要进入这里，不该挤在 domain 里。

### status

回答“它现在处于什么生命周期”。

建议初版枚举：

```text
active
unresolved
digested
archived
protected
```

注意：有些 status 可以从现有字段推出来：

```text
archived: 看 type/path
digested: 看 digested
protected: 看 protected/pinned
unresolved: 看 resolved=false 或专门字段
```

因此可以先在 API/UI 层生成 `status_view`，不急着把所有状态都写回 frontmatter，避免状态不同步。

### tags

补充线索，可多个。

例子：

```yaml
tags: ["gateway", "relationship_tone", "shared_memory"]
```

tags 可以包含跨域线索，但不应该取代主域。

### legacy_domain

旧 `domain` 不要删，先降级保存：

```yaml
domain: ["project_code"]
legacy_domain: ["AI", "人际", "未解决"]
canonical_domain: project_code
kind: event
status: unresolved
```

等新字段跑稳定，再决定旧字段是否清理。

## Prompt 改法

需要改写入/导入/分析 prompt，让新记忆直接产出新字段。

建议 prompt 加这段规则：

```text
canonical_domain describes what the memory is mainly about, not when it should be used.
Choose exactly one canonical_domain.
Do not put scene, lifecycle, confidence, anchor/favorite/profile labels into canonical_domain.
Use kind/status/tags for those.
Scene is decided at runtime by Gateway from the current query/context_mode, and must not be stored in memory metadata.
```

输出结构建议：

```json
{
  "canonical_domain": "project_code",
  "kind": "event",
  "status": "active",
  "tags": ["gateway", "relationship_tone"],
  "domain": ["project_code"]
}
```

`domain` 暂时保留兼容，建议新写入时等于 `[canonical_domain]`。

## Dashboard 筛选方案

前端主筛选不要再只铺一排旧 domain chip。建议分四组。

### 状态

```text
全部 / 活跃 / 未解决 / 已消化 / 归档 / 受保护
```

### 主域

```text
代码项目 / AI工具 / 关系 / 亲密 / 内心 / 日常 / 社交 / 学业工作 / 手工身体
```

### 类型

```text
事件 / 偏好 / 画像事实 / 反思 / 情绪锚点 / 日印象 / 原文记录 / 关系天气
```

### 标记

```text
Anchor / 钉选 / favorite / source_record / self_anchor
```

旧 `domain` 可以放到“旧标签”折叠区，默认不显示。

## Gateway 用法

Gateway 不应该继续直接信旧 `domain`。后续 Reading Note 应该根据：

```text
context_mode + canonical_domain + kind + status + exact/entity/source evidence
```

示例规则：

```text
scene = task
allow: project_code, ai_tools, study_work
silent: relationship, inner_state
block: intimacy unless explicitly asked

scene = intimate
allow: relationship, intimacy
silent: inner_state, daily_life
block/background: project_code unless query mentions it

scene = emotional_support
allow: inner_state, relationship, daily_life
silent: profile_fact
block/background: project_code unless relevant

scene = memory_lookup
entity/exact/source_record 优先，不按 domain 硬挡
```

重点：`canonical_domain` 不是 scene。它只表示记忆主要内容。scene 每轮由 Gateway 判断。

## 批量迁移功能

批量迁移值得做，但必须先 preview，不要破坏性清洗。

最小功能：

```text
1. 多选 bucket
2. 批量设置 canonical_domain
3. 批量设置 kind
4. 批量设置 status
5. 批量追加 / 删除 tags
6. 迁移前 preview：显示 old domain -> new fields
7. 保留 legacy_domain，不直接删旧值
```

迁移时推荐写法：

```yaml
domain: ["project_code"]
legacy_domain: ["AI", "人际", "未解决"]
canonical_domain: project_code
kind: event
status: unresolved
```

## 推荐推进顺序

### Phase 1：新写入先变干净

改 `dehydrator.py` / `import_memory.py` 里的分析 prompt，让新桶输出：

```text
canonical_domain
kind
status
tags
domain=[canonical_domain]
```

同时 `bucket_manager.py` 确认这些字段能进入 metadata。

### Phase 2：API/UI 兼容读取

前端优先读取新字段：

```text
canonical_domain -> domain fallback
kind -> type/layer fallback
status_view -> existing flags/path fallback
legacy_domain -> old domain display only
```

### Phase 3：批量迁移 dry-run

先做本地推断，不写文件：

```text
old domain/tags/type/path/name/content
-> suggested canonical_domain/kind/status
```

输出 preview 给人手确认。

### Phase 4：Gateway Reading Note 接入

在 Reading Note 里使用新字段：

```text
explicit_recall / silent_tone / background / ignore
```

不要让旧 `domain` 再决定是否直接注入。

## 暂不建议

```text
不要批量重写老记忆正文。
不要让小模型批量总结老桶。
不要直接删除旧 domain。
不要把 profile 做成 canonical_domain 杂物筐。
不要把 scene 写死进记忆元数据。
不要在 memory_lookup 场景用 domain 硬挡 exact/entity/source_record。
```

## 给下个窗口的重点

这次不是要“修一下 domain 名字”，而是把混在一起的四种问题拆开：

```text
内容主域 -> canonical_domain
记忆类型 -> kind
生命周期 -> status/status_view
特殊标记 -> flags/tags
运行场景 -> Gateway context_mode
```

旧 `domain` 已经像一排混合标签：有内容、有状态、有类型、有标记。继续拿它当主筛选，只会越用越乱。
