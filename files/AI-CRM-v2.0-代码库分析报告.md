# AI-CRM v2.0 代码库分析报告

> 分析对象：`AI-CRM-v2.0`（SeaChat AI 职业咨询 CRM）
> 分析范围：`app/`、`scripts/`、`tests/`、`run.py`、`app/db/schema.sql`、`app/static/js/`
> 报告口径：方法包含 Python 函数/类方法、Flask 视图函数、前端 JS 函数、脚本与测试函数；数据库包含 SQLite 与 Chroma 向量库。

---

## 1. 项目总览

### 1.1 技术栈与运行形态

| 项 | 内容 |
|---|---|
| 后端框架 | Flask 3（应用工厂 `create_app`） |
| 业务数据库 | SQLite（`./data/crm.db`，WAL 模式） |
| 向量数据库 | Chroma 本地持久化客户端（`./data/chroma`） |
| 对话模型 | DeepSeek `deepseek-chat`（OpenAI 兼容协议） |
| 向量模型 | 百炼 `text-embedding-v4`，1024 维，批量 ≤ 10 |
| 联网搜索 | Tavily Search |
| 前端 | 原生 HTML + Jinja2 + 原生 JS（无构建链） |
| 运行方式 | 单进程多线程（开发 `app.run`，生产 `waitress`） |
| 离线能力 | 无密钥时 LLM 退化为 `EchoClient`，Embedding 退化为本地哈希向量 |

### 1.2 分层架构

| 层 | 目录 | 职责 |
|---|---|---|
| 接入层 | `app/api/` | HTTP 路由、参数校验、鉴权、SSE 输出、页面渲染 |
| 编排层 | `app/core/` | 会话编排、意图识别、RAG 检索、工具/Agent 循环、画像抽取、知识入库、限流、错误定义 |
| 数据访问层 | `app/db/` | SQLite 连接管理、各表 CRUD |
| 向量层 | `app/vector/` | Chroma 客户端、collection 管理、向量写入/删除/检索 |
| 模型层 | `app/llm/` | LLM 客户端与 Embedding 客户端（含离线降级） |
| 配置层 | `app/config.py` | `.env` 读取、路径解析、结构化 JSON 日志 |
| 前端 | `app/templates/`、`app/static/` | 用户端聊天页、管理端客户页/知识库页 |

### 1.3 文件职责速览

| 文件 | 职责 |
|---|---|
| `run.py` | 应用启动入口，按环境选择 Flask 开发服务器或 waitress |
| `app/__init__.py` | 应用工厂、蓝图注册、统一异常处理、JSON 字符集 |
| `app/config.py` | 单例配置、环境变量解析、日志格式化 |
| `app/db/connection.py` | 读写/只读连接、PRAGMA、建表 |
| `app/db/customers.py` | `customers` 表读写、画像合并、阶段流转 |
| `app/db/conversations.py` | `conversations`、`messages` 表读写 |
| `app/db/kb.py` | `kb_documents` 表读写 |
| `app/vector/store.py` | Chroma 三个 collection 的增删查与统计 |
| `app/llm/client.py` | DeepSeek 客户端、离线 Echo 客户端、单例管理 |
| `app/llm/embedding.py` | 百炼 Embedding、本地哈希向量、单例管理 |
| `app/core/pipeline.py` | 一次对话的完整编排与 SSE 事件生成 |
| `app/core/intent.py` | 意图识别（LLM 强制工具调用 + 关键词降级） |
| `app/core/agents.py` | 4 个工具定义、SQL 守卫、工具执行、Agent 循环 |
| `app/core/profiler.py` | 后台画像抽取（LLM + 正则）与合并 |
| `app/core/retriever.py` | 向量检索、上下文与引用组装 |
| `app/core/knowledge.py` | 切片、知识入库、删除、重建索引 |
| `app/core/docparse.py` | PDF/Word/TXT/Markdown 文本抽取与清洗 |
| `app/core/strategies.py` | 画像摘要、按阶段生成回答策略 |
| `app/core/prompts.py` | 全部 Prompt 常量与工具 Schema |
| `app/core/ratelimit.py` | 进程内滑动窗口限流 |
| `app/core/errors.py` | 统一错误类型与错误响应体 |
| `app/api/auth.py` | 管理端登录、Session、接口/页面鉴权 |
| `app/api/chat.py` | `/api/session`、`/api/chat`、`/api/customer-card` |
| `app/api/customers.py` | 管理端客户接口 |
| `app/api/kb.py` | 知识库接口（列表/新增/上传/删除/重建） |
| `app/api/health.py` | 依赖健康检查 |
| `app/api/pages.py` | 用户端与管理端页面路由 |

---

## 2. 数据库总览

### 2.1 存储清单与职责

| 存储 | 类型与位置 | 在项目中的职责 | 是否可作为真相源 |
|---|---|---|---|
| SQLite `crm.db` | 关系型，`SQLITE_PATH`，默认 `./data/crm.db` | 保存客户、会话、消息、知识条目元数据、意图字典；承载业务状态与统计 | 是，业务与知识元数据的唯一真相源 |
| Chroma | 向量库，`CHROMA_PATH`，默认 `./data/chroma` | 保存知识切片及其向量，提供余弦相似度语义检索 | 否，派生索引，可由 `kb_documents` 重建 |
| 进程内内存 | `_buckets`、模块级单例、`_rebuild_lock` | 限流计数、LLM/Embedder/Chroma 客户端缓存、重建互斥 | 否，进程重启即丢失 |

### 2.2 SQLite 的作用与实现要点

- **职责**：CRM 业务主库。客户画像、会话与消息、知识条目正文、意图字典全部落在这里；管理端列表/详情/统计、对话落库、画像合并、SQL Agent 查询均依赖它。
- **连接参数**（`app/db/connection.py`）：
  - `row_factory = sqlite3.Row`，按列名取值。
  - `PRAGMA busy_timeout=5000`，写锁等待 5 秒。
  - `PRAGMA foreign_keys=ON`，启用外键级联。
  - 读写连接：`journal_mode=WAL`、`synchronous=NORMAL`。
  - 只读连接：URI `mode=ro`，并设置 `PRAGMA query_only=ON`，供 SQL Agent 使用。
- **事务**：`write_conn()` 正常结束自动 `commit`，异常 `rollback`。
- **时间约定**：所有时间字段为本地时间字符串 `YYYY-MM-DD HH:MM:SS`，由 `datetime('now','localtime')` 生成。
- **建表**：`init_db()` 执行 `app/db/schema.sql`（幂等），返回五张表行数。

### 2.3 Chroma 的作用与实现要点

- **职责**：知识库的语义检索索引。把 `kb_documents.content` 切片、向量化后存入，回答阶段按用户问题做余弦相似度召回。
- **三个 collection**：

| collection 键 | Chroma 名称 | 用途 |
|---|---|---|
| `market` | `kb_market` | 行业/岗位/前景类知识（职业咨询） |
| `course` | `kb_course` | 课程/价格/师资/报名类知识（课程咨询） |
| `general` | `kb_general` | 机构介绍等通用知识，供工具检索 |
| `all` | — | 仅作为重建参数，表示三个集合全部重建 |

- **距离度量**：`metadata={"hnsw:space": "cosine"}`。
- **切片**：`RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=80)`，过滤长度 < 15 的切片。
- **Chunk 主键**：`{doc_id}::{chunk_index}`，保证重复写入幂等。
- **检索**：`n_results=k`，`score = 1 - cosine_distance`，结果按 score 降序。

### 2.4 两库协同与一致性

1. 写入知识时先完成全部 Embedding，再写向量，最后写 `kb_documents` 元数据。
2. 采用「临时前缀 chunk → 删除旧 chunk → 写正式 doc_id → 删除临时 chunk」的顺序，避免更新中途出现空档。
3. 向量写入或元数据写入失败时，把已有 `kb_documents.enabled` 置 0，避免「元数据已更新、向量仍旧」。
4. `kb_documents` 是真相源；`rebuild` 会清空目标 collection 后按表中的启用文档重新切片、向量化、写回。
5. 离线向量与线上 Embedding 不兼容，切换 `DASHSCOPE_API_KEY` 后必须重建索引。

