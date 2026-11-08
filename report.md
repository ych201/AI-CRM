# SeaChat AI CRM v2.0 代码库分析报告

> 分析范围：`app/`、`scripts/`、`tests/`、`run.py`（不含 `data/` 运行时数据与前端模板样式细节）。
> 报告内容以源码为准，共 **37 个 Python 文件**、2 个前端 JS 文件、5 张业务表、3 个向量集合。

---

## 1. 项目概览

### 1.1 定位与技术栈

SeaChat AI CRM 是一套面向 AI 职业培训机构的智能咨询 CRM：客户以手机号进入 → 意图识别分流（RAG / 联网 / SQL / 直答）→ SSE 流式回答 → 画像沉淀 → 顾问后台跟进。

| 层 | 技术 | 说明 |
|---|---|---|
| Web 框架 | Flask 3 | 单进程多线程，`create_app()` 工厂 + Blueprint |
| 关系数据库 | SQLite（WAL） | 业务数据唯一真相源，默认 `./data/crm.db` |
| 向量数据库 | Chroma（PersistentClient） | 知识切片向量索引，默认 `./data/chroma` |
| LLM | DeepSeek `deepseek-chat`（OpenAI 兼容） | 未配置 Key 时降级 `EchoClient` |
| Embedding | 百炼 `text-embedding-v4`（1024 维，批量 ≤10） | 未配置 Key 时降级本地字符 n-gram 哈希向量 |
| 联网搜索 | Tavily | 未配置 Key 时返回 `TAVILY_NOT_CONFIGURED` |
| 文件解析 | `pypdf` + `python-docx` | 知识库上传 PDF / Word(.docx) / TXT / Markdown；旧版 `.doc` 与无文本层扫描件不支持 |
| 前端 | 原生 HTML + Jinja2 + 原生 JS | `fetch` + `ReadableStream` 解析 SSE |
| 测试 | pytest | 离线可跑，无需外部服务 |

### 1.2 目录与模块职责

| 路径 | 职责 |
|---|---|
| `run.py` | 启动入口：开发模式 `app.run(threaded, use_reloader=False)`，生产模式 waitress |
| `app/__init__.py` | 应用工厂：注册 Blueprint、统一错误处理、DB 初始化、JSON 编码 |
| `app/config.py` | `.env` 读取、单例配置、绝对路径解析、结构化 JSON 日志 |
| `app/api/` | HTTP 接口层（会话/对话、客户、知识库、健康检查、页面） |
| `app/core/` | 业务核心：编排、意图、检索、Agent、画像、知识库写入、文件解析、限流、策略、Prompt |
| `app/db/` | SQLite 访问层：连接管理、DDL、三组表读写 |
| `app/llm/` | LLM 客户端与 Embedding 封装（含离线降级实现） |
| `app/vector/` | Chroma 客户端、Collection 管理、写入与检索 |
| `app/templates/`、`app/static/` | 三个页面与前端逻辑 |
| `scripts/` | 建表、导入示例知识、端到端冒烟 |
| `tests/` | 意图、对话链路、知识库、知识库文件上传、检索、SQL 守卫单测 |

### 1.3 一次对话的主链路

```
POST /api/chat
  └─ ratelimit.check_chat(phone)
      └─ pipeline.start_chat → ChatStream.run()
          ├─ 写入 user 消息、更新 customers.last_active_at
          ├─ 取最近 6 条历史 + 首条需求摘要
          ├─ intent.classify()  ← intents 表 / 关键词降级
          ├─ route ∈ {rag_market, rag_course} → retrieve() + build_context()
          │     └─ 无命中 → route = rag_miss
          ├─ route ∈ {web_search, sql} → run_agent() → execute_tool()
          ├─ 其余 → llm.stream() 直出
          ├─ yield meta / delta / tool / citation / done
          ├─ 写入 assistant 消息（含 citations、latency、tokens）
          └─ profiler.schedule() 后台线程更新 customers.profile/stage
```

---

## 2. 数据库总览

### 2.1 存储清单

| 存储 | 实际路径 | 类型 | 在项目中的职责 | 是否真相源 |
|---|---|---|---|---|
| SQLite `crm.db` | `data/crm.db`（`SQLITE_PATH`） | 关系库 | 客户、会话、消息、知识条目元数据、意图字典；管理端列表/详情/统计、SQL 工具查询均读此库 | **是** |
| Chroma | `data/chroma/`（`CHROMA_PATH`） | 向量库 | 存放知识切片文本、1024 维向量与切片元数据，供 RAG 语义检索 | 否（派生数据，可由 `kb_documents` 重建） |
| 进程内存 | — | 缓存 | 限流滑动窗口、意图字典缓存、LLM/Embedder/Chroma 单例 | 否（重启即失效） |

> Chroma 目录内部的 `chroma.sqlite3` 只是 Chroma 自身的持久化实现，不作为业务数据源，业务代码不直接访问它。

### 2.2 SQLite 的作用与访问方式

| 项 | 说明 |
|---|---|
| 建表 | `app/db/schema.sql`，`CREATE TABLE IF NOT EXISTS`，幂等；`init_db()` 在应用启动与脚本中执行 |
| 写连接 | `write_conn()`：`journal_mode=WAL`、`synchronous=NORMAL`、`foreign_keys=ON`、`busy_timeout=5000`，异常自动回滚 |
| 读连接 | `read_conn()`：URI `file:...?mode=ro` + `PRAGMA query_only=ON`，供查询、统计与 SQL 工具使用 |
| 时间 | 统一本地时间字符串 `YYYY-MM-DD HH:MM:SS`（`datetime('now','localtime')`） |
| 外键 | `conversations.customer_id → customers.id`、`messages.conversation_id → conversations.id`，均 `ON DELETE CASCADE` |
| 主要消费者 | `app/db/*`、`app/api/customers.py`、`app/core/pipeline.py`、`app/core/intent.py`、`app/core/agents.py` |

### 2.3 Chroma 的作用与结构

| 项 | 说明 |
|---|---|
| Collection | 逻辑键 `market` / `course` / `general`，实际名称 `kb_market` / `kb_course` / `kb_general` |
| 距离度量 | `metadata={"hnsw:space": "cosine"}`；检索得分 `score = 1 - cosine_distance`，保留 4 位小数 |
| 与业务对应 | `rag_market → market`、`rag_course → course`；`general` 供 `search_knowledge_base` 工具显式检索 |
| 写入 | `add_chunks()`：ID 为 `{doc_id}::{chunk_index}`，同时写 documents、embeddings、metadatas |
| 删除 | 按元数据 `where={"doc_id": doc_id}` 批量删除；重建时直接删除并重建整个 Collection |
| 一致性 | `kb_documents` 为真相源；写入采用「临时 doc_id → 删旧 → 正式 doc_id → 写元数据」顺序，失败时旧数据仍可用 |

### 2.4 表关系

```
customers (1) ──< conversations (1) ──< messages
     │
     └─ profile(JSON) / tags(JSON)

intents (code)  ←逻辑引用─ messages.intent_code

kb_documents (id=TEXT doc_id) ──1:N──> Chroma 切片（metadata.doc_id）
```

- `messages.intent_code` 仅存整数，**未声明外键**，由应用层与 `intents` 表对齐。
- `kb_documents.id` 与 Chroma 元数据 `doc_id` 同名，是两库之间唯一的关联键。

### 2.5 数据生命周期

