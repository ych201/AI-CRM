# SeaChat AI CRM v2.0 · 项目要求达成报告

| # | 项目要求 | 结论 | 主要落地代码 |
|---|---|---|---|
| 1 | 基于伪代码完成整体流程，程序可正式运行 | 已达成 | `run.py`、`app/__init__.py`、`app/core/pipeline.py`、`app/api/chat.py` |
| 2 | 至少涉及一个 function calling 工具定义 | 已达成（4 个业务工具 + 2 个内部工具） | `app/core/agents.py`、`app/core/prompts.py`、`app/llm/client.py` |
| 3 | 本地部署向量数据库，完成创建与更新 | 已达成 | `app/vector/store.py`、`app/core/knowledge.py`、`app/llm/embedding.py` |
| 4 | 创建本地 SQL 数据库并搭建 SQL Agent | 已达成 | `app/db/schema.sql`、`app/db/connection.py`、`app/core/agents.py` |
| 5 | 对外开放向量库更新接口 | 已达成 | `app/api/kb.py`、`app/api/auth.py`、`app/core/knowledge.py` |

技术栈总览：Flask 3 + SQLite(WAL) + Chroma 本地持久化 + DeepSeek `deepseek-chat`(OpenAI 兼容) + 百炼 `text-embedding-v4` + Tavily Search + 原生 JS/SSE。

---

## 一、要求 1：基于伪代码完成整体流程，程序可以正式运行

### 1.1 伪代码 `SeaChatInterview(phone_number, query)` 的落点

| 伪代码步骤 | 系统实现 | 代码位置 |
|---|---|---|
| 入参 `phone_number` 识别身份、取历史 | 手机号 upsert 客户 + 复用 24h 内会话 | `pipeline.start_session` / `start_chat` |
| 查询用户信息（SQL） | SQLite `customers` / `conversations` / `messages` | `app/db/*` |
| 意图识别 | 一次强制 `tool_choice` 的 function calling + 关键词降级 | `app/core/intent.py:120` |
| 意图分流三分支 | `rag_market` / `rag_course` / `web_search` / `sql` / `direct` | `app/core/pipeline.py:172` |
| 检索 / 联网 / 查库 | Chroma 检索、Tavily、只读 SQL | `retriever.py`、`agents.py` |
| 按策略生成回复 | 画像 → 话术策略 → system prompt | `app/core/strategies.py` |
| 流式返回 | SSE（`meta`/`delta`/`tool`/`citation`/`done`/`error`） | `app/api/chat.py:44` |
| 落库 + 画像沉淀 | `messages` 落库、后台线程更新 `customers.profile` | `pipeline.py:253`、`profiler.py` |

### 1.2 关键代码

应用工厂 + 蓝图装配（`init_db()` 建表在启动时幂等执行）：

```73:123:app/__init__.py
def create_app(test_config: dict | None = None) -> Flask:
    cfg = get_config()
    ...
    init_db()
    _register_blueprints(app)
    _register_error_handlers(app)
```

SSE 接口：后台线程产出事件 + 主线程 15s 心跳保活：

```44:75:app/api/chat.py
def event_stream(session: pipeline.ChatStream):
    """后台线程产出事件，主线程负责保活与格式化为 SSE。"""
    box: queue.Queue = queue.Queue()

    def produce() -> None:
        try:
            for event in session.run():
                box.put(event)
        ...
```

主链路（意图 → 分流 → 生成 → 落库 → 画像）：

```172:246:app/core/pipeline.py
            intent = classify(
                self.message,
                summary,
                profile_summary(customer.get("profile"), customer.get("name")),
            )
            route = intent.route
            ...
            if route in ("web_search", "sql"):
                for kind, payload in run_agent(llm_messages, customer["id"], conv_id, route):
                    ...
            else:
                usage_out: dict = {}
                for delta in get_llm().stream(
                    llm_messages, temperature=0.6, usage_out=usage_out
                ):
                    ...
```