### 2.5 非持久化状态

| 状态 | 位置 | 说明 |
|---|---|---|
| 限流桶 `_buckets` | `app/core/ratelimit.py` | 滑动窗口计数，单进程有效 |
| LLM 单例 `_llm` | `app/llm/client.py` | 进程内缓存客户端 |
| Embedder 单例 `_embedder` | `app/llm/embedding.py` | 进程内缓存客户端 |
| Chroma 客户端与 collection 句柄 | `app/vector/store.py` | 加锁复用，路径变化时重建 |
| 意图字典缓存 `_cache` | `app/core/intent.py` | 首次读取后缓存，可 `reset_cache` |
| 重建互斥锁 `_rebuild_lock` | `app/core/knowledge.py` | 防止并发重建 |

---

## 3. 数据表与字段字典

### 3.1 `customers`（客户主表）

| 字段 | 类型/约束 | 含义 |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | 客户主键，会话与消息通过它关联 |
| `phone` | TEXT NOT NULL UNIQUE | 手机号，业务身份标识；接口校验 `^1[3-9]\d{9}$`；列表与详情默认脱敏 |
| `name` | TEXT | 客户称呼，画像抽取时写入，最长 40 字符 |
| `stage` | TEXT NOT NULL DEFAULT `'new'` | 客户阶段：`new` 新客、`contacted` 已接触、`qualified` 有明确意向、`won` 已成交、`lost` 已流失 |
| `intent_level` | TEXT NOT NULL DEFAULT `'cold'` | 意向等级：`cold` 仅浏览、`warm` 有目标无预算/时间、`hot` 目标且预算或时间窗明确 |
| `tags` | TEXT NOT NULL DEFAULT `'[]'` | JSON 字符串数组，人工标签；接口限制最多 12 个、单个 ≤ 12 字 |
| `profile` | TEXT NOT NULL DEFAULT `'{}'` | JSON 对象，客户画像，字段见 3.7 白名单 |
| `note` | TEXT | 顾问备注，接口截断至 500 字符 |
| `created_at` | TEXT NOT NULL DEFAULT 本地时间 | 创建时间 |
| `last_active_at` | TEXT NOT NULL DEFAULT 本地时间 | 最近活跃时间；`touch`、画像合并、字段更新时刷新；列表按其倒序 |

索引：

| 索引 | 字段 | 用途 |
|---|---|---|
| `idx_customers_active` | `last_active_at DESC` | 客户列表按活跃时间排序 |
| `idx_customers_stage` | `stage` | 按阶段筛选与统计 |

阶段流转规则：

| 流转 | 触发条件 |
|---|---|
| `new → contacted` | 客户产生 ≥ 3 条 user 消息（`auto_stage_by_rounds`） |
| `new/contacted → qualified` | `intent_level=hot` 且 `profile.goal` 非空（`merge_profile`） |
| `won` / `lost` | 管理端人工标记 |

### 3.2 `conversations`（会话表）

| 字段 | 类型/约束 | 含义 |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | 会话主键 |
| `customer_id` | INTEGER NOT NULL，外键 `customers.id`，`ON DELETE CASCADE` | 所属客户 |
| `title` | TEXT | 会话标题；首条用户消息前 20 字符自动写入，已有值不覆盖 |
| `created_at` | TEXT NOT NULL DEFAULT 本地时间 | 创建时间 |
| `updated_at` | TEXT NOT NULL DEFAULT 本地时间 | 最近更新时间；新增消息或 `touch` 时刷新；用于会话复用与排序 |

索引：

| 索引 | 字段 | 用途 |
|---|---|---|
| `idx_conv_customer` | `customer_id` | 查询某客户的会话列表 |

### 3.3 `messages`（消息表）

| 字段 | 类型/约束 | 含义 |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | 消息主键，按自增顺序即时间顺序 |
| `conversation_id` | INTEGER NOT NULL，外键 `conversations.id`，`ON DELETE CASCADE` | 所属会话 |
| `role` | TEXT NOT NULL | 消息角色：`user` 客户、`assistant` AI 顾问、`system` 系统（Schema 允许，业务只写前两种） |
| `content` | TEXT NOT NULL | 消息正文；用户消息 ≤ 2000 字符 |
| `intent_code` | INTEGER | 意图编码 `0~4`，仅助手消息记录；问候消息固定 0 |
| `route` | TEXT | 本次回答路由：`rag_market`、`rag_course`、`web_search`、`sql`、`direct`、`rag_miss` |
| `citations` | TEXT NOT NULL DEFAULT `'[]'` | JSON 数组，引用来源（知识条目或网页） |
| `latency_ms` | INTEGER | 本次回答耗时（毫秒） |
| `tokens_in` | INTEGER | 输入 token 数 |
| `tokens_out` | INTEGER | 输出 token 数 |
| `created_at` | TEXT NOT NULL DEFAULT 本地时间 | 创建时间 |

索引：

| 索引 | 字段 | 用途 |
|---|---|---|
| `idx_msg_conv` | `conversation_id, id` | 按会话顺序读取消息 |

### 3.4 `kb_documents`（知识条目表）

| 字段 | 类型/约束 | 含义 |
|---|---|---|
| `id` | TEXT PRIMARY KEY | `doc_id`，与 Chroma metadata 中的 `doc_id` 一致；规则：字母/数字/下划线/短横线/点，≤ 80 字符 |
| `collection` | TEXT NOT NULL | 所属集合：`market`、`course`、`general` |
| `title` | TEXT NOT NULL | 标题；缺省取正文前 20 字符 |
| `content` | TEXT NOT NULL | 知识正文，单条 ≤ 20000 字符；超长文件会被拆成多条 |
| `source` | TEXT | 来源标记：`manual`、`cms`、`import`，上传时为 `file:pdf`、`file:docx`、`file:txt`、`file:md` 等 |
| `chunk_count` | INTEGER NOT NULL DEFAULT 0 | 当前条目在 Chroma 中的切片数量 |
| `enabled` | INTEGER NOT NULL DEFAULT 1 | 是否启用：1 启用、0 停用；停用不参与检索与重建 |
| `created_at` | TEXT NOT NULL DEFAULT 本地时间 | 创建时间 |
| `updated_at` | TEXT NOT NULL DEFAULT 本地时间 | 最近更新时间；upsert、chunk_count 更新时刷新 |

索引：

| 索引 | 字段 | 用途 |
|---|---|---|
| `idx_kb_collection` | `collection, updated_at DESC` | 按集合列出知识条目 |

### 3.5 `intents`（意图字典表）

| 字段 | 类型/约束 | 含义 |
|---|---|---|
| `code` | INTEGER PRIMARY KEY | 意图编码 0~4 |
| `name` | TEXT NOT NULL | 意图名称，用于前端与日志展示 |
| `description` | TEXT NOT NULL | 意图说明，供运营理解分类边界 |
| `route` | TEXT NOT NULL | 该意图对应的处理路由 |
| `enabled` | INTEGER NOT NULL DEFAULT 1 | 是否启用；`_load_intents` 只读取启用项 |

内置数据：

| code | name | route | 说明 |
|---|---|---|---|
| 0 | 闲聊/其他 | `direct` | 问候、感谢、无法归类 |
| 1 | 职业咨询 | `rag_market` | 转行可行性、岗位要求、学习路径、行业前景 |
| 2 | 课程咨询 | `rag_course` | 课程内容、价格、周期、师资、报名、就业服务 |
| 3 | 实时资讯 | `web_search` | 最新政策、薪资行情、公司动态等需联网信息 |
| 4 | 数据查询 | `sql` | 查询客户记录或系统统计 |

### 3.6 Chroma 集合与切片结构