| 场景 | 行为 |
|---|---|
| 客户首次进入 | `get_or_create(phone)` 创建 `customers`；24 小时内复用最近会话，否则新建 `conversations` |
| 每轮对话 | 写 1 条 user 消息和 1 条 assistant 消息；刷新 `customers.last_active_at`、`conversations.updated_at` |
| 首条用户消息 | 自动以内容前 20 字回填 `conversations.title` |
| 画像更新 | 后台线程合并 `customers.profile`；`intent_level=hot` 且有 `goal` 时 `stage→qualified`；用户消息 ≥3 条时 `new→contacted` |
| 删除客户 | `DELETE customers` 级联删除其会话与消息（知识库不受影响） |
| 知识条目更新 | 重算切片与向量，覆盖 `kb_documents` 并清理旧切片 |
| 文件上传入库 | 抽取文本 → 超 20000 字按段落拆成多条 → 每条切片向量化并写入 `kb_documents`（`source=file:<ext>`）；同内容重复上传为更新 |
| 重建索引 | 清空指定 Collection，按 `kb_documents.content` 重新切片、向量化、回写 `chunk_count` |

---

## 3. 数据表与字段说明

### 3.1 `customers` — 客户主表

一条记录 = 一个手机号对应的客户，是画像与跟进状态的载体。

| 字段 | 类型 | 约束/默认 | 含义 |
|---|---|---|---|
| `id` | INTEGER | PK AUTOINCREMENT | 客户内部主键，接口与关联表使用 |
| `phone` | TEXT | NOT NULL UNIQUE | 手机号，业务身份唯一标识；对外默认脱敏为 `138****8000` |
| `name` | TEXT | 可空 | 客户称呼/姓名；由画像抽取写入，最长 40 字 |
| `stage` | TEXT | NOT NULL `'new'` | 销售阶段：`new` 新客 / `contacted` 已接触 / `qualified` 有明确意向 / `won` 已成交 / `lost` 已流失 |
| `intent_level` | TEXT | NOT NULL `'cold'` | 意向等级：`cold` 冷 / `warm` 中 / `hot` 高 |
| `tags` | TEXT | NOT NULL `'[]'` | JSON 数组字符串，运营标签；接口限制最多 12 个、单个 ≤12 字 |
| `profile` | TEXT | NOT NULL `'{}'` | JSON 对象，结构化画像，字段见 §3.6 |
| `note` | TEXT | 可空 | 顾问备注；管理端写入时截断至 500 字 |
| `created_at` | TEXT | 本地时间 | 首次进入时间 |
| `last_active_at` | TEXT | 本地时间 | 最后活跃时间；列表默认按其倒序，`touch()` 每轮刷新 |

索引：`idx_customers_active(last_active_at DESC)`、`idx_customers_stage(stage)`。

### 3.2 `conversations` — 会话表

| 字段 | 类型 | 约束/默认 | 含义 |
|---|---|---|---|
| `id` | INTEGER | PK AUTOINCREMENT | 会话主键，SSE `meta.conversation_id` 返回 |
| `customer_id` | INTEGER | NOT NULL，FK→`customers.id`，CASCADE | 所属客户 |
| `title` | TEXT | 可空 | 会话标题，首条用户消息前 20 字自动写入，之后不再覆盖 |
| `created_at` | TEXT | 本地时间 | 会话创建时间 |
| `updated_at` | TEXT | 本地时间 | 最后消息时间；`reusable()` 以其判断 24 小时复用窗口 |

索引：`idx_conv_customer(customer_id)`。

### 3.3 `messages` — 消息表

| 字段 | 类型 | 约束/默认 | 含义 |
|---|---|---|---|
| `id` | INTEGER | PK AUTOINCREMENT | 消息主键，`done.message_id` 返回 |
| `conversation_id` | INTEGER | NOT NULL，FK→`conversations.id`，CASCADE | 所属会话 |
| `role` | TEXT | NOT NULL | 角色：`user` 客户 / `assistant` AI 顾问 / `system` 系统 |
| `content` | TEXT | NOT NULL | 消息正文；客户端中断时追加 ` [truncated]` |
| `intent_code` | INTEGER | 可空 | 意图编码 0–4，与 `intents.code` 对应（仅逻辑关联） |
| `route` | TEXT | 可空 | 本轮实际路由：`rag_market` / `rag_course` / `web_search` / `sql` / `direct` / `rag_miss` |
| `citations` | TEXT | NOT NULL `'[]'` | JSON 数组，引用项含 `doc_id`、`title`、`score`、`snippet` |
| `latency_ms` | INTEGER | 可空 | 从接收请求到落库的耗时（毫秒） |
| `tokens_in` | INTEGER | 可空 | 输入 token 数（模型返回或离线估算） |
| `tokens_out` | INTEGER | 可空 | 输出 token 数 |
| `created_at` | TEXT | 本地时间 | 消息创建时间 |

索引：`idx_msg_conv(conversation_id, id)`。

### 3.4 `kb_documents` — 知识条目表（真相源）

| 字段 | 类型 | 约束/默认 | 含义 |
|---|---|---|---|
| `id` | TEXT | PK | 业务 `doc_id`，与 Chroma 元数据 `doc_id` 一致；正则 `[A-Za-z0-9_.-]{1,80}`，未传则自动生成 `{collection}_{YYYYMMDD}_{6位hex}`；文件上传生成 `{collection}_{文件名slug}_{内容sha1前6位}`，重复上传同内容幂等 |
| `collection` | TEXT | NOT NULL | 所属集合：`market` / `course` / `general` |
| `title` | TEXT | NOT NULL | 条目标题；未传时取正文前 20 字；上传取文件名（超长文件拼接 `（i/n）` 序号） |
| `content` | TEXT | NOT NULL | 知识全文，单条限制 20000 字；切片来源。文件上传超过该上限时由 `split_for_documents` 按段落拆成多条记录 |
| `source` | TEXT | 可空 | 来源标记：`manual` 手工 / `cms` 内容系统 / `import` 导入 / `file:pdf`、`file:docx`、`file:txt`、`file:md` 文件上传 |
| `chunk_count` | INTEGER | NOT NULL `0` | 当前切片数，写入/重建时回填 |
| `enabled` | INTEGER | NOT NULL `1` | 是否启用：1 参与检索与重建，0 停用（写入失败时会被置 0） |
| `created_at` | TEXT | 本地时间 | 创建时间 |
| `updated_at` | TEXT | 本地时间 | 最后更新时间；列表按其倒序 |

索引：`idx_kb_collection(collection, updated_at DESC)`。

### 3.5 `intents` — 意图字典表

可运营调整，无需改代码；`intent._load_intents()` 启动后缓存，`enabled=1` 才生效。

| 字段 | 类型 | 约束/默认 | 含义 |
|---|---|---|---|
| `code` | INTEGER | PK | 意图编码：0 闲聊/其他、1 职业咨询、2 课程咨询、3 实时资讯、4 数据查询 |
| `name` | TEXT | NOT NULL | 意图名称，SSE `meta.intent_name` 返回 |
| `description` | TEXT | NOT NULL | 意图说明，用于运营理解与分类提示 |
| `route` | TEXT | NOT NULL | 默认路由：`direct` / `rag_market` / `rag_course` / `web_search` / `sql` |
| `enabled` | INTEGER | NOT NULL `1` | 是否启用；停用后不进入缓存，缺失编码回退意图 0；表为空或不可读时整体回退内置 `DEFAULT_INTENTS` |

内置字典（`INSERT OR IGNORE`）：

| code | name | route |
|---|---|---|
| 0 | 闲聊/其他 | `direct` |
| 1 | 职业咨询 | `rag_market` |
| 2 | 课程咨询 | `rag_course` |
| 3 | 实时资讯 | `web_search` |
| 4 | 数据查询 | `sql` |