### 1.3 运行方式

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env        # 可留空，系统自动进入离线演示模式
python scripts/init_db.py     # 建表 + 意图字典（幂等）
python scripts/seed_kb.py     # 可选：导入 7 条示例知识
python run.py                 # http://127.0.0.1:5000
```

- 入口：`run.py`（开发模式 `threaded=True`，生产用 waitress 8 线程）。
- 页面：用户端 `/`、`/chat`；管理端 `/admin/login`、`/admin/customers`、`/admin/kb`。
- **无密钥也能跑**：未配置 `DEEPSEEK_API_KEY` / `DASHSCOPE_API_KEY` 时自动降级为 `EchoClient` + 本地 n-gram 哈希向量，整条链路不崩。

### 1.4 相关技术

- Flask 3 应用工厂 + Blueprint 分层（`api` / `core` / `db` / `llm` / `vector`）。
- SSE（`text/event-stream`）+ `stream_with_context` + 生产者线程 + `queue` 解耦、心跳 `: ping`。
- 结构化 JSON 日志（`app/config.py:143` `JsonFormatter`），日志中手机号只落 SHA-256 前 8 位。
- 全局异常 → 统一错误码（`UNPROCESSABLE` / `RATE_LIMITED` / `SQL_REJECTED` 等）。
- 进程内滑动窗口限流（`app/core/ratelimit.py`）。

---

## 二、要求 2：至少一个 function calling 工具定义

### 2.1 工具清单（共 4 个业务工具 + 2 个内部工具）

| 工具名 | 作用 | 定义位置 |
|---|---|---|
| `search_knowledge_base` | 检索本地知识库（market/course/general） | `app/core/agents.py:23` |
| `web_search` | Tavily 联网检索实时行情 | `app/core/agents.py:40` |
| `query_crm_db` | 只读 SQL 查 CRM（**SQL Agent 的核心工具**） | `app/core/agents.py:55` |
| `update_customer_profile` | 更新客户画像（白名单字段） | `app/core/agents.py:70` |
| `route_intent` | 意图识别，`tool_choice` 强制调用 | `app/core/prompts.py:17` |
| `update_customer_profile`（独立副本） | 后台画像抽取 | `app/core/prompts.py:70` |

工具定义是标准 OpenAI function calling schema（`type: function` + JSON Schema 参数）：

```55:69:app/core/agents.py
    {
        "type": "function",
        "function": {
            "name": "query_crm_db",
            "description": "用只读 SQL 查询 CRM 数据库。可查 customers、conversations、messages、kb_documents、intents。只允许单条 SELECT。",
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "单条 SELECT 语句，不要带分号"},
                    "purpose": {"type": "string", "description": "一句话说明查询目的"},
                },
                "required": ["sql", "purpose"],
            },
        },
    },
```

### 2.2 Agent 循环

```380:451:app/core/agents.py
def run_agent(
    messages: list[dict],
    customer_id: int,
    conversation_id: int,
    route: str,
    max_steps: int | None = None,
) -> Iterator[tuple[str, Any]]:
    """工具调用循环：产出 ("delta", text) / ("tool", info)。"""
    ...
    for step in range(steps):
        resp = llm.chat(messages, tools=tools, tool_choice="auto", temperature=0.6)
        ...
        if not resp.tool_calls:
            ...
            return
        messages.append(_assistant_tool_message(resp.content, resp.tool_calls))
        for call in resp.tool_calls:
            ...
            result = execute_tool(call.name, call.arguments or {}, ctx)
            ...
            messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)[:6000]})
```

### 2.3 相关技术

- OpenAI Python SDK 的 `tools` / `tool_choice` 原生函数调用；DeepSeek 通过 OpenAI 兼容端点接入。
- `role: assistant` 的 `tool_calls` 与 `role: tool` + `tool_call_id` 的消息协议回灌。
- 流式与非流式混用：工具轮走非流式 `chat()`（需要完整 `tool_calls`），最终回答走 `stream()` 或对非流式结果切片下发（首字仍即刻可见）。

---

## 三、要求 3：本地部署向量数据库，完成创建与更新

### 3.1 部署形态

- 使用 **Chroma `PersistentClient`**（嵌入式、本地磁盘持久化），不是 Docker/HttpClient，无需额外服务进程。
- 落盘路径：`CHROMA_PATH`，默认 `./data/chroma`（`app/config.py:77`）。
- 三个 collection：`kb_market` / `kb_course` / `kb_general`，距离度量余弦（`hnsw:space=cosine`）。

```33:64:app/vector/store.py
def get_client() -> Any:
    global _client, _client_path
    cfg = get_config()
    with _lock:
        if _client is not None and _client_path == cfg.chroma_path:
            return _client
        import chromadb
        from chromadb.config import Settings

        cfg.chroma_path.mkdir(parents=True, exist_ok=True)
        _client = chromadb.PersistentClient(
            path=str(cfg.chroma_path),
            settings=Settings(anonymized_telemetry=False, allow_reset=True),
        )
        ...