| 元素 | 含义 |
|---|---|
| `ids` | `{doc_id}::{chunk_index}`，如 `course_pricing::0` |
| `documents` | 切片正文 |
| `embeddings` | 1024 维向量（线上为 text-embedding-v4，离线为本地哈希向量） |
| `metadatas.doc_id` | 对应 `kb_documents.id` |
| `metadatas.collection` | 冗余集合名，便于跨集合识别 |
| `metadatas.title` | 标题，引用展示用 |
| `metadatas.chunk_index` | 切片序号，从 0 开始 |
| `metadatas.updated_at` | 索引写入时间 |

### 3.7 JSON 字段子结构

`customers.profile` 白名单（其余键在 `merge_profile` 中直接丢弃）：

| 键 | 含义 | 约束 |
|---|---|---|
| `current_role` | 当前身份/岗位 | 字符串，≤ 120 字符 |
| `years_exp` | 工作年限 | 字符串，≤ 120 字符 |
| `goal` | 转行/学习目标 | 字符串，≤ 120 字符；非空是 `qualified` 的条件之一 |
| `budget` | 预算范围 | 字符串，≤ 120 字符 |
| `time_window` | 可投入时间/期望周期 | 字符串，≤ 120 字符 |
| `city` | 城市 | 字符串，≤ 120 字符 |
| `concerns` | 顾虑列表 | 字符串数组，最多 6 项，每项 ≤ 20 字符 |

`customers.tags`：字符串数组，接口层限制最多 12 个、每个 ≤ 12 字。

`messages.citations` 数组元素：

| 键 | 含义 |
|---|---|
| `doc_id` | 知识条目 ID，网页来源时存 URL |
| `title` | 来源标题 |
| `score` | 相似度分数，网页来源为 null |
| `snippet` | 摘要片段（知识 ≤ 60 字符，工具回填 ≤ 60 字符） |

### 3.8 表间关系

```text
customers (1) ──< conversations (1) ──< messages
     │
     └── profile / tags 为 JSON 文本字段

kb_documents (id) ──1:1──> Chroma chunk metadata.doc_id
intents 独立字典表，与业务表无外键关系
```

删除客户时，`conversations` 与 `messages` 通过外键级联物理删除。

---

## 4. 方法清单（按模块）

### 4.1 应用入口与配置

#### `run.py`

| 方法 | 作用 |
|---|---|
| 模块级 `app = create_app()` | 创建 Flask 应用实例 |
| `__main__` 分支 | 开发环境用 `app.run(debug, threaded, use_reloader=False)`；生产优先 `waitress.serve(threads=8)`，未安装则回退 Flask |

#### `app/__init__.py`

| 方法 | 作用 |
|---|---|
| `_register_blueprints(app)` | 注册 `pages`、`chat`、`customers`、`kb`、`health` 五个蓝图 |
| `_register_error_handlers(app)` | 注册统一异常处理 |
| `_api_error(exc)` | 将 `ApiError` 转为 `{"error": {...}}` 与对应状态码 |
| `_http_error(exc)` | 将 404/405/413 等 HTTP 异常转为 JSON 错误；413 映射为文件过大提示 |
| `_unhandled(exc)` | 兜底异常：API 路径返回 500 JSON，页面返回文本 |
| `create_app(test_config)` | 应用工厂：覆盖测试配置、重置单例、配置日志、设置 Cookie/上传上限、建表、注册蓝图与错误处理；未配置密钥时打印降级告警 |
| `_json_charset(response)` | `after_request` 钩子，为 JSON 响应补充 `charset=utf-8` |

#### `app/config.py`

| 方法 | 作用 |
|---|---|
| `_as_bool(value, default)` | 字符串转布尔，支持 `1/true/yes/on` |
| `_as_int(value, default)` | 字符串转整数，失败返回默认值 |
| `_as_float(value, default)` | 字符串转浮点数，失败返回默认值 |
| `_as_path(value, default)` | 相对路径按项目根目录解析为绝对路径 |
| `Config.__init__()` | 读取 `.env`（不覆盖已有环境变量）并调用 `reload` |
| `Config.reload()` | 重新加载全部配置项并确保目录存在 |
| `Config.ensure_dirs()` | 创建 SQLite 与 Chroma 目录 |
| `Config.llm_configured`（property） | 是否配置 DeepSeek Key |
| `Config.embedding_configured`（property） | 是否配置 DashScope Key |
| `get_config()` | 返回进程内配置单例 |
| `JsonFormatter.format(record)` | 将日志格式化为单行 JSON，附加 route/intent/latency/tokens 等字段 |
| `configure_logging(level)` | 配置根日志为 JSON 输出，降低第三方库日志级别 |

### 4.2 数据访问层

#### `app/db/connection.py`

| 方法 | 作用 |
|---|---|
| `_db_path()` | 返回当前配置的 SQLite 路径 |
| `_configure(conn, readonly)` | 设置行工厂、busy_timeout、外键；只读连接加 `query_only`，读写连接设置 WAL |
| `connect(readonly=False)` | 建立连接；只读用 URI `mode=ro` |
| `read_conn()` | 只读连接上下文，退出自动关闭 |
| `write_conn()` | 读写连接上下文，正常提交、异常回滚、退出关闭 |
| `init_db(verbose=False)` | 执行 `schema.sql`（幂等），返回五张表行数，可选打印 |

#### `app/db/customers.py`