### 3.6 `customers.profile` JSON 字段（画像白名单）

键必须在白名单内，否则 `merge_profile()` 直接忽略；单值最长 120 字。

| JSON 键 | 含义 | 抽取/清理规则 |
|---|---|---|
| `current_role` | 当前身份/岗位，如「行政」 | 字符串，≤120 字 |
| `years_exp` | 工作年限，如「3」 | 字符串，≤120 字 |
| `goal` | 转行/学习目标，如「AI 产品」 | 字符串，≤120 字 |
| `budget` | 预算，如「一万以内」 | 字符串，≤120 字 |
| `time_window` | 可投入时间/期望周期，如「3 个月」 | 字符串，≤120 字 |
| `city` | 所在城市 | 字符串，≤120 字 |
| `concerns` | 顾虑/担心点，字符串数组 | 每项 ≤20 字，最多 6 项；传字符串时自动转单元素数组 |

> `name` 是例外：虽然出现在画像工具参数中，但落库到 `customers.name`，不写入 `profile`。

### 3.7 Chroma 切片结构

| 组成 | 内容 |
|---|---|
| 记录 ID | `{doc_id}::{chunk_index}`（重建或覆盖时先删旧再写） |
| `embeddings` | 1024 维浮点向量（`EMBEDDING_DIM`） |
| `documents` | 切片文本，来自 `RecursiveCharacterTextSplitter`（chunk 500 / overlap 80，最小长度 15） |
| `metadata.doc_id` | 对应 `kb_documents.id` |
| `metadata.collection` | `market` / `course` / `general` |
| `metadata.title` | 条目标题，用于引用展示 |
| `metadata.chunk_index` | 切片序号，从 0 开始 |
| `metadata.updated_at` | 本次写入时间 |

### 3.8 数据库索引

| 索引 | 所在表 | 作用 |
|---|---|---|
| `idx_customers_active` | `customers` | 客户列表按最近活跃排序 |
| `idx_customers_stage` | `customers` | 按销售阶段筛选 |
| `idx_conv_customer` | `conversations` | 按客户查会话 |
| `idx_msg_conv` | `messages` | 按会话顺序取消息 |
| `idx_kb_collection` | `kb_documents` | 按集合 + 更新时间列知识条目 |

---

## 4. 方法清单（按模块）

> 说明：下表覆盖项目内全部函数/方法（含内部辅助函数、类方法与测试函数）。「返回」列仅列关键返回形态。

### 4.1 启动与应用工厂

| 方法 | 用途 | 关键行为 / 返回 |
|---|---|---|
| `run.py` 模块级 | 创建应用 | `app = create_app()`；开发模式 `threaded=True, use_reloader=False`，生产优先 waitress（8 线程） |
| `app.create_app(test_config)` | 应用工厂 | 读取配置 → 测试配置覆盖并重置 LLM/Embedder/Chroma/意图缓存/限流 → 注册蓝图与错误处理 → `init_db()` → 返回 Flask 实例；`MAX_CONTENT_LENGTH = 11MB`（`docparse.MAX_FILE_BYTES + 1MB`） |
| `app._register_blueprints(app)` | 注册路由 | 依次注册 `pages`、`chat`、`customers`、`kb`、`health` 蓝图 |
| `app._register_error_handlers(app)` | 注册错误处理 | 内部函数 `_api_error`（`ApiError`→JSON）、`_http_error`（HTTP 异常→错误码；413 转为「单个文件不超过 10MB」提示）、`_unhandled`（未捕获异常→500） |
| `app._json_charset(response)` | `after_request` 钩子 | 为 `application/json` 响应补 `charset=utf-8` |

### 4.2 配置与日志（`app/config.py`）

| 方法 | 用途 | 关键行为 / 返回 |
|---|---|---|
| `_as_bool(value, default)` | 解析布尔环境变量 | `1/true/yes/on` 为真，空值取默认 |
| `_as_int(value, default)` | 解析整型环境变量 | 失败返回默认值 |
| `_as_float(value, default)` | 解析浮点环境变量 | 失败返回默认值 |
| `_as_path(value, default)` | 解析路径 | 相对路径基于项目根目录，返回绝对路径 |
| `Config.__init__()` | 配置单例初始化 | `load_dotenv()` 后调用 `reload()` |
| `Config.reload()` | 重载全部配置 | LLM/Embedding/Tavily、存储路径、密钥、RAG 参数、窗口、限流、工具超时、切片参数；最后 `ensure_dirs()` |
| `Config.ensure_dirs()` | 创建数据目录 | 确保 SQLite 父目录与 Chroma 目录存在 |
| `Config.llm_configured`（property） | 是否配置对话模型 | `bool(DEEPSEEK_API_KEY)` |
| `Config.embedding_configured`（property） | 是否配置向量模型 | `bool(DASHSCOPE_API_KEY)` |
| `get_config()` | 获取配置单例 | 返回全局 `config` |
| `JsonFormatter.format(record)` | 结构化日志格式化 | 输出单行 JSON，含 ts/level/logger/msg 及 route、intent_code、latency_ms、tokens、phone_hash 等扩展键 |
| `configure_logging(level)` | 初始化日志 | stdout handler + JsonFormatter，压低 chromadb/httpx/openai 日志级别 |

**关键配置默认值**：`rag_top_k=4`、`rag_score_threshold=0.35`、`history_window=6`、`max_tool_steps=3`、`max_message_len=2000`、`llm_timeout=60s`、会话复用 24h、切片 500/80、限流 10/60/60 次每 60s、工具超时 3/8/5/2 秒。

### 4.3 数据访问层

#### 4.3.1 连接与建表（`app/db/connection.py`）

| 方法 | 用途 | 关键行为 / 返回 |
|---|---|---|
| `_db_path()` | 取 SQLite 路径 | `get_config().sqlite_path` |
| `_configure(conn, readonly)` | 统一连接参数 | Row 工厂、busy_timeout、外键；只读加 `query_only`，读写加 WAL + `synchronous=NORMAL` |
| `connect(readonly=False)` | 创建连接 | 只读用 `file:...?mode=ro`；调用方负责提交/关闭 |
| `read_conn()` | 只读连接上下文 | `with` 结束自动关闭 |
| `write_conn()` | 读写连接上下文 | 正常提交、异常回滚、最终关闭 |
| `init_db(verbose=False)` | 执行 DDL | 幂等建表与意图字典，返回 5 张表行数 |

#### 4.3.2 客户表（`app/db/customers.py`）