```

### 3.2 创建（首次自动）

`get_or_create_collection` 保证"首次访问即创建"，`add_chunks` 负责切片写入：

```86:110:app/vector/store.py
def add_chunks(
    collection_key: str,
    doc_id: str,
    title: str,
    chunks: list[str],
    embeddings: list[list[float]],
) -> int:
    ...
    ids = [f"{doc_id}::{i}" for i in range(len(chunks))]
    metadatas = [
        {
            "doc_id": doc_id,
            "collection": collection_key,
            "title": title,
            "chunk_index": i,
            "updated_at": now,
        }
        for i in range(len(chunks))
    ]
```

### 3.3 更新（幂等 + 失败不毁旧数据）

更新走"临时前缀 chunk → 成功后删旧 → 改回正式 doc_id → 最后写元数据"的两阶段写法：

```136:158:app/core/knowledge.py
    embeddings = get_embedder().embed_documents(chunks)  # 失败直接抛 503，此时尚未动旧数据
    tmp_id = f"{doc_id}::tmp::{uuid.uuid4().hex[:8]}"

    try:
        store.add_chunks(collection, tmp_id, title, chunks, embeddings)
    except Exception as exc:  # noqa: BLE001
        logger.error("staging chunks failed", extra={"error": str(exc)})
        _safe_disable(doc_id, existing)
        raise UpstreamUnavailable("向量库写入失败") from exc

    try:
        store.delete_doc(collection, doc_id)
        if existing and existing.get("collection") and existing["collection"] != collection:
            store.delete_doc(existing["collection"], doc_id)
        n = store.add_chunks(collection, doc_id, title, chunks, embeddings)
        store.delete_doc(collection, tmp_id)