| 方法 | 作用 |
|---|---|
| `mask_phone(phone)` | 手机号脱敏为 `前3****后4` |
| `_loads(raw, fallback)` | 安全解析 JSON 文本，失败返回兜底值 |
| `_row_to_customer(row, with_mask=True)` | 行转字典，解析 `tags`/`profile`，默认脱敏手机号 |
| `get_by_phone(phone)` | 按手机号查客户 |
| `get_by_id(customer_id, with_mask=True)` | 按主键查客户，可返回明文手机号 |
| `create(phone, name=None)` | 新建客户并返回对象 |
| `get_or_create(phone)` | 存在则返回，不存在则创建；返回 `(客户, 是否新建)` |
| `touch(customer_id)` | 刷新 `last_active_at` |
| `_sanitize_profile_value(key, value)` | 清洗画像值：`concerns` 去空、截断 20 字、最多 6 项；其他字段截断 120 字 |
| `merge_profile(customer_id, fields, intent_level, note)` | 按白名单合并画像与 `name`；校验意向等级；`hot + goal` 自动置 `qualified`；刷新活跃时间；返回对象并附 `_changed` |
| `update_fields(customer_id, stage, intent_level, tags, note)` | 管理端局部更新字段；无变更时直接返回当前对象 |
| `_like(term)` | 转义 `%`、`_`、`\` 后生成 LIKE 模式 |
| `list_customers(q, stage, intent_level, page, page_size)` | 分页查询客户，附带最后一条用户消息与消息数 |
| `stats()` | 返回客户总数、今日活跃、hot 数、近 7 天新增 |
| `auto_stage_by_rounds(customer_id, min_user_messages=3)` | `new` 客户 user 消息达阈值后自动转 `contacted` |
| `delete(customer_id)` | 物理删除客户，返回是否删除成功 |

#### `app/db/conversations.py`

| 方法 | 作用 |
|---|---|
| `_loads(raw, fallback)` | 安全解析 JSON |
| `create(customer_id, title=None)` | 新建会话 |
| `get(conversation_id)` | 按主键查会话 |
| `latest(customer_id)` | 查客户最近更新的会话 |
| `reusable(customer_id, hours=24)` | 查 N 小时内可复用的最近会话 |
| `get_or_create(customer_id, hours=24)` | 复用或新建会话，返回 `(会话, 是否新建)` |
| `touch(conversation_id)` | 刷新会话 `updated_at` |
| `add_message(...)` | 写入消息并刷新会话时间；首条用户消息写入标题；返回消息 ID |
| `list_messages(conversation_id, limit, ascending)` | 取最近 N 条消息，可按正序或倒序输出 |
| `_row_to_message(row)` | 行转消息字典，解析 `citations` |
| `recent_for_customer(customer_id, limit)` | 取客户跨会话最近消息，按时间正序 |
| `history(conversation_id, limit=6)` | 取会话最近 N 条历史，正序 |
| `list_for_customer(customer_id)` | 列出客户全部会话及消息数 |
| `count(customer_id=None)` | 统计会话总数，可按客户过滤 |
| `first_user_message(conversation_id)` | 取会话首条用户消息（用于长期需求摘要） |
| `last_assistant_message(customer_id)` | 取客户最后一条助手消息（用于问候语） |

#### `app/db/kb.py`

| 方法 | 作用 |
|---|---|
| `_row_to_doc(row)` | 行转知识条目字典，`enabled` 转布尔 |
| `get(doc_id)` | 按 `doc_id` 查条目（含全文） |
| `exists(doc_id)` | 判断条目是否存在 |
| `upsert(...)` | 插入或覆盖知识条目，更新 `updated_at`，返回对象 |
| `update_chunk_count(doc_id, chunk_count, enabled=True)` | 更新切片数与启用状态 |
| `set_enabled(doc_id, enabled)` | 单独切换启用状态 |
| `delete(doc_id)` | 删除条目，返回是否成功 |
| `list_documents(collection, include_disabled, page, page_size)` | 分页列出条目，默认隐藏停用项；列表不返回 `content` |
| `all_enabled(collection=None)` | 取全部启用条目，供重建索引 |
| `count()` | 统计知识条目总数 |

### 4.3 向量层

#### `app/vector/store.py`

| 方法 | 作用 |
|---|---|
| `_require_collection_key(key)` | 校验集合键，非法抛 `ValueError` |
| `get_client()` | 获取/重建 Chroma `PersistentClient`，路径变化时清空句柄缓存 |
| `get_collection(key)` | 获取/创建 collection（余弦距离），加锁缓存 |
| `reset()` | 清空客户端与 collection 缓存（测试用） |
| `heartbeat()` | 探测 Chroma 是否可用 |
| `add_chunks(collection_key, doc_id, title, chunks, embeddings)` | 批量写入切片，生成 ID 与 metadata，返回切片数 |
| `delete_doc(collection_key, doc_id)` | 按 `doc_id` 删除该文档全部切片 |
| `delete_ids(collection_key, ids)` | 按 ID 列表删除切片 |
| `clear_collection(collection_key)` | 删除并重建空 collection（重建索引用） |
| `count(collection_key)` | 返回集合切片数，异常返回 0 |
| `total_count()` | 三个集合切片数之和 |
| `query(collection_key, embedding, k=4)` | 向量检索，距离转相似度，按分数降序 |
| `search_all(embedding, k=4)` | 跨三个集合检索后合并取 top-k |

### 4.4 模型层

#### `app/llm/client.py`

| 方法 | 作用 |
|---|---|
| `_retryable(exc)` | 判断异常是否可重试（超时/连接错误/5xx） |
| `_estimate_tokens(text)` | 粗略估算 token：中文 1 字 1 token，英文 4 字符 1 token |
| `DeepSeekClient.__init__()` | 用配置创建 OpenAI 兼容客户端 |
| `DeepSeekClient.chat(...)` | 非流式对话，解析文本、工具调用与 token 用量 |
| `DeepSeekClient.stream(...)` | 流式对话，逐段产出文本；已产出内容后不再重试；失败抛 `UpstreamUnavailable` |
| `DeepSeekClient._call(kwargs, stream)` | 统一调用入口，可重试错误最多尝试 2 次 |
| `EchoClient.__init__()` | 初始化离线客户端 |
| `EchoClient.chat(...)` | 离线模拟：SQL 场景返回 `query_crm_db` 工具调用；意图场景返回空以触发规则降级；其他返回组合文本 |
| `EchoClient.stream(...)` | 把组合文本按 4 字符切片流式输出，并估算 token |
| `EchoClient._last_user(messages)` | 取最后一条用户消息 |
| `EchoClient._has_tool_result(messages)` | 判断消息中是否已有工具结果 |
| `EchoClient._demo_sql(text)` | 命中「新增/几个客户/统计」时返回演示 SQL |
| `EchoClient._compose(messages)` | 按工具结果、RAG 上下文、实时资讯、默认文案组合回答 |
| `get_llm()` | 返回 LLM 单例：有 Key 用 DeepSeek，否则用 Echo |
| `reset_llm()` | 清空 LLM 单例 |
| `llm_available()` | 是否配置了线上 LLM |

#### `app/llm/embedding.py`

| 方法 | 作用 |
|---|---|
| `_local_embed(text, dim)` | 字符 1-gram + 2-gram 哈希到固定维度并 L2 归一化 |
| `Embedder.__init__()` | 有 DashScope Key 时创建 OpenAI 兼容客户端，否则用本地向量 |
| `Embedder.available`（property） | 是否使用线上 Embedding |
| `Embedder._batch(texts)` | 单批向量化，失败重试 1 次；兼容不支持 `dimensions` 的旧 SDK |
| `Embedder.embed_documents(texts)` | 按 `embedding_batch` 分批向量化文档切片 |
| `Embedder.embed_query(text)` | 向量化单条查询 |
| `get_embedder()` | 返回 Embedder 单例 |
| `reset_embedder()` | 清空 Embedder 单例 |

### 4.5 核心编排与业务

#### `app/core/pipeline.py`

| 方法 | 作用 |
|---|---|
| `_clean(text, limit=2000)` | 压缩空白并截断，用于历史与日志摘要 |
| `phone_hash(phone)` | 手机号 SHA-256 前 8 位，用于日志脱敏 |
| `build_greeting(customer)` | 生成问候语：优先「上次聊到…」，其次带画像模板，最后新客模板；可选一次 5 秒 LLM 改写 |
| `start_session(phone)` | 建立/复用客户与会话，生成问候语并在新会话中落库，返回客户、会话 ID、是否新客、问候语 |
| `ChatStream.__init__(...)` | 保存客户、会话、消息、手机号指纹，创建取消事件 |
| `ChatStream.cancel()` | 标记客户端已取消 |
| `ChatStream._persist(...)` | 落库助手消息（内容、意图、路由、引用、耗时、token），失败仅记录日志 |
| `ChatStream.run()` | 单轮对话主流程：落用户消息 → 刷新活跃 → 载入历史 → 意图识别 → RAG/Agent/直答 → 流式产出 → 落助手消息 → 发送 `done` → 触发后台画像 |
| `start_chat(phone, conversation_id, message)` | 校验会话归属，未指定时取最近会话或新建，返回 `ChatStream` |
| `customer_card(phone, with_messages=False)` | 组装聊天页客户卡；可选带最近 20 条消息 |

`ChatStream.run()` 的 SSE 事件顺序：`meta → delta/tool → citation → done`，异常时 `error`；客户端取消时落库并追加 `[truncated]`。

#### `app/core/intent.py`

| 方法 | 作用 |
|---|---|
| `IntentResult`（dataclass） | 意图结果：编码、名称、路由、置信度、原因、来源、是否低置信 |
| `_load_intents()` | 从 `intents` 表读取启用项并缓存；表不可用时用内置字典兜底 |
| `reset_cache()` | 清空意图缓存 |
| `rule_classify(query)` | 关键词降级分类：数据查询 → 课程 → 实时 → 职业 → 闲聊 |
| `_result(code, confidence, reason, source)` | 按编码组装意图结果 |
| `classify(query, history_summary, profile_summary)` | 一次强制 `route_intent` 工具调用做分类；非法返回或异常走规则；置信度 < 0.55 转 `direct` 并标记低置信 |

#### `app/core/agents.py`

| 方法/对象 | 作用 |
|---|---|
| `TOOLS` | 四个工具的 OpenAI function schema：`search_knowledge_base`、`web_search`、`query_crm_db`、`update_customer_profile` |
| `TOOLS_BY_NAME` | 工具名到定义的映射 |
| `ROUTE_TOOLS` | 路由到可用工具列表：`web_search` → 联网；`sql` → 查库；`direct` → 无工具 |
| `tools_for_route(route)` | 返回该路由可用工具 schema |
| `validate_sql(sql)` | SQL 守卫：非空、单条、仅 SELECT/WITH、禁注释、禁写操作/PRAGMA、表白名单、允许 CTE 名称 |
| `_make_authorizer(deadline)` | 生成 SQLite authorizer：只放行 SELECT/FUNCTION/RECURSIVE 与白名单表读取，超时拒绝 |
| `_mask_value(column, value)` | 结果中列名含 `phone` 的字符串脱敏 |
| `run_sql(sql)` | 只读执行单条 SELECT：5 秒超时、最多 100 行、phone 脱敏，返回列、行、行数、是否截断 |
| `_tool_search_knowledge_base(args)` | 校验集合与查询，调用 `retrieve`，返回标题、摘要、分数、doc_id |
| `_tool_web_search(args)` | 调用 Tavily 搜索，未配置 Key 或失败时返回错误结构 |
| `_tool_query_crm_db(args)` | 调用 `run_sql`，把 `SQLRejected` 转为 `{"error": "SQL_REJECTED"}` |
| `_tool_update_customer_profile(args, customer_id)` | 调用 `merge_profile` 写入画像，返回更新字段 |
| `ToolContext`（dataclass） | 工具执行上下文：客户 ID、会话 ID、引用列表、最近 SQL 结果 |
| `_with_timeout(fn, timeout, *args)` | 用线程池执行并限制超时 |
| `_with_retry(fn, timeout, args)` | 只读工具失败后等待 1 秒重试 1 次 |
| `execute_tool(name, args, ctx)` | 白名单分派工具；收集引用与 SQL 结果；异常转错误结构回填模型 |
| `_assistant_tool_message(content, calls)` | 构造带 `tool_calls` 的 assistant 消息 |
| `_iter_text(text, size=10)` | 将最终文本按 10 字切片，模拟流式输出 |
| `dedupe_citations(items)` | 按 `doc_id` 或 `title` 去重引用 |
| `run_agent(messages, customer_id, conversation_id, route, max_steps)` | Agent 循环：无工具直接流式；有工具则最多 N 轮「模型 → 工具 → 回填」，最后非流式生成并切片下发；产出 delta/tool/usage/citations |

#### `app/core/profiler.py`

| 方法 | 作用 |
|---|---|
| `extract_with_rules(text)` | 正则抽取身份、目标、预算、时间窗、年限、顾虑，并推断 `warm`/`hot` |
| `_llm_extract(messages)` | 强制 `update_customer_profile` 工具调用抽取画像 |
| `run_extraction(customer_id, conversation_id)` | 取最近 4 条消息，LLM 抽取优先、正则补齐，合并画像并尝试自动流转阶段 |
| `_safe_extract(customer_id, conversation_id)` | 包裹异常，避免后台线程崩溃影响主流程 |
| `schedule(customer_id, conversation_id)` | 启动 daemon 线程执行画像抽取 |

#### `app/core/retriever.py`

| 方法 | 作用 |
|---|---|
| `retrieve(collection, query, top_k, threshold)` | 向量化查询、检索并按阈值过滤；离线模式改用相对阈值 |
| `build_context(hits)` | 把命中切片组装为 `<context>` 提示块 |
| `citations_from_hits(hits)` | 知识命中转引用结构（doc_id、title、score、snippet） |
| `citations_from_web(results)` | 网页结果转引用结构（url 作为 doc_id） |

#### `app/core/knowledge.py`

| 方法 | 作用 |
|---|---|
| `_splitter()` | 构建递归字符切片器；依赖缺失时返回 None |
| `split_text(content)` | 按 500 字/80 重叠切片，过滤 < 15 字的空片 |
| `split_for_documents(content, limit=20000)` | 超长文本按段落拆成多条 ≤ 20000 字符的文档 |
| `new_doc_id(collection)` | 生成 `集合_日期_6位随机` 的 doc_id |
| `doc_id_from_file(collection, filename, content)` | 用文件名 slug + 内容指纹生成稳定 doc_id，重复上传只更新 |
| `_validate(collection, content, doc_id)` | 校验集合、正文非空、正文长度、doc_id 格式 |
| `upsert_document(...)` | 入库主流程：切片 → 向量化 → 写临时 chunk → 删旧 → 写正式 → 删临时 → 写元数据；失败停用旧条目 |
| `_safe_disable(doc_id, existing)` | 异常时把已有条目置为停用，忽略二次异常 |
| `delete_document(doc_id)` | 先删向量再删元数据；向量删除失败抛 503 |
| `_rebuild_one(collection)` | 清空集合，按启用文档逐条切片、向量化、写入并更新切片数；失败停用 |
| `rebuild(collection='all')` | 重建一个或全部集合；非阻塞锁，重复触发抛 `Conflict`；返回文档数、切片数、失败数、耗时 |

#### `app/core/docparse.py`

| 方法 | 作用 |
|---|---|
| `ext_of(filename)` | 取小写扩展名 |
| `_normalize(text)` | 统一换行、去除控制字符、压缩空白与多余空行 |
| `_decode_plain(data)` | 识别 BOM，按 UTF-8 → GB18030 → 容错 UTF-8 解码 |
| `_pdf_text(data)` | 用 pypdf 抽取 PDF 文本，处理加密与解析失败 |
| `_docx_text(data)` | 用 python-docx 抽取段落与表格文本 |
| `extract_text(filename, data)` | 按扩展名校验并抽取纯文本；空文件、超 10MB、旧版 `.doc`、不支持格式均抛 422 |
| `source_label(filename)` | 生成 `file:pdf` 等来源标签 |

#### `app/core/strategies.py`

| 方法 | 作用 |
|---|---|
| `profile_summary(profile, name)` | 将画像字段转为中文摘要，如「身份=行政；目标=转 AI 产品」 |
| `strategy_for(customer)` | 按阶段与意向返回回答策略：won/lost 克制、qualified/hot 给下一步、contacted 给选项、new 先确认处境 |
| `build_system_prompt(customer, strategy_instruction, context)` | 用回答模板 + 画像 + 策略 + 检索上下文组装 system prompt |

#### `app/core/prompts.py`

纯常量模块，无函数。主要常量：

| 常量 | 作用 |
|---|---|
| `INTENT_SYSTEM`、`ROUTE_INTENT_TOOL` | 意图分类系统提示与工具 Schema |
| `ANSWER_SYSTEM` | 回答顾问人设、事实约束、长度与语气要求 |
| `CONTEXT_BLOCK`、`CONTEXT_ITEM` | RAG 上下文包裹格式 |
| `RAG_MISS_INSTRUCTION` | 知识库未命中的回答约束 |
| `PROFILE_SYSTEM`、`PROFILE_TOOL` | 画像抽取提示与工具 Schema |
| `GREETING_NEW`、`GREETING_NEW_WITH_PROFILE`、`GREETING_RETURNING`、`GREETING_LLM_SYSTEM` | 新客/老客问候语模板与 LLM 改写提示 |
| `EMPTY_ANSWER_FALLBACK`、`TRUNCATED_SUFFIX` | 空回答兜底与截断标记 |

#### `app/core/ratelimit.py`

| 方法 | 作用 |
|---|---|
| `_prune(bucket, window, now)` | 移除窗口外的历史时间戳 |
| `hit(key, limit, window)` | 消耗一次配额，返回是否允许与需等待秒数 |
| `peek(key, limit, window)` | 只检查是否超限，不消耗配额（登录预检查） |
| `check_chat(phone)` | 先检查全局配额，再检查单手机号配额 |
| `check_kb()` | 知识库写操作全局限流 |
| `reset()` | 清空全部限流桶 |

#### `app/core/errors.py`

| 类/方法 | 作用 |
|---|---|
| `ApiError.__init__` | 统一错误：消息、错误码、HTTP 状态、字段、是否可重试、附加信息 |
| `ApiError.to_dict()` | 输出 `{"error": {code, message, field?, ...}}` |
| `InvalidArgument` | 400 参数错误 |
| `Unauthorized` | 401 未登录 |
| `NotFound` | 404 资源不存在 |
| `Conflict` | 409 冲突（如重建进行中） |
| `Unprocessable` | 422 语义/格式不可处理 |
| `RateLimited` | 429 限流 |
| `UpstreamUnavailable` | 503 上游不可用，默认可重试 |
| `SQLRejected` | SQL 安全拒绝，作为工具结果回填，不直接抛给 HTTP |

### 4.6 接入层（API）

#### `app/api/auth.py`

| 方法 | 作用 |
|---|---|
| `_eq(left, right)` | 常量时间字符串比较 |
| `check_credentials(username, password)` | 校验管理员账号与密码 |
| `_login_key()` | 按 `X-Forwarded-For` 或远端地址生成登录限流键 |
| `guard_login_attempt()` | 登录前检查失败次数（10 次/300 秒） |
| `record_login_failure()` | 记录一次登录失败 |
| `login_admin(username)` | 清空并写入 Session，设为永久会话 |
| `logout_admin()` | 移除 Session 中的管理员标记 |
| `current_admin()` | 读取当前登录管理员 |
| `_api_key_ok()` | 校验 `X-API-Key`（脚本调用兼容） |
| `is_authenticated()` | Session 或 API Key 任一通过即视为已登录 |
| `_safe_next(target)` | 只允许跳回 `/admin/` 内部路径，防开放重定向 |
| `login_url()` | 生成带安全 `next` 的登录地址 |
| `require_admin(fn)` | 接口鉴权装饰器，未登录抛 401 |
| `admin_page_required(fn)` | 页面鉴权装饰器，未登录重定向登录页 |
| `require_api_key` | `require_admin` 的兼容别名 |

#### `app/api/chat.py`

| 方法/视图 | 作用 |
|---|---|
| `_json_body()` | 校验请求体必须是 JSON 对象 |
| `_phone(data)` | 校验手机号非空且符合 `1[3-9]` 开头 11 位 |
| `format_sse(event, data)` | 格式化为 `event:`/`data:` SSE 文本块 |
| `event_stream(session)` | 后台线程生产事件，主线程负责 15 秒保活 ping 与 SSE 输出；结束时取消会话 |
| `POST /api/session` `create_session()` | 手机号进入，返回客户、会话、问候语 |
| `POST /api/chat` `chat()` | 校验消息与 `conversation_id`，限流后返回 SSE 流 |
| `GET /api/customer-card` `customer_card()` | 聊天页刷新客户卡，可选带最近 20 条消息 |

#### `app/api/customers.py`

| 方法/视图 | 作用 |
|---|---|
| `_int_arg(name, default, low, high)` | 解析并校验整数查询参数 |
| `GET /api/customers` `list_customers()` | 客户列表：搜索、阶段/意向筛选、分页，并附统计 |
| `GET /api/customers/{id}` `get_customer()` | 客户详情 + 会话列表 + 最近 50 条消息 |
| `GET /api/customers/{id}/contact` `get_contact()` | 返回明文手机号（审计用途，记日志） |
| `PATCH /api/customers/{id}` `patch_customer()` | 修改阶段、意向、标签、备注，逐项校验 |
| `DELETE /api/customers/{id}` `delete_customer()` | 删除客户，级联删除会话与消息 |

#### `app/api/kb.py`

| 方法/视图 | 作用 |
|---|---|
| `_check_limit()` | 知识库写操作限流 |
| `GET /api/kb/documents` `list_documents()` | 按集合分页列条目，可选显示停用项 |
| `POST /api/kb/documents` `upsert_document()` | 新增/覆盖知识条目并索引 |
| `POST /api/kb/upload` `upload_document()` | 上传文件、解析文本、超长拆分、逐条入库 |
| `GET /api/kb/documents/{doc_id}` `get_document()` | 取单条全文（编辑弹窗） |
| `DELETE /api/kb/documents/{doc_id}` `delete_document()` | 删除条目及全部向量 |
| `POST /api/kb/rebuild` `rebuild()` | 按集合或全部重建索引 |

#### `app/api/health.py`

| 方法/视图 | 作用 |
|---|---|
| `_probe(fn, timeout=3.0)` | 在独立线程执行探测，返回 `ok`/`down` |
| `_sqlite_probe()` | 执行 `SELECT 1` 探测数据库 |
| `_llm_probe()` | 一次极短 LLM 调用探测模型 |
| `_embedding_probe()` | 一次查询向量化探测 Embedding |
| `GET /api/health` `health()` | 汇总 SQLite/Chroma/LLM/Embedding 状态与运行模式 |

#### `app/api/pages.py`

| 方法/视图 | 作用 |
|---|---|
| `_safe_next(value)` | 过滤非 `/admin/` 或含 `//`、`\` 的跳转地址 |
| `GET /` `index()` | 用户入口页 |
| `GET /chat` `chat_page()` | 用户聊天页 |
| `GET /admin` `admin_root()` | 已登录跳客户页，否则跳登录页 |
| `GET/POST /admin/login` `admin_login_page()` | 登录页：限流、校验凭证、写入 Session、安全跳转 |
| `POST /admin/logout` `admin_logout()` | 退出登录 |
| `GET /admin/customers` `admin_customers_page()` | 管理端客户页（需登录） |
| `GET /admin/kb` `admin_kb_page()` | 管理端知识库页（需登录） |
| `GET /favicon.ico` `favicon()` | 返回 204 |