| 方法 | 用途 | 关键行为 / 返回 |
|---|---|---|
| `mask_phone(phone)` | 手机号脱敏 | ≥7 位→`前3****后4`，否则原样返回 |
| `_loads(raw, fallback)` | 安全解析 JSON | 解析失败返回 fallback |
| `_row_to_customer(row, with_mask=True)` | 行转字典 | JSON 字段反序列化，默认脱敏手机号 |
| `get_by_phone(phone)` | 按手机号查客户 | 客户字典或 `None` |
| `get_by_id(customer_id, with_mask=True)` | 按 ID 查客户 | 可选返回明文手机号（仅审计接口用） |
| `create(phone, name=None)` | 新建客户 | 返回新客户（已脱敏） |
| `get_or_create(phone)` | 幂等获取/创建 | `(customer, is_new)` |
| `touch(customer_id)` | 刷新最后活跃时间 | 更新 `last_active_at` |
| `_sanitize_profile_value(key, value)` | 清洗画像值 | `concerns` 转数组、每项 20 字、最多 6 项；其他值去空白、截断 120 字 |
| `merge_profile(customer_id, fields, intent_level, note)` | 白名单合并画像 | `name` 落 `customers.name`；`hot` + `goal` 且 stage 为 new/contacted 时置 `qualified`；返回客户并附带 `_changed` |
| `update_fields(customer_id, stage, intent_level, tags, note)` | 管理端局部更新 | 动态 SQL；无字段时直接返回当前客户 |
| `_like(term)` | 构造 LIKE 模式 | 转义 `\`、`%`、`_` |
| `list_customers(q, stage, intent_level, page, page_size)` | 客户分页列表 | 附带最后一条 user 消息（30 字）与消息总数，返回 `total/page/page_size/items` |
| `stats()` | 客户统计 | 总数、今日活跃、hot 数、近 7 天新增 |
| `auto_stage_by_rounds(customer_id, min_user_messages=3)` | 自动阶段流转 | 仅 `new` 且用户消息 ≥3 时改为 `contacted` |
| `delete(customer_id)` | 删除客户 | 依赖外键级联删除会话与消息；返回是否删除成功 |

#### 4.3.3 会话与消息（`app/db/conversations.py`）

| 方法 | 用途 | 关键行为 / 返回 |
|---|---|---|
| `_loads(raw, fallback)` | 安全解析 JSON | 同上 |
| `create(customer_id, title=None)` | 新建会话 | 返回会话字典 |
| `get(conversation_id)` | 按 ID 取会话 | 会话字典或 `None` |
| `latest(customer_id)` | 最近一条会话 | 按 `updated_at DESC, id DESC` |
| `reusable(customer_id, hours=24)` | 查找可复用会话 | 更新时间在 N 小时内的最近会话 |
| `get_or_create(customer_id, hours=24)` | 复用或新建会话 | `(conversation, is_new)` |
| `touch(conversation_id)` | 刷新会话更新时间 | 更新 `updated_at` |
| `add_message(...)` | 追加消息 | 写消息、刷新会话时间、首条 user 消息回填标题；返回消息 ID |
| `list_messages(conversation_id, limit=50, ascending=True)` | 取会话消息 | 先取最近 N 条再按 ID 排序 |
| `_row_to_message(row)` | 行转字典 | `citations` 反序列化 |
| `recent_for_customer(customer_id, limit=50)` | 客户跨会话最近消息 | 正序返回 |
| `history(conversation_id, limit=6)` | 对话历史窗口 | 正序最近 N 条 |
| `list_for_customer(customer_id)` | 客户的会话列表 | 附带每条会话的消息数 |
| `count(customer_id=None)` | 会话计数 | 全局或指定客户 |
| `first_user_message(conversation_id)` | 首条用户消息 | 用于生成「最初需求」摘要 |
| `last_assistant_message(customer_id)` | 最近一条 AI 回复 | 用于老客户问候语 |

#### 4.3.4 知识条目（`app/db/kb.py`）

| 方法 | 用途 | 关键行为 / 返回 |
|---|---|---|
| `_row_to_doc(row)` | 行转字典 | `enabled` 转布尔，`id→doc_id` |
| `get(doc_id)` | 按 doc_id 取全文 | 文档字典或 `None` |
| `exists(doc_id)` | 是否存在 | 布尔 |
| `upsert(doc_id, collection, title, content, source, chunk_count, enabled)` | 新增或覆盖 | `ON CONFLICT(id) DO UPDATE`，刷新 `updated_at` |
| `update_chunk_count(doc_id, chunk_count, enabled=True)` | 回写切片数与启用态 | 重建索引后使用 |
| `set_enabled(doc_id, enabled)` | 启用/停用 | 仅改 `enabled` |
| `delete(doc_id)` | 删除条目 | 返回是否删除成功 |
| `list_documents(collection, include_disabled, page, page_size)` | 分页列表 | 默认只列启用项；**不返回 content** |
| `all_enabled(collection=None)` | 取全部启用条目 | 重建索引用，含全文 |
| `count()` | 知识条目总数 | 整数 |

### 4.4 API 层

#### 4.4.1 鉴权（`app/api/auth.py`）

| 方法 | 用途 | 关键行为 / 返回 |
|---|---|---|
| `require_api_key(fn)` | 管理端鉴权装饰器 | 内部 `wrapper` 取 `X-API-Key`，与 `ADMIN_API_KEY` 做 `hmac.compare_digest`；失败抛 `Unauthorized`(401) |

#### 4.4.2 对话接口（`app/api/chat.py`）

| 方法 | 用途 | 关键行为 / 返回 |
|---|---|---|
| `_json_body()` | 校验 JSON 请求体 | 非对象抛 `InvalidArgument` |
| `_phone(data)` | 校验手机号 | 非空 + 正则 `^1[3-9]\d{9}$` |
| `format_sse(event, data)` | 格式化 SSE 块 | `event: ...\ndata: ...\n\n` |
| `event_stream(session)` | SSE 事件生成器 | 内部 `produce` 在子线程遍历 `session.run()` 入队并兜底 `error`；主线程 15s 无事件发 `: ping`；结束时 `session.cancel()` |
| `create_session()` | `POST /api/session` | 手机号进入，返回客户、会话 ID、`is_new`、问候语 |
| `chat()` | `POST /api/chat` | 校验 message（非空、≤2000）与 conversation_id，限流后返回 `text/event-stream` |
| `customer_card()` | `GET /api/customer-card` | 按手机号返回客户卡；`with_messages=1` 时带最近 20 条消息 |

#### 4.4.3 客户接口（`app/api/customers.py`）

| 方法 | 用途 | 关键行为 / 返回 |
|---|---|---|
| `_int_arg(name, default, low, high)` | 解析并校验整数查询参数 | 越界抛 `Unprocessable` |
| `list_customers()` | `GET /api/customers` | 支持 `q/stage/intent_level/page/page_size`，附 `stats` |
| `get_customer(customer_id)` | `GET /api/customers/{id}` | 客户 + 会话列表 + 最近 50 条消息 |
| `get_contact(customer_id)` | `GET /api/customers/{id}/contact` | 审计用途，唯一返回明文手机号的接口，记录日志 |
| `patch_customer(customer_id)` | `PATCH /api/customers/{id}` | 仅允许 `stage/intent_level/tags/note`；标签 ≤12 个且每个 ≤12 字；备注截断 500 字 |
| `delete_customer(customer_id)` | `DELETE /api/customers/{id}` | 先判存在再删除，级联会话与消息 |

#### 4.4.4 知识库接口（`app/api/kb.py`）

| 方法 | 用途 | 关键行为 / 返回 |
|---|---|---|
| `_check_limit()` | KB 写操作限流 | 超限抛 `RateLimited`(429) |
| `list_documents()` | `GET /api/kb/documents` | 支持 `collection/show_disabled/page/page_size` |
| `upsert_document()` | `POST /api/kb/documents` | 校验后调用 `knowledge.upsert_document`，返回 doc_id/切片数/动作 |
| `upload_document()` | `POST /api/kb/upload` | 接收 multipart `file` + `collection`（可选 `title/doc_id/source`）；`docparse.extract_text` 抽取文本，超 20000 字按段落拆成多条入库；返回 `doc_ids/documents/chunks/action/filename/chars` |
| `get_document(doc_id)` | `GET /api/kb/documents/{doc_id}` | 返回全文（编辑弹窗用） |
| `delete_document(doc_id)` | `DELETE /api/kb/documents/{doc_id}` | 删向量 + 删元数据；不存在返回 404 |
| `rebuild()` | `POST /api/kb/rebuild` | 按 `collection`（默认 `all`）重建索引 |

#### 4.4.5 健康检查与页面

| 方法 | 用途 | 关键行为 / 返回 |
|---|---|---|
| `_probe(fn, timeout=3.0)` | 带超时的探针执行 | 成功 `ok`、异常 `down` |
| `_sqlite_probe()` | SQLite 探测 | 只读执行 `SELECT 1` |
| `_llm_probe()` | LLM 探测 | 发送 `ping`，`max_tokens=1`，3s 超时 |
| `_embedding_probe()` | Embedding 探测 | 对 `ping` 取向量并校验非空 |
| `health()` | `GET /api/health` | 汇总 sqlite/chroma/llm/embedding 状态、版本、运行模式 |
| `chat_page()` | `GET /` | 渲染 `chat.html` |
| `admin_customers_page()` | `GET /admin/customers` | 渲染客户管理页 |
| `admin_kb_page()` | `GET /admin/kb` | 渲染知识库管理页 |
| `favicon()` | `GET /favicon.ico` | 返回 204 |

### 4.5 对话编排（`app/core/pipeline.py`）

| 方法 | 用途 | 关键行为 / 返回 |
|---|---|---|
| `_clean(text, limit=2000)` | 文本清洗 | 空白折叠、去首尾、截断 |
| `phone_hash(phone)` | 手机号日志指纹 | SHA-256 前 8 位，日志不落明文 |
| `build_greeting(customer)` | 生成问候语 | 老客户引用上次回复摘要；有画像用画像版；`GREETING_USE_LLM=true` 时 5s 短调用（失败静默降级） |
| `start_session(phone)` | 建立/恢复会话 | 取或建客户与 24h 内会话；新会话落库问候语；返回 customer/conversation_id/is_new/greeting |
| `ChatStream.__init__(customer, conversation, message, phone_hash_value)` | 初始化一次流式对话 | 保存上下文、创建 `cancelled` 事件 |
| `ChatStream.cancel()` | 标记取消 | 客户端断开时停止生成 |
| `ChatStream._persist(content, intent_code, route, citations, latency_ms, usage, suffix)` | 落库 AI 回复 | 空内容不写；异常仅记日志；返回消息 ID |
| `ChatStream.run()` | 核心生成器 | 写 user 消息 → 历史窗口 → 意图识别 → 预检索/工具循环/直答 → 产出 `meta/delta/tool/citation/done/error` → 落库 → 调度画像 |
| `start_chat(phone, conversation_id, message)` | 构造对话流 | 校验会话归属（不存在抛 404）；未传会话则取最近或新建 |
| `customer_card(phone, with_messages=False)` | 组装客户卡 | 返回基础字段与画像；可选最近 20 条消息 |

### 4.6 意图识别（`app/core/intent.py`）

| 方法 | 用途 | 关键行为 / 返回 |
|---|---|---|
| `IntentResult`（dataclass） | 意图结果载体 | 字段：code、name、route、confidence、reason、source、low_confidence |
| `_load_intents()` | 加载意图字典 | 读 `intents` 表启用项并进程级缓存；异常/空表回退 `DEFAULT_INTENTS` |
| `reset_cache()` | 清空意图缓存 | 测试或配置切换时调用 |
| `rule_classify(query)` | 关键词规则分类 | 优先级：SQL → 课程（排除行情词）→ 实时 → 课程 → 职业 → 0；返回 `(code, reason)` |
| `_result(code, confidence, reason, source)` | 组装意图结果 | 从字典取 name/route，缺失回退意图 0 |
| `classify(query, history_summary, profile_summary)` | 意图识别主入口 | 强制 `tool_choice=route_intent`、`temperature=0`；非法参数/异常走规则；`confidence<0.55` 转 `direct` 并标记 `low_confidence` |

### 4.7 RAG 检索（`app/core/retriever.py`）

| 方法 | 用途 | 关键行为 / 返回 |
|---|---|---|
| `retrieve(collection, query, top_k, threshold)` | 语义检索 | 查询向量 → Chroma 取 top_k → 阈值过滤；离线模式改相对阈值 `max(0.02, min(0.35, top*0.6))` |
| `build_context(hits)` | 组装 `<context>` | 按 `CONTEXT_ITEM` 编号拼接标题、doc_id、正文，并附「仅作事实依据」声明 |
| `citations_from_hits(hits)` | 知识引用 | `doc_id/title/score/snippet(≤60 字)` |
| `citations_from_web(results)` | 联网引用 | 以 URL 作为 `doc_id`，`score=None` |

### 4.8 工具与 Agent（`app/core/agents.py`）

| 方法 | 用途 | 关键行为 / 返回 |
|---|---|---|
| `tools_for_route(route)` | 按路由取工具 | `web_search→[web_search]`、`sql→[query_crm_db]`，其他为空 |
| `validate_sql(sql)` | SQL 静态校验 | 仅单条 `SELECT/WITH`；禁分号、注释、写操作/PRAGMA/ATTACH 等；表白名单；允许 CTE 别名；返回去空白语句 |
| `_make_authorizer(deadline)` | 生成 SQLite authorizer | 仅允许 SELECT/READ(白名单表)/FUNCTION/RECURSIVE；超时返回 DENY |
| `_mask_value(column, value)` | 结果脱敏 | 列名含 `phone` 的字符串值脱敏 |
| `run_sql(sql)` | 只读执行 SQL | 只读连接 + 5s 进度中断 + authorizer + `fetchmany(101)` 截断 100 行；返回 columns/rows/row_count/truncated |
| `_tool_search_knowledge_base(args)` | 知识库工具 | 校验 collection/query，top_k 限制 1–8；返回 hits（含 200 字摘要） |
| `_tool_web_search(args)` | 联网搜索工具 | 校验 query 非空（工具 schema 限 120 字）、max_results 限制 1–5；无 Key 返回 `TAVILY_NOT_CONFIGURED`；异常返回 `TAVILY_FAILED` |
| `_tool_query_crm_db(args)` | SQL 工具 | 调用 `run_sql`，拒绝时返回 `SQL_REJECTED` + 原因 |
| `_tool_update_customer_profile(args, customer_id)` | 画像写入工具 | 调用 `merge_profile`，返回更新字段列表或错误码 |
| `ToolContext`（dataclass） | 工具上下文 | customer_id、conversation_id、citations、last_sql_result |
| `_with_timeout(fn, timeout, *args)` | 工具超时控制 | 线程池执行，超时抛 `TimeoutError` |
| `_with_retry(fn, timeout, args)` | 只读工具重试 | 失败等 1s 重试 1 次 |
| `execute_tool(name, args, ctx)` | 工具分派 | 白名单 if 分派；知识/联网结果写入 `ctx.citations`；未知工具返回 `UNKNOWN_TOOL` |
| `_assistant_tool_message(content, calls)` | 构造 assistant tool_calls 消息 | OpenAI 兼容格式，arguments 序列化为 JSON |
| `_iter_text(text, size=10)` | 文本切片 | 按 10 字切块下发，模拟流式 |
| `dedupe_citations(items)` | 引用去重 | 以 `doc_id` 或 `title` 为键保序去重 |
| `run_agent(messages, customer_id, conversation_id, route, max_steps=None)` | Agent 循环 | 最多 `max_tool_steps=3` 轮；产出 `delta/tool/usage/citations`；最后一轮直接切片下发；超限追加「直接回答」系统指令 |

### 4.9 知识库写入（`app/core/knowledge.py`）

| 方法 | 用途 | 关键行为 / 返回 |
|---|---|---|
| `_splitter()` | 构造切片器 | `RecursiveCharacterTextSplitter(500/80)`，分隔符按段落/句号等；不可用时返回 `None` |
| `split_text(content)` | 切片 | 无 splitter 时按 500 字硬切；过滤长度 <15 的切片 |
| `split_for_documents(content, limit=20000)` | 超长文本拆分（文件导入） | 先按空行分段，单段超限时优先在句号处断开；再按 `limit` 装箱，返回若干 ≤20000 字的片段 |
| `new_doc_id(collection)` | 生成 doc_id | `{collection}_{YYYYMMDD}_{6位hex}` |
| `doc_id_from_file(collection, filename, content)` | 上传生成稳定 doc_id | 文件名 slug（仅保留 `A-Za-z0-9_.-`，≤40 字）+ 内容 sha1 前 6 位；中文文件名自动省略 slug |
| `_validate(collection, content, doc_id)` | 入参校验 | collection 枚举、content 非空且 ≤20000、doc_id 正则 |
| `upsert_document(collection, content, title, doc_id, source)` | 新增/覆盖知识条目 | 先全量 embedding → 写临时前缀切片 → 删旧 → 写正式 id → 删临时 → 写元数据；失败置 `enabled=0` 并抛 503；返回 doc_id/chunks/action/indexed_at/title |
| `_safe_disable(doc_id, existing)` | 失败兜底 | 已存在条目则置为停用 |
| `delete_document(doc_id)` | 删除条目 | 先删 Chroma 向量再删元数据；不存在返回 `deleted=False` |
| `_rebuild_one(collection)` | 单集合重建 | 清空集合 → 逐条切片向量化 → 回写 chunk_count；失败条目停用；返回 (文档数, 切片数, 失败数) |
| `rebuild(collection="all")` | 重建索引 | 非阻塞锁防并发（409）；支持单集合或全部；返回统计与耗时 |

### 4.10 文件解析（`app/core/docparse.py`）

| 方法 | 用途 | 关键行为 / 返回 |
|---|---|---|
| `MAX_FILE_BYTES` / `SUPPORTED_EXTS`（模块常量） | 上传约束 | 单文件 10MB；支持 `.pdf` / `.docx` / `.txt` / `.md` / `.markdown` |
| `ext_of(filename)` | 取扩展名 | 小写后缀（含点），无后缀返回空串 |
| `_normalize(text)` | 文本清洗 | 统一换行、去控制字符、折叠行内空白、把连续 3 个以上换行折叠为 1 个空行 |
| `_decode_plain(data)` | 纯文本解码 | 识别 UTF-16 / UTF-8 BOM；依次尝试 UTF-8、GB18030，最后 UTF-8 容错解码 |
| `_pdf_text(data)` | PDF 抽取 | `pypdf` 按页 `extract_text()`；加密、损坏、缺依赖抛 `Unprocessable`(422) |
| `_docx_text(data)` | Word 抽取 | `python-docx` 读取段落与表格（表格按行拼接各单元格文本）；损坏、缺依赖抛 422 |
| `extract_text(filename, data)` | 统一抽取入口 | 校验文件名 / 空文件 / 大小 / 扩展名 → 按类型抽取 → 清洗；无文本抛 422（扫描件提示先 OCR） |
| `source_label(filename)` | 来源标记 | 返回 `file:pdf` / `file:docx` / `file:txt` / `file:md` 等，写入 `kb_documents.source` |

### 4.11 客户画像（`app/core/profiler.py`）

| 方法 | 用途 | 关键行为 / 返回 |
|---|---|---|
| `extract_with_rules(text)` | 正则抽取画像 | 身份/目标/预算/时间窗/年限/顾虑；目标 + 预算或时间→`hot`，仅目标→`warm`；返回 `(fields, level)` |
| `_llm_extract(messages)` | LLM 抽取画像 | 强制调用 `update_customer_profile`，`temperature=0`；返回 `(fields, level)` |
| `run_extraction(customer_id, conversation_id)` | 画像抽取主流程 | 取最近 4 条消息 → LLM 抽取 → 规则补齐（`setdefault`）→ `merge_profile` → `auto_stage_by_rounds` |
| `_safe_extract(customer_id, conversation_id)` | 异常隔离 | 捕获所有异常并记日志 |
| `schedule(customer_id, conversation_id)` | 调度后台线程 | daemon 线程执行，不阻塞 SSE |

### 4.12 限流、策略与错误

| 方法 | 模块 | 用途 |
|---|---|---|
| `_prune(bucket, window, now)` | `core/ratelimit.py` | 移除窗口外的历史时间戳 |
| `hit(key, limit, window)` | `core/ratelimit.py` | 滑动窗口计数，返回 `(allowed, wait_seconds)` |
| `check_chat(phone)` | `core/ratelimit.py` | 先全局 60 次/60s，再单手机号 10 次/60s |
| `check_kb()` | `core/ratelimit.py` | KB 写操作 20 次/60s |
| `reset()` | `core/ratelimit.py` | 清空所有计数桶（测试用） |
| `profile_summary(profile, name=None)` | `core/strategies.py` | 中文标签拼接画像摘要，空则「暂无」 |
| `strategy_for(customer)` | `core/strategies.py` | 按 stage/intent_level 返回回答策略指令（won/lost 克制、qualified 直接给下一步、contacted 给两个选项、new 先确认处境） |
| `build_system_prompt(customer, strategy_instruction, context=None)` | `core/strategies.py` | 填充 `ANSWER_SYSTEM`，有检索结果时追加 `<context>` |
| `ApiError.__init__(message, code, status, field, retryable, extra)` | `core/errors.py` | 统一错误对象 |
| `ApiError.to_dict()` | `core/errors.py` | 输出 `{"error": {code, message, field?, ...}}` |
| `InvalidArgument` / `Unauthorized` / `NotFound` / `Conflict` / `Unprocessable` / `RateLimited` / `UpstreamUnavailable` | `core/errors.py` | 对应 400/401/404/409/422/429/503 |
| `SQLRejected.__init__(reason)` | `core/errors.py` | SQL 安全拒绝，作为工具结果回填模型 |

### 4.13 LLM 客户端（`app/llm/client.py`）

| 方法 | 用途 | 关键行为 / 返回 |
|---|---|---|
| `ToolCall` / `LLMResult`（dataclass） | 结果载体 | 工具调用（id/name/arguments）与内容、token、模型名 |
| `_retryable(exc)` | 判断是否可重试 | 超时、连接错误、5xx 为可重试 |
| `_estimate_tokens(text)` | 粗略估算 token | 中文 1 字≈1 token，其他 4 字符≈1 token |
| `DeepSeekClient.__init__()` | 初始化线上客户端 | OpenAI SDK 指向 DeepSeek，`max_retries=0` |
| `DeepSeekClient.chat(...)` | 非流式对话 | 透传 tools/tool_choice；解析 tool_calls 与 usage；失败抛 `UpstreamUnavailable` |
| `DeepSeekClient.stream(...)` | 流式对话 | `stream_options.include_usage`；已产出内容后不重试，否则重试 1 次 |
| `DeepSeekClient._call(kwargs, stream=False)` | 底层调用与重试 | 最多 2 次，可重试错误间隔 1s |
| `EchoClient.__init__()` | 离线客户端初始化 | 读取配置 |
| `EchoClient.chat(...)` | 离线非流式 | SQL 演示：命中「新增/几个客户/多少客户/统计」时返回 `query_crm_db` 调用；`route_intent` 直接返回空（触发关键词降级） |
| `EchoClient.stream(...)` | 离线流式 | 组合答复按 4 字切片，8ms 间隔，回填估算 token |
| `EchoClient._last_user(messages)` | 取最后一条用户输入 | 字符串 |
| `EchoClient._has_tool_result(messages)` | 是否已有工具结果 | 布尔 |
| `EchoClient._demo_sql(text)` | 演示 SQL | 返回今日新增客户统计 SQL 或 `None` |
| `EchoClient._compose(messages)` | 离线答复生成 | 优先用工具结果，其次 `<context>`，再判断联网类问题，最后通用话术 |
| `get_llm()` | 获取 LLM 单例 | 有 Key 用 `DeepSeekClient`，否则 `EchoClient` |
| `reset_llm()` | 重置单例 | 测试/配置切换 |
| `llm_available()` | 是否线上模型 | 布尔 |

### 4.14 Embedding（`app/llm/embedding.py`）

| 方法 | 用途 | 关键行为 / 返回 |
|---|---|---|
| `_local_embed(text, dim)` | 本地哈希向量 | 字符 1-gram(0.5) + 2-gram(1.0) MD5 取模累加，L2 归一化 |
| `Embedder.__init__()` | 初始化向量客户端 | 有 Key 建 OpenAI 兼容客户端，否则走本地向量 |
| `Embedder.available`（property） | 是否线上向量 | 客户端是否存在 |
| `Embedder._batch(texts)` | 单批向量化 | 线上批量 ≤10、失败重试 1 次；离线逐条本地向量 |
| `Embedder.embed_documents(texts)` | 文档批量向量化 | 按 `embedding_batch` 分批 |
| `Embedder.embed_query(text)` | 查询向量化 | 单条 |
| `get_embedder()` | 获取 Embedder 单例 | 线程安全 |
| `reset_embedder()` | 重置单例 | 测试/配置切换 |

### 4.15 向量库（`app/vector/store.py`）

| 方法 | 用途 | 关键行为 / 返回 |
|---|---|---|
| `_require_collection_key(key)` | 校验集合键 | 非法抛 `ValueError` |
| `get_client()` | 获取 Chroma 客户端 | 路径变化时重建并清缓存 |
| `get_collection(key)` | 获取/创建集合 | 名称映射 `kb_*`，cosine 距离 |
| `reset()` | 释放缓存句柄 | 测试用 |
| `heartbeat()` | 健康探测 | 布尔 |
| `add_chunks(collection_key, doc_id, title, chunks, embeddings)` | 写入切片 | ID `{doc_id}::{i}`，写文本/向量/元数据；返回切片数 |
| `delete_doc(collection_key, doc_id)` | 按 doc_id 删切片 | 失败记日志并抛出 |
| `delete_ids(collection_key, ids)` | 按 ID 批量删 | 空列表直接返回 |
| `clear_collection(collection_key)` | 清空集合并重建 | 重建索引用 |
| `count(collection_key)` | 单集合切片数 | 异常返回 0 |
| `total_count()` | 全部集合切片数 | 三集合之和 |
| `query(collection_key, embedding, k=4)` | 向量检索 | 返回 content/doc_id/title/collection/chunk_index/score，按分数倒序 |
| `search_all(embedding, k=4)` | 跨集合检索 | 合并三集合结果取 top_k |

### 4.16 脚本

| 方法 | 用途 |
|---|---|
| `scripts/init_db.py::main()` | 初始化日志、建表、打印 SQLite/Chroma 路径与各表行数 |
| `scripts/seed_kb.py::main()` | 初始化 DB 后导入 7 条示例知识（course 3 / market 3 / general 1），走与 API 相同的入库函数 |
| `scripts/smoke_test.py::check(name, ok, detail)` | 打印 PASS/FAIL 并累计失败项 |
| `scripts/smoke_test.py::parse_sse(body)` | 解析 SSE 响应为 `(event, data)` 列表 |
| `scripts/smoke_test.py::main()` | 端到端冒烟：健康检查 → 会话 → 课程问答（含引用）→ SQL Agent → 落库 → 鉴权 |

### 4.17 测试

| 文件 | 方法 / 用例 | 覆盖点 |
|---|---|---|
| `tests/conftest.py` | `app`、`client`、`headers` fixtures | 临时 SQLite/Chroma、离线模式、测试 API Key |
| `tests/test_chat_flow.py` | `parse_sse`、`test_session_and_chat`、`test_profile_pipeline`、`test_chat_validations` | SSE 事件序列、引用落库、画像自动更新、参数校验 |
| `tests/test_intent.py` | `test_rule_classify`、`test_classify_falls_back_to_rules`、`test_low_confidence_becomes_direct`、`test_routes_match_intent_table` | 关键词分类、LLM 失败降级、低置信度转直答、意图路由映射 |
| `tests/test_kb_api.py` | `test_requires_api_key`、`test_create_update_delete`、`test_validation`、`test_auto_doc_id`、`test_rebuild` | 鉴权、幂等覆盖、切片一致、参数校验、重建索引 |
| `tests/test_kb_upload.py` | `test_upload_requires_api_key`、`test_upload_markdown`、`test_upload_txt_gbk`、`test_upload_docx`、`test_upload_pdf`、`test_upload_long_file_splits_into_documents`、`test_upload_same_file_is_idempotent`、`test_upload_rejects_bad_input` | 上传鉴权、四种格式解析（含手工构造 PDF）、GBK 编码、长文件拆分、重复上传幂等、非法输入 |
| `tests/test_retriever.py` | `seeded` fixture、`test_retrieve_returns_ranked_hits`、`test_threshold_filters_everything`、`test_build_context_and_citations`、`test_empty_collection` | 排序、阈值过滤、上下文/引用格式、空集合 |
| `tests/test_sql_guard.py` | `test_accepts_single_select`、`test_accepts_with_clause`、`test_rejects_dangerous_sql`、`test_readonly_connection_blocks_writes`、`test_masks_phone_in_results`、`test_limits_rows_to_100`、`test_unknown_table_rejected` | 只读约束、危险语句拦截、脱敏、100 行上限、表白名单 |

> 当前 `pytest tests` 共 **47 个用例**，离线模式（无任何外部 Key）全部通过。

### 4.18 前端（`app/static/js/`）

#### `chat.js`（客户端聊天页）

| 方法 | 用途 |
|---|---|
| `toast(msg, kind)` | 顶部轻提示，3.2s 自动消失 |
| `showLogin(message)` / `showChat()` | 切换登录视图与聊天视图 |
| `renderCustomer(c)` | 渲染客户卡：手机号、阶段/意向、画像键值、标签 |
| `loadCard(withMessages)` | 拉取 `/api/customer-card`，可选恢复历史消息 |
| `addMessage(role, text, opts)` | 创建消息节点（气泡、徽标、工具位、慢响应提示） |
| `setBadge(node, route)` | 按路由显示中文徽标 |
| `addHint(node, text)` | 追加提示文本（如低置信度提示） |
| `addToolChip(node, data)` | 渲染工具调用胶囊（名称/摘要/耗时） |
| `renderCitations(node, items)` | 渲染引用折叠面板，HTTP 来源附外链 |
| `renderHistory(messages)` | 清空并重建历史消息 |
| `scrollToBottom(force)` | 自动滚底（受 `state.stick` 控制） |
| `setSendingUI(sending)` | 发送/停止按钮状态切换 |
| `send(text, options)` | 发送消息：`fetch` POST、AbortController 中断、5s 慢提示、错误分类（429/503/离线） |
| `consume(response, node, onFirstToken)` | 读取 `ReadableStream` 并按 `\n\n` 分块；内部 `dispatch` 解析事件、`pump` 递归读取 |
| `handleEvent(event, data, node, onFirstToken)` | 处理 meta/delta/tool/citation/done/error |
| `showErrorBar(node, message)` | 错误条与「重试」按钮 |
| `enter(phone)` | `POST /api/session` 进入会话、写入 localStorage、渲染问候与历史 |
| `autoResize()` | 输入框高度自适应（上限 132px） |
| `bind()` | 绑定登录、发送、回车、停止、切换手机号、面板、滚动、在线/离线事件 |
| `init()` | 从 URL 或 localStorage 恢复手机号，决定进入还是登录 |

#### `admin.js`（管理端）

| 方法 | 用途 |
|---|---|
| `apiKey()` | 从 localStorage 读取管理端 Key |
| `toast(message, kind)` | 轻提示 |
| `readResponse(res)` | 统一解析响应：401 提示 Key 无效，其他错误弹 toast 并抛出，成功返回 JSON |
| `api(path, options)` | 统一 fetch 封装：自动带 `X-API-Key`，JSON 请求体，复用 `readResponse` |
| `apiUpload(path, formData)` | multipart 上传封装：自动带 `X-API-Key`，不设置 `Content-Type`，复用 `readResponse` |
| `text(tag, value, className)` | 创建文本节点元素 |
| `fmtTime(value)` | 时间显示为 `MM-DD HH:mm` |
| `stageBadge(stage)` / `levelText(level)` | 阶段徽标 / 意向等级文本与配色 |
| `initKeyBox()` | API Key 输入与保存状态（内部 `mark()` 刷新状态） |
| `initCustomers()` | 客户页初始化：分页、筛选、抽屉、标签、备注（内部 `params/load/render/rowFor/openDrawer/closeDrawer/fillDrawer/renderTags/patch/refreshRow/addTag`） |
| `initKb()` | 知识库页初始化：集合 Tab、列表、编辑弹窗、保存、删除、重建、文件上传（内部 `load/render/openModal/closeModal/updateCount/save/removeDoc/rebuild/syncTabs/fileExt/fmtSize/openUpload/closeUpload/addFiles/renderUploads/uploadAll`） |
| `init()` | 按 `data-page` 分发到客户页或知识库页 |

---

## 5. 关键流程的方法协作

### 5.1 客户进入

`POST /api/session` → `_json_body` → `_phone` → `pipeline.start_session` → `customers.get_or_create` → `conversations.get_or_create` → `build_greeting` →（新会话）`add_message` → 返回客户/会话/问候语。

### 5.2 一轮问答

`POST /api/chat` → 参数校验 → `ratelimit.check_chat` → `pipeline.start_chat` → `ChatStream.run`：

1. `add_message(user)` + `customers.touch`
2. `conversations.history` + `first_user_message` 控制上下文长度
3. `intent.classify`（`intents` 表 + `rule_classify` 降级）
4. 路由分支：
   - `rag_market` / `rag_course`：`retrieve` → `build_context` / `citations_from_hits`；无命中改 `rag_miss`
   - `web_search` / `sql`：`run_agent` → `execute_tool`（超时 + 只读重试）
   - 其他：`get_llm().stream`
5. 产出 `meta` → `delta`/`tool` → `citation` → `done`
6. `_persist` 落库 → `profiler.schedule` 后台画像

### 5.3 知识库写入

`POST /api/kb/documents` → `_check_limit` → `knowledge.upsert_document` → `split_text` → `embed_documents` → `store.add_chunks(tmp)` → `store.delete_doc(旧)` → `store.add_chunks(正式)` → `store.delete_doc(tmp)` → `kb_db.upsert`。

### 5.4 知识库文件上传

`POST /api/kb/upload`（multipart）→ `_check_limit` → 校验 `collection` → `docparse.extract_text`（PDF/Word/TXT/MD 抽取 + 清洗）→ `knowledge.split_for_documents`（超 20000 字按段落拆分）→ `doc_id_from_file` 生成稳定 doc_id → 对每个片段调用 `upsert_document`（首段用原 id，其余追加 `_2`、`_3`，标题追加 `（i/n）`）→ 汇总 `documents / chunks / doc_ids` 返回。

### 5.5 画像沉淀

`ChatStream.run` 结束 → `profiler.schedule` → `run_extraction` → `_llm_extract` + `extract_with_rules` → `customers.merge_profile` → `auto_stage_by_rounds`。

---

## 6. 全局约定与限制

| 项 | 约定 |
|---|---|
| 时间格式 | 全部为本地时间字符串 `YYYY-MM-DD HH:MM:SS`，无时区字段 |
| 手机号 | 业务唯一身份；列表/详情/SQL 工具默认脱敏，仅 `/api/customers/{id}/contact` 返回明文并记日志 |
| API 鉴权 | 管理端接口统一 `X-API-Key`；`/api/session`、`/api/chat`、`/api/customer-card`、`/api/health` 无鉴权 |
| 错误体 | `{"error": {"code", "message", "field"?}}`；错误码 400/401/404/409/422/429/503 |
| 页面路由 | `/`、`/admin/customers`、`/admin/kb`、`/favicon.ico` |
| 限流 | 单手机号 10 次/60s、全局 60 次/60s、KB 写 20 次/60s；仅单进程有效 |
| 文件上传 | 仅 `POST /api/kb/upload`（需 Key），支持 PDF / Word(.docx) / TXT / Markdown，单文件 ≤10MB；解析后按段落拆分，每条 ≤20000 字；旧版 `.doc` 与无文本层扫描件不支持 |
| 请求体上限 | `MAX_CONTENT_LENGTH = 11MB`（上传 10MB + 1MB 余量），超出返回 413 与中文提示 |
| SQL 安全 | 只读连接 + 单条 SELECT/WITH + 5 表白名单 + authorizer + 5s 超时 + 100 行上限 + phone 脱敏 |
| 上下文控制 | 历史窗口 6 条；超出窗口只保留首条需求 60 字摘要；工具最多 3 轮 |
| 离线降级 | 无 LLM Key → `EchoClient`；无 Embedding Key → 本地哈希向量 + 相对阈值；无 Tavily Key → `TAVILY_NOT_CONFIGURED` |
| 数据保留 | 删除客户级联删除会话与消息；知识条目删除同时清理向量，不可恢复 |
| 并发约束 | Chroma 客户端与 LLM/Embedder 为进程内单例；重建索引使用非阻塞锁，重复请求返回 409 |

---

## 附录：枚举速查

| 枚举 | 取值 |
|---|---|
| `customers.stage` | `new` / `contacted` / `qualified` / `won` / `lost` |
| `customers.intent_level` | `cold` / `warm` / `hot` |
| `messages.role` | `user` / `assistant` / `system` |
| `messages.route` | `rag_market` / `rag_course` / `web_search` / `sql` / `direct` / `rag_miss` |
| `intents.code` | 0 闲聊 / 1 职业咨询 / 2 课程咨询 / 3 实时资讯 / 4 数据查询 |
| `kb_documents.collection` | `market` / `course` / `general` |
| `kb_documents.source` | `manual` / `cms` / `import` / `file:pdf` / `file:docx` / `file:txt` / `file:md` / `file:markdown` |
| `customers.profile` 键 | `current_role` / `years_exp` / `goal` / `budget` / `time_window` / `city` / `concerns` |
| Chroma 集合名 | `kb_market` / `kb_course` / `kb_general` |
| 工具名 | `search_knowledge_base` / `web_search` / `query_crm_db` / `update_customer_profile` |