```

配套能力：

| 能力 | 实现 |
|---|---|
| 切片 | `RecursiveCharacterTextSplitter`，chunk 500 / overlap 80，中文标点分隔符（`knowledge.split_text`） |
| 去重 | `kb_documents.id` 主键 + Chroma `doc_id` metadata；同 `doc_id` 重复提交只更新（`action=updated`） |
| 上传幂等 | `doc_id_from_file`：文件名 slug + 内容 SHA-1 前 6 位，同一文件重复上传不产生重复切片 |
| 删除 | `delete_document`：先删向量再删元数据，失败抛 503 |
| 全量重建 | `rebuild()`：清空 collection → 按 SQLite 真相源逐条重新 embedding；`_rebuild_lock` 防并发 |
| 健康检查 | `store.heartbeat()` 暴露在 `GET /api/health` |

`kb_documents` 是真相源，向量可随时重建：

```43:55:app/db/schema.sql
-- 知识条目：SQLite 是真相源，向量在 Chroma，可按此重建
CREATE TABLE IF NOT EXISTS kb_documents (
  id          TEXT PRIMARY KEY,                       -- doc_id，与 Chroma metadata 一致
  collection  TEXT    NOT NULL,                       -- market|course|general
  ...
```

### 3.4 Embedding

- 线上：百炼 `text-embedding-v4`，1024 维，**批量 ≤ 10**，单批失败指数退避重试 1 次。
- 离线：字符 1-gram + 2-gram 哈希到 1024 维并 L2 归一化，保证无密钥可测。
- 检索相似度：`score = 1 - cosine_distance`，默认阈值 0.35（`RAG_SCORE_THRESHOLD`）；离线模式改相对阈值避免长文档天然低分被全过滤。

### 3.5 相关技术

- ChromaDB 嵌入式持久化 + HNSW 余弦索引 + metadata 过滤（`delete(where={"doc_id": ...})`）。
- `langchain-text-splitters` 中文友好递归切片。
- 百炼 embedding（OpenAI 兼容 `/embeddings`），`dimensions` 参数 + 老 SDK `TypeError` 兼容降级。

---

## 四、要求 4：本地 SQL 数据库 + SQL Agent

### 4.1 选型：SQLite（按要求）

- 单文件 `./data/crm.db`，`PRAGMA journal_mode=WAL` + `synchronous=NORMAL` + `busy_timeout=5000`，兼顾读并发与写稳定。
- 读写连接分离：`write_conn()` 自动 commit / rollback；`read_conn()` 以 `?mode=ro` URI 打开并 `PRAGMA query_only=ON`。

```29:40:app/db/connection.py
def connect(readonly: bool = False) -> sqlite3.Connection:
    """普通读写连接。调用方负责 commit / close（或用 conn() 上下文）。"""
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if readonly:
        uri = f"file:{path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    else:
        conn = sqlite3.connect(str(path), timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
```

5 张表：`customers` / `conversations` / `messages` / `kb_documents` / `intents`，DDL 幂等（`CREATE TABLE IF NOT EXISTS`），`init_db()` 在 `create_app()` 时执行。

### 4.2 SQL Agent 的三道防线

LLM 生成 SQL 后必须通过三层校验才允许执行：

**第一层：文本校验 `validate_sql`**

```127:151:app/core/agents.py
def validate_sql(sql: str) -> str:
    text = (sql or "").strip()
    if not text:
        raise SQLRejected("SQL 为空")
    if ";" in text:
        raise SQLRejected("仅允许单条语句，不要带分号")
    if not re.match(r"^\s*(SELECT|WITH)\b", text, re.IGNORECASE):
        raise SQLRejected("仅允许 SELECT/WITH 查询")
    if "--" in text or "/*" in text:
        raise SQLRejected("不允许 SQL 注释")
    if _FORBIDDEN.search(text):
        raise SQLRejected("SQL 含禁止的关键字（写操作或 PRAGMA）")
    tables = {t.lower() for t in _TABLE_REF.findall(text)}
    ...
    illegal = tables - ALLOWED_TABLES - cte_names
    if illegal:
        raise SQLRejected(f"仅允许查询 {', '.join(sorted(ALLOWED_TABLES))} 等业务表")
```

**第二层：只读连接 + `set_authorizer` 回调**（数据库层拒绝非 SELECT，含拒绝 `sqlite_master`）

```154:173:app/core/agents.py
def _make_authorizer(deadline: float):
    allowed = {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_RECURSIVE,
    }

    def authorizer(action: int, arg1: Any, arg2: Any, dbname: Any, trigger: Any) -> int:
        if time.monotonic() > deadline:
            return sqlite3.SQLITE_DENY
        if action in allowed:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            table = str(arg1 or "").lower()
            if table in ALLOWED_TABLES:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_DENY
```

**第三层：执行期约束**（5s 超时 progress handler、100 行上限、手机号脱敏）

```182:204:app/core/agents.py
def run_sql(sql: str) -> dict:
    """只读执行单条 SELECT，行数上限 100，phone 脱敏。"""
    statement = validate_sql(sql)
    deadline = time.monotonic() + SQL_TIMEOUT_MS / 1000.0
    conn = connect(readonly=True)
    try:
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
        conn.set_authorizer(_make_authorizer(deadline))
        cur = conn.execute(statement)
        columns = [d[0] for d in (cur.description or [])]
        rows = cur.fetchmany(MAX_ROWS + 1)
        ...
```

### 4.3 Agent 闭环

`query_crm_db` 工具 → `_tool_query_crm_db` → `run_sql` → 结果 JSON 回填 → 模型用自然语言回答。`SQL_REJECTED` 不会中断对话，而是作为工具结果回填，模型自行纠正重试（实测中模型先猜了不存在的 `status` 列，随后自动改查 `stage` 并成功）。

### 4.4 相关技术

- `sqlite3`：URI 只读模式、`set_authorizer`、`set_progress_handler`、WAL、`row_factory=Row`。
- 正则静态分析 + SQLite 引擎级鉴权双重防护（静态规则可被绕过，引擎级不可）。
- 行级脱敏：`mask_phone`（`138****8000`），日志不落明文号码。

---

## 五、要求 5：对外开放向量数据库更新接口

### 5.1 接口清单（Blueprint 前缀 `/api/kb`）

| 方法 | 路径 | 作用 | 鉴权 |
|---|---|---|---|
| `POST` | `/api/kb/documents` | 新建 / 更新知识条目（按 `doc_id` 幂等 upsert） | 管理员 Session 或 `X-API-Key` |
| `POST` | `/api/kb/upload` | 上传 PDF / Word(.docx) / TXT / Markdown，解析后自动切片入库 | 同上 |
| `DELETE` | `/api/kb/documents/<doc_id>` | 删除条目（同步删向量） | 同上 |
| `GET` | `/api/kb/documents` | 分页列表（可按 collection 过滤） | 同上 |
| `GET` | `/api/kb/documents/<doc_id>` | 取全文 | 同上 |
| `POST` | `/api/kb/rebuild` | 重建索引（`market` / `course` / `general` / `all`） | 同上 |

写接口带限流（默认 20 次/60s）：

```41:67:app/api/kb.py
@bp.post("/documents")
@require_admin
def upsert_document():
    _check_limit()
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise InvalidArgument("请求体必须是 JSON 对象")

    doc_id = str(data.get("doc_id") or "").strip() or None
    collection = str(data.get("collection") or "").strip()
    title = str(data.get("title") or "").strip() or None
    content = data.get("content")
    source = str(data.get("source") or "manual").strip() or "manual"
    ...
    result = knowledge.upsert_document(
        collection=collection,
        content=content,
        title=title,
        doc_id=doc_id,
        source=source,
    )
```

鉴权（Session 优先，兼容脚本用 API Key）：

```91:100:app/api/auth.py
def require_admin(fn):
    """管理端接口鉴权：Session 优先，其次兼容 X-API-Key。"""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not is_authenticated():
            raise Unauthorized("请先登录管理后台")
        return fn(*args, **kwargs)

    return wrapper
```

### 5.2 调用示例（外部系统对接）

```bash
# 新增/更新一条知识（幂等：同 doc_id 覆盖）
curl -s -X POST http://127.0.0.1:5000/api/kb/documents \
  -H 'Content-Type: application/json' -H 'X-API-Key: change-me-please' \
  -d '{"doc_id":"course_pricing","collection":"course","title":"价格表","content":"实战班 9800 元"}'

# 上传文件（-F 表单）
curl -s -X POST http://127.0.0.1:5000/api/kb/upload \
  -H 'X-API-Key: change-me-please' \
  -F 'collection=course' -F 'file=@outline.docx'

# 删除
curl -s -X DELETE http://127.0.0.1:5000/api/kb/documents/course_pricing \
  -H 'X-API-Key: change-me-please'

# 重建索引
curl -s -X POST http://127.0.0.1:5000/api/kb/rebuild \
  -H 'Content-Type: application/json' -H 'X-API-Key: change-me-please' \
  -d '{"collection":"all"}'
```

### 5.3 接口对外可用性细节

- 参数校验：`collection` 枚举、`content` 非空且 ≤ 20000 字符、`doc_id` 仅 `[A-Za-z0-9_.-]{1,80}`，非法输入返回 422 + `field`。
- 长文件分片：`split_for_documents` 按段落切成 ≤ 20000 字符的多条文档，返回 `doc_ids` 数组。
- 上传大小限制 10MB，超限由全局异常处理返回中文提示。
- 并发重建保护：`_rebuild_lock`，重复请求返回 409 `索引重建正在进行中`。
- 错误码统一：401 未鉴权 / 404 不存在 / 409 冲突 / 422 参数错误 / 429 限流 / 503 向量服务不可用。

### 5.4 相关技术

- RESTful Blueprint + `@require_admin` 装饰器、Flask Session（`hmac.compare_digest` 常量时间比较）。
- 上传解析：`pypdf` / `python-docx` / 文本编码探测（`app/core/docparse.py`）。
- 切片与入库复用同一函数 `knowledge.upsert_document`（脚本 `seed_kb.py`、管理端页面、开放接口三条入口行为一致）。

---

## 附录：文件与职责对照

| 文件 | 职责 |
|---|---|
| `run.py` | 启动入口（开发 / waitress） |
| `app/__init__.py` | 应用工厂、蓝图注册、统一错误处理 |
| `app/config.py` | `.env` 配置、路径解析、JSON 结构化日志 |
| `app/api/chat.py` | `/api/session`、`/api/chat`(SSE)、`/api/customer-card` |
| `app/api/kb.py` | `/api/kb/*` 知识库开放接口 |
| `app/api/customers.py` | `/api/customers*` 管理端客户接口 |
| `app/api/health.py` | `/api/health` 四依赖探活 |
| `app/core/pipeline.py` | 对话编排与 SSE 事件生成 |
| `app/core/intent.py` | 意图识别（强制工具调用 + 关键词降级） |
| `app/core/agents.py` | **工具定义、Agent 循环、SQL Agent 与守卫** |
| `app/core/knowledge.py` | 切片、入库、删除、重建索引 |
| `app/core/retriever.py` | 检索、context 拼装、引用生成 |
| `app/core/profiler.py` | 画像抽取（LLM + 正则兜底） |
| `app/core/strategies.py` | 画像 → 话术策略 |
| `app/db/schema.sql` | 5 张表 DDL（幂等） |
| `app/db/connection.py` | 只读/读写连接、`init_db` |
| `app/llm/client.py` | DeepSeek 客户端 + 离线 Echo 客户端 |
| `app/llm/embedding.py` | 百炼 embedding + 本地哈希向量 |
| `app/vector/store.py` | **Chroma 客户端、collection、增删改查** |
| `scripts/init_db.py` / `seed_kb.py` / `smoke_test.py` | 建表 / 示例知识 / 端到端冒烟 |
| `tests/*.py` | 53 个用例 |