### 4.7 前端 JavaScript

#### `app/static/js/chat.js`

| 函数 | 作用 |
|---|---|
| `toast(msg, kind)` | 顶部短暂提示 |
| `showEntry(message)` | 显示手机号入口视图 |
| `showChat()` | 显示聊天视图 |
| `renderCustomer(c)` | 渲染客户卡：手机号、阶段、意向、画像、标签 |
| `loadCard(withMessages)` | 拉取 `/api/customer-card`，可选恢复历史 |
| `addMessage(role, text, opts)` | 创建消息节点（气泡、时间、路由徽标、工具区、慢响应提示） |
| `setBadge(node, route)` | 设置路由徽标文案与样式 |
| `addHint(node, text)` | 追加提示条（如低置信提示） |
| `addToolChip(node, data)` | 展示工具调用名称、参数摘要与耗时 |
| `renderCitations(node, items)` | 渲染可折叠引用列表与来源链接 |
| `renderHistory(messages)` | 渲染历史消息与分隔线 |
| `scrollToBottom(force)` | 根据吸底状态滚动到底部 |
| `setSendingUI(sending)` | 切换发送/停止按钮状态 |
| `send(text, options)` | 发起 `/api/chat` 请求，解析流并处理重试、429、503、断网 |
| `consume(response, node, onFirstToken)` | 读取 `ReadableStream`，按 `\n\n` 分块解析 SSE |
| `consume.dispatch(block)` | 解析单个 SSE 块并分发 |
| `consume.pump()` | 递归读取流数据 |
| `handleEvent(event, data, node, onFirstToken)` | 处理 meta/delta/tool/citation/done/error 事件 |
| `showErrorBar(node, message)` | 显示错误条与重试按钮 |
| `enter(phone)` | 调用 `/api/session` 进入会话、恢复历史、加载客户卡 |
| `autoResize()` | 输入框高度自适应 |
| `bind()` | 绑定表单、键盘、停止、切换手机号、面板、滚动、网络事件 |
| `init()` | 从 URL 或 localStorage 恢复手机号，决定进入或显示入口 |

#### `app/static/js/admin.js`

| 函数 | 作用 |
|---|---|
| `goLogin()` | 跳转登录页并带 `next` |
| `toast(message, kind)` | 提示消息 |
| `readResponse(res)` | 统一解析响应；401 跳登录，其他错误提示 |
| `api(path, options)` | JSON 请求封装，带同源 Cookie |
| `apiUpload(path, formData)` | 文件上传封装 |
| `text(tag, value, className)` | 创建文本节点元素 |
| `fmtTime(value)` | 时间显示为 `MM-DD HH:MM` |
| `stageBadge(stage)` | 阶段徽标 |
| `levelText(level)` | 意向等级文字与配色 |
| `initCustomers()` | 客户页初始化：状态、分页、搜索、筛选、抽屉、标签、备注 |
| `initCustomers.params()` | 组装列表查询参数 |
| `initCustomers.load(reset)` | 加载客户列表 |
| `initCustomers.render(data, reset)` | 渲染统计、表格、空态、加载更多 |
| `initCustomers.rowFor(item)` | 生成单行客户 |
| `initCustomers.openDrawer(id)` | 拉取详情并打开抽屉 |
| `initCustomers.closeDrawer()` | 关闭抽屉 |
| `initCustomers.fillDrawer(c)` | 填充客户详情、画像、标签、最近消息 |
| `initCustomers.renderTags(tags)` | 渲染可删除标签 |
| `initCustomers.patch(payload, done)` | 局部更新客户并刷新行 |
| `initCustomers.refreshRow(updated)` | 同步列表中对应行 |
| `initCustomers.addTag()` | 新增标签 |
| `initKb()` | 知识库页初始化：列表、编辑、删除、重建、上传 |
| `initKb.load()` | 加载当前集合条目 |
| `initKb.render(data)` | 渲染知识条目表格 |
| `initKb.openModal(docId)` | 打开新建/编辑弹窗，编辑时取全文 |
| `initKb.closeModal()` | 关闭弹窗 |
| `initKb.updateCount()` | 更新正文长度计数 |
| `initKb.save()` | 校验并提交新增/更新 |
| `initKb.removeDoc(doc)` | 确认后删除条目 |
| `initKb.rebuild()` | 确认后重建当前集合索引 |
| `initKb.fileExt(name)` | 取文件扩展名 |
| `initKb.fmtSize(bytes)` | 格式化文件大小 |
| `initKb.openUpload()` | 打开上传弹窗并重置状态 |
| `initKb.closeUpload()` | 关闭上传弹窗 |
| `initKb.addFiles(files)` | 校验格式/大小并加入待上传列表 |
| `initKb.renderUploads()` | 渲染上传队列与状态 |
| `initKb.uploadAll()` | 串行上传全部文件 |
| `initKb.syncTabs()` | 同步集合标签激活态 |
| `init()` | 按 `data-page` 初始化对应管理页 |

### 4.8 脚本

| 文件/方法 | 作用 |
|---|---|
| `scripts/init_db.py::main()` | 配置日志、建表、打印表行数与数据库路径 |
| `scripts/seed_kb.py::DOCS` | 7 条示例知识（course 3、market 3、general 1） |
| `scripts/seed_kb.py::main()` | 建表后逐条调用 `upsert_document` 导入示例知识并统计耗时 |
| `scripts/smoke_test.py::check(name, ok, detail)` | 打印 PASS/FAIL 并累计失败项 |
| `scripts/smoke_test.py::parse_sse(body)` | 解析 SSE 文本为事件列表 |
| `scripts/smoke_test.py::main()` | 端到端冒烟：健康检查 → 会话 → 课程问答 → SQL Agent → 落库 → 鉴权 |

### 4.9 测试

| 文件 | 测试方法 | 验证内容 |
|---|---|---|
| `tests/conftest.py` | `app(tmp_path)` | 用临时目录创建测试应用，隔离 SQLite 与 Chroma |
| | `client(app)` | Flask 测试客户端 |
| | `headers()` | 管理端 `X-API-Key` 请求头 |
| `tests/test_intent.py` | `test_rule_classify` | 关键词分类参数化用例 |
| | `test_classify_falls_back_to_rules` | LLM 异常时规则降级 |
| | `test_low_confidence_becomes_direct` | 低置信转直答 |
| | `test_routes_match_intent_table` | 意图与路由表一致 |
| `tests/test_chat_flow.py` | `parse_sse` | SSE 解析辅助 |
| | `test_session_and_chat` | 会话建立、流式回答、引用与落库 |
| | `test_profile_pipeline` | 画像抽取与阶段流转 |
| | `test_chat_validations` | 手机号/消息/会话参数校验 |
| `tests/test_sql_guard.py` | `test_accepts_single_select` | 合法单条 SELECT |
| | `test_accepts_with_clause` | 合法 CTE |
| | `test_rejects_dangerous_sql` | 拒绝写操作、多语句、注释等 |
| | `test_readonly_connection_blocks_writes` | 只读连接阻止写 |
| | `test_masks_phone_in_results` | 结果手机号脱敏 |
| | `test_limits_rows_to_100` | 行数上限 |
| | `test_unknown_table_rejected` | 非白名单表拒绝 |
| `tests/test_retriever.py` | `seeded(app)` | 预置知识数据 |
| | `test_retrieve_returns_ranked_hits` | 检索返回排序结果 |
| | `test_threshold_filters_everything` | 阈值过滤 |
| | `test_build_context_and_citations` | 上下文与引用组装 |
| | `test_empty_collection` | 空集合返回空 |
| `tests/test_kb_api.py` | `test_requires_api_key` | 写接口鉴权 |
| | `test_create_update_delete` | 新增、覆盖更新、删除 |
| | `test_validation` | 集合/正文/长度校验 |
| | `test_auto_doc_id` | 自动生成 doc_id |
| | `test_rebuild` | 重建索引 |
| `tests/test_kb_upload.py` | `_upload`、`_docx_bytes`、`_pdf_bytes` | 上传与文件构造辅助 |
| | `test_upload_requires_api_key` | 上传鉴权 |
| | `test_upload_markdown` | Markdown 上传 |
| | `test_upload_txt_gbk` | GBK 文本上传 |
| | `test_upload_docx` | Word 上传 |
| | `test_upload_pdf` | PDF 上传 |
| | `test_upload_long_file_splits_into_documents` | 超长文件拆条 |
| | `test_upload_same_file_is_idempotent` | 重复上传幂等 |
| | `test_upload_rejects_bad_input` | 非法输入拒绝 |
| `tests/test_admin_auth.py` | `admin_credentials(app)` | 读取测试管理员凭证 |
| | `test_user_pages_are_public` | 用户页免登录 |
| | `test_admin_page_redirects_to_login` | 管理页未登录跳转 |
| | `test_admin_api_requires_login` | 管理接口未登录 401 |
| | `test_login_with_wrong_password` | 错误密码拒绝 |
| | `test_login_then_access_admin` | 登录后可访问 |
| | `test_logout_clears_session` | 退出清除会话 |

---

## 5. 数据访问矩阵

| 表/存储 | 主要写入方法 | 主要读取方法 | 主要调用方 |
|---|---|---|---|
| `customers` | `create`、`merge_profile`、`update_fields`、`touch`、`auto_stage_by_rounds`、`delete` | `get_by_phone`、`get_by_id`、`list_customers`、`stats` | `pipeline`、`profiler`、`agents`、`api/customers`、`api/chat` |
| `conversations` | `create`、`touch`、`add_message` 内更新 | `get`、`latest`、`reusable`、`list_for_customer`、`count` | `pipeline`、`api/customers` |
| `messages` | `add_message` | `list_messages`、`history`、`recent_for_customer`、`first_user_message`、`last_assistant_message` | `pipeline`、`profiler`、`api/customers`、`api/chat` |
| `kb_documents` | `upsert`、`update_chunk_count`、`set_enabled`、`delete` | `get`、`exists`、`list_documents`、`all_enabled`、`count` | `knowledge`、`api/kb` |
| `intents` | 仅 `schema.sql` 初始化插入 | `_load_intents` | `intent` |
| Chroma | `add_chunks`、`delete_doc`、`delete_ids`、`clear_collection` | `query`、`search_all`、`count`、`total_count` | `knowledge`、`retriever`、`agents`、`health` |
| SQL Agent 只读视图 | 无（强制只读） | 五张业务表 | `agents.run_sql` |

---

## 6. 核心业务流程

### 6.1 会话建立

```text
POST /api/session
 → pipeline.start_session(phone)
 → customers_db.get_or_create(phone)
 → conv_db.get_or_create(customer_id, 24h)
 → build_greeting(customer)
 → 新会话写入 assistant 问候消息
 → 返回 customer / conversation_id / is_new / greeting
```

### 6.2 单轮对话

```text
POST /api/chat
 → 参数校验 + 限流 check_chat
 → pipeline.start_chat 校验会话归属
 → ChatStream.run
    ├─ 写入 user 消息、刷新客户活跃时间
    ├─ 读取最近 history_window 条历史
    ├─ intent.classify 识别意图与路由
    ├─ rag_market / rag_course：retriever.retrieve + build_context
    │    └─ 无命中则 route=rag_miss，追加「知识库暂无资料」指令
    ├─ web_search / sql：agents.run_agent 工具循环
    ├─ 其他：LLM 流式直答
    ├─ 落库 assistant 消息（意图、路由、引用、耗时、token）
    ├─ SSE 发送 citation / done
    └─ profiler.schedule 启动后台画像抽取
```

### 6.3 知识入库与重建

```text
POST /api/kb/documents 或 /api/kb/upload
 → knowledge.upsert_document
 → split_text 切片
 → Embedding 分批向量化
 → 写临时 chunk（doc_id::tmp::xxx）
 → 删除旧 doc_id 向量
 → 写正式 chunk
 → 删除临时 chunk
 → kb_documents.upsert
 → 任一步失败：旧条目 enabled=0

POST /api/kb/rebuild
 → 获取重建锁
 → clear_collection
 → 遍历 all_enabled 文档：切片 → 向量化 → add_chunks → update_chunk_count
 → 失败文档 set_enabled(0)
```

### 6.4 画像沉淀

```text
done 事件后 profiler.schedule
 → 取最近 4 条消息
 → _llm_extract（工具调用）+ extract_with_rules（正则）
 → merge_profile（白名单合并）
 → hot + goal → qualified
 → auto_stage_by_rounds：user 消息 ≥ 3 → contacted
```

### 6.5 SQL Agent 安全链路

```text
模型提出 query_crm_db
 → validate_sql：单条 SELECT/WITH、禁分号、禁注释、禁写操作/PRAGMA、表白名单
 → connect(readonly=True)：mode=ro + query_only
 → set_progress_handler：5 秒超时
 → set_authorizer：仅允许 SELECT/FUNCTION/RECURSIVE 与白名单表读取
 → fetchmany(101)：最多返回 100 行
 → phone 列脱敏
 → 结果 JSON 回填模型
```

---

## 7. 接口、事件与错误码

### 7.1 HTTP 接口

| 方法 | 路径 | 鉴权 | 作用 |
|---|---|---|---|
| POST | `/api/session` | 无 | 手机号进入会话 |
| POST | `/api/chat` | 无 | SSE 流式对话 |
| GET | `/api/customer-card` | 无 | 聊天页客户卡 |
| GET | `/api/customers` | 登录 | 客户列表 + 统计 |
| GET | `/api/customers/{id}` | 登录 | 客户详情 |
| GET | `/api/customers/{id}/contact` | 登录 | 明文手机号（审计） |
| PATCH | `/api/customers/{id}` | 登录 | 修改阶段/意向/标签/备注 |
| DELETE | `/api/customers/{id}` | 登录 | 删除客户 |
| GET | `/api/kb/documents` | 登录 | 知识条目列表 |
| GET | `/api/kb/documents/{doc_id}` | 登录 | 单条全文 |
| POST | `/api/kb/documents` | 登录 | 新增/覆盖知识 |
| POST | `/api/kb/upload` | 登录 | 上传文件入库 |
| DELETE | `/api/kb/documents/{doc_id}` | 登录 | 删除知识及向量 |
| POST | `/api/kb/rebuild` | 登录 | 重建索引 |
| GET | `/api/health` | 无 | 依赖健康检查 |
| GET | `/`、`/chat` | 无 | 用户端页面 |
| GET/POST | `/admin/login` | 无 | 管理端登录 |
| POST | `/admin/logout` | 登录 | 退出登录 |
| GET | `/admin/customers`、`/admin/kb` | 登录 | 管理端页面 |

### 7.2 SSE 事件

| 事件 | 数据 | 含义 |
|---|---|---|
| `meta` | conversation_id、intent_code、intent_name、route、customer、intent_low_confidence | 意图与路由确定 |
| `delta` | `{t: 文本片段}` | 回答增量 |
| `tool` | name、args、ms | 工具调用信息 |
| `citation` | items 数组 | 引用来源 |
| `done` | message_id、latency_ms、tokens、persisted、customer | 本轮完成 |
| `error` | code、message、retryable | 本轮失败 |

另有每 15 秒一行的 `: ping` 保活注释。

### 7.3 错误码

| 错误码 | HTTP | 含义 |
|---|---|---|
| `INVALID_ARGUMENT` | 400 | 参数缺失或格式错误 |
| `UNAUTHORIZED` | 401 | 未登录或登录失效 |
| `NOT_FOUND` | 404 | 资源不存在 |
| `CONFLICT` | 409 | 并发冲突（如重建进行中） |
| `UNPROCESSABLE` | 422 | 语义不可处理（文件格式、长度等） |
| `RATE_LIMITED` | 429 | 触发限流 |
| `INTERNAL` | 500 | 未预期异常 |
| `UPSTREAM_UNAVAILABLE` | 503 | LLM/Embedding/Chroma 等上游不可用 |

### 7.4 枚举与常量

| 类别 | 取值 |
|---|---|
| 客户阶段 `stage` | `new`、`contacted`、`qualified`、`won`、`lost` |
| 意向等级 `intent_level` | `cold`、`warm`、`hot` |
| 路由 `route` | `rag_market`、`rag_course`、`web_search`、`sql`、`direct`、`rag_miss` |
| 消息角色 `role` | `user`、`assistant`、`system` |
| 知识集合 `collection` | `market`、`course`、`general` |
| 意图编码 `intent_code` | 0 闲聊、1 职业咨询、2 课程咨询、3 实时资讯、4 数据查询 |
| 知识来源 `source` | `manual`、`cms`、`import`、`file:pdf`、`file:docx`、`file:txt`、`file:md` 等 |
| 上传格式 | `.pdf`、`.docx`、`.txt`、`.md`、`.markdown` |
| 文件大小上限 | 10MB；知识正文上限 20000 字符；消息上限 2000 字符 |
| 检索参数 | top_k 默认 4；线上阈值 0.35；离线相对阈值 top × 0.6，下限 0.02 |
| 工具步数 | 默认 3 轮 |
| 历史窗口 | 默认 6 条 |
| 会话复用 | 默认 24 小时 |
| 限流 | 单手机号 10 次/60s、全局 60 次/60s、知识库写 20 次/60s、登录 10 次/300s |

---

## 8. 结论与观察

1. **架构清晰**：接入层、编排层、数据访问层、向量层、模型层职责分明，核心业务集中在 `pipeline.py`、`agents.py`、`knowledge.py` 三个文件。
2. **SQLite 是唯一业务真相源**：客户、会话、消息、知识元数据、意图字典均以 SQLite 为准；Chroma 仅作为可重建的检索索引，二者通过 `doc_id` 对齐。
3. **降级链路完整**：无 LLM Key 时走 `EchoClient`，无 Embedding Key 时走本地哈希向量，无 Tavily Key 时返回明确错误，保证开发与测试可离线运行。
4. **安全约束到位**：管理端 Session + 登录限流；SQL Agent 五层防护（语法白名单、只读连接、authorizer、超时、行数上限、脱敏）；手机号默认脱敏。
5. **一致性与可靠性**：知识写入采用临时前缀与失败停用策略；重建使用非阻塞锁；消息与画像写入失败均有兜底日志，不阻塞主回答。
6. **已知边界**：限流、重建锁、客户端缓存均为进程内实现，多 worker 部署会失效；手机号即身份，仅适合本机/内网演示；旧版 `.doc` 与扫描版 PDF 不支持。
