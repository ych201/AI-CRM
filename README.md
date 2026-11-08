# SeaChat AI CRM（MVP）

按 `AI-CRM-SDD.md` v1.0 实现的 AI 职业咨询 CRM：手机号进入 → 意图分流（RAG / 联网 / SQL / 直答）→ 流式回答 → 画像沉淀 → 顾问后台跟进。

- 后端：Flask 3 + SQLite（WAL）+ Chroma 本地向量库
- 模型：DeepSeek `deepseek-chat`（OpenAI 兼容）、百炼 `text-embedding-v4`、Tavily Search
- 前端：原生 HTML + Jinja2 + 原生 JS（无构建链、无 CDN，可离线打开）
- 单进程多线程，目标 20 并发在线

---

## 1. 快速开始

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env          # Windows: copy .env.example .env

python scripts/init_db.py     # 建表 + 意图字典（幂等）
python scripts/seed_kb.py     # 可选：导入 7 条示例知识（13 个切片）
python run.py                 # http://127.0.0.1:5000
```

三个页面：

| 路由 | 说明 |
|---|---|
| `/` | 客户端聊天页 |
| `/admin/customers` | 客户列表 + 详情抽屉（右上角填 `ADMIN_API_KEY`） |
| `/admin/kb` | 知识库管理（新建 / 编辑 / 删除 / 上传 PDF·Word·TXT·Markdown / 重建索引） |

> 端口被占用时：设置环境变量 `PORT=5055` 再启动。
> 管理端 API Key 默认 `change-me-please`，只存在浏览器 `localStorage`，请在上线前改 `.env`。

### 离线演示模式

未配置 `DEEPSEEK_API_KEY` / `DASHSCOPE_API_KEY` 时系统**不会崩溃**，会自动降级：

| 能力 | 降级行为 |
|---|---|
| 回答生成 | `EchoClient`：基于知识库 `<context>` 或 SQL 工具结果生成简短回答 |
| 意图识别 | 关键词规则（`app/core/intent.py`），结果在 `meta` 中标注来源 |
| 画像抽取 | 正则规则（`app/core/profiler.py`），如「我现在做行政，想转 AI 产品，预算一万以内」 |
| Embedding | 本地字符 n-gram 哈希向量（1024 维），检索改用相对阈值 |
| 联网搜索 | 返回 `TAVILY_NOT_CONFIGURED`，由模型说明需要联网 |

因此 `pytest`、`scripts/smoke_test.py` 与完整对话链路在**没有任何密钥**的环境下也能跑通。
填入密钥后自动切回线上实现，无需改代码；`GET /api/health` 的 `mode` 字段会显示当前模式。

> 注意：离线向量与真实 embedding 不兼容。切换 `DASHSCOPE_API_KEY` 后请执行一次「重建索引」。

---

## 2. 目录结构

```
AI-CRM-v2.0/
├─ app/
│  ├─ __init__.py            # create_app、错误处理
│  ├─ config.py              # .env 读取、结构化 JSON 日志
│  ├─ api/
│  │  ├─ auth.py             # X-API-Key 鉴权（hmac.compare_digest）
│  │  ├─ chat.py             # /api/session、/api/chat(SSE)、/api/customer-card
│  │  ├─ customers.py        # /api/customers*
│  │  ├─ kb.py               # /api/kb/*
│  │  ├─ health.py           # /api/health
│  │  └─ pages.py            # 三个页面路由
│  ├─ core/
│  │  ├─ pipeline.py         # 编排 + SSE 事件生成
│  │  ├─ intent.py           # 意图识别 + 关键词降级
│  │  ├─ retriever.py        # 检索 + context/引用组装
│  │  ├─ agents.py           # 4 个工具、Agent 循环、SQL 守卫
│  │  ├─ profiler.py         # 画像抽取（后台线程）
│  │  ├─ knowledge.py        # 切片、入库、删除、重建索引
│  │  ├─ docparse.py         # PDF / Word / TXT / Markdown 文本抽取
│  │  ├─ ratelimit.py        # 进程内滑动窗口限流
│  │  ├─ strategies.py       # 画像 → 回答策略
│  │  ├─ prompts.py          # Prompt 常量
│  │  └─ errors.py           # ApiError / 错误码
│  ├─ db/
│  │  ├─ schema.sql          # DDL（幂等）
│  │  ├─ connection.py       # 读写连接 / 只读连接
│  │  ├─ customers.py
│  │  ├─ conversations.py
│  │  └─ kb.py
│  ├─ llm/
│  │  ├─ client.py           # DeepSeek 客户端 + 离线 Echo 客户端
│  │  └─ embedding.py        # 百炼 embedding（批量 ≤10）+ 本地向量
│  ├─ vector/store.py        # Chroma 客户端与 collection 管理
│  ├─ templates/             # base / chat / admin_customers / admin_kb
│  └─ static/                # css/app.css, js/chat.js, js/admin.js
├─ data/                     # 运行时生成：crm.db、chroma/
├─ scripts/
│  ├─ init_db.py
│  ├─ seed_kb.py
│  └─ smoke_test.py          # 端到端冒烟
├─ tests/                    # pytest：意图 / 对话链路 / 知识库 / 检索 / SQL 守卫
├─ .env.example
├─ requirements.txt
├─ run.py
└─ README.md
```

---

## 3. 配置

`.env.example` 已列出全部变量，关键项：

| 变量 | 说明 |
|---|---|
| `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` | 对话模型 |
| `DASHSCOPE_API_KEY` / `EMBEDDING_MODEL` / `EMBEDDING_DIM` / `EMBEDDING_BATCH` | 向量化（v4，1024 维，批量 ≤10） |
| `TAVILY_API_KEY` | 联网搜索 |
| `SQLITE_PATH` / `CHROMA_PATH` | 默认 `./data/crm.db`、`./data/chroma` |
| `ADMIN_API_KEY` | 管理端 `X-API-Key` |
| `RAG_TOP_K` / `RAG_SCORE_THRESHOLD` | 默认 4 / 0.35 |
| `GREETING_USE_LLM` | 老客户问候语是否走一次短调用（5s 超时降级） |
| `RATE_LIMIT_PHONE` / `RATE_LIMIT_GLOBAL` / `RATE_LIMIT_KB` | 10 / 60 / 20 次每 60s |

---

## 4. 接口

| Method | Path | 鉴权 | 说明 |
|---|---|---|---|
| POST | `/api/session` | 无 | 手机号进入会话，返回客户、会话 ID、问候语 |
| POST | `/api/chat` | 无 | 主对话接口，`text/event-stream` |
| GET | `/api/customers` | Key | 客户列表（`q` / `stage` / `intent_level` / 分页）+ 统计 |
| GET | `/api/customers/{id}` | Key | 客户详情 + 画像 + 会话 + 最近 50 条消息 |
| PATCH | `/api/customers/{id}` | Key | 改阶段 / 意向 / 标签 / 备注 |
| DELETE | `/api/customers/{id}` | Key | 删除客户（级联会话与消息，P1） |
| GET | `/api/customers/{id}/contact` | Key | 「查看完整手机号」审计接口（P1，见 §6） |
| GET | `/api/kb/documents` | Key | 知识条目列表（`collection` / `show_disabled`） |
| GET | `/api/kb/documents/{doc_id}` | Key | 单条全文（编辑弹窗用，见 §6） |
| POST | `/api/kb/documents` | Key | 新增或覆盖更新（对外开放，幂等） |
| POST | `/api/kb/upload` | Key | 上传 PDF / Word(.docx) / TXT / Markdown，解析后切片入库（单文件 ≤ 10MB） |
| DELETE | `/api/kb/documents/{doc_id}` | Key | 删除条目与全部向量 |
| POST | `/api/kb/rebuild` | Key | 按 collection 重建索引（并发返回 409） |
| GET | `/api/health` | 无 | SQLite / Chroma / LLM / Embedding 探测 |
| GET | `/api/customer-card` | 无 | 聊天页客户卡（`with_messages=1` 带最近 20 条，见 §6） |

错误统一为：

```json
{ "error": { "code": "INVALID_ARGUMENT", "message": "phone 格式不正确", "field": "phone" } }
```

错误码：`INVALID_ARGUMENT` 400 / `UNAUTHORIZED` 401 / `NOT_FOUND` 404 / `CONFLICT` 409 /
`UNPROCESSABLE` 422 / `RATE_LIMITED` 429 / `INTERNAL` 500 / `UPSTREAM_UNAVAILABLE` 503。

### SSE 事件

```
event: meta       {"conversation_id":7,"message_id":null,"intent_code":2,"intent_name":"课程咨询",
                   "route":"rag_course","customer":{"stage":"new","intent_level":"cold"},
                   "intent_low_confidence":false}
event: delta      {"t":"这门课"}
event: tool       {"name":"query_crm_db","args":{...},"ms":820}
event: citation   {"items":[{"doc_id":"course_pricing","title":"2026 年课程价格表","score":0.82,"snippet":"…"}]}
event: done       {"message_id":88,"latency_ms":1830,"tokens":{"in":1240,"out":321},
                   "persisted":true,"customer":{...}}
event: error      {"code":"UPSTREAM_TIMEOUT","message":"模型响应超时","retryable":true}
```

- 每 15s 发送一次 `: ping` 注释行保活
- 客户端断开时停止生成，已生成内容落库并追加 `[truncated]`
- `error` 之后不再发送 `done`

---

## 5. 关键实现

**分流**：意图识别一次 `tool_choice` 强制调用（`route_intent`，`temperature=0`）；
失败或非法枚举走关键词规则；`confidence < 0.55` 转 `direct` 并透出 `intent_low_confidence`。
意图 1/2 走**预检索**（先查再生成，省一轮往返）；意图 3/4 走工具循环（最多 3 轮）；
检索为空时 `rag_*` 降级为 `rag_miss` 并要求模型说明「知识库暂无资料」。

**RAG 写入**：按 SDD 7.4 的「临时前缀 → 删旧 → 改回正式 doc_id → 写元数据」顺序，
写入前先完成全部 embedding，失败时旧数据保持可用；`kb_documents` 是真相源，可随时重建。

**工具**：`search_knowledge_base`、`web_search`、`query_crm_db`、`update_customer_profile`
（OpenAI tools 格式，超时 + 只读工具重试 1 次，异常转 `{"error": ...}` 回填模型）。

**SQL 守卫**（MUST）：只读连接（`mode=ro`）+ 仅 `SELECT/WITH` + 禁止多语句/注释/写操作/PRAGMA +
表白名单（`customers/conversations/messages/kb_documents/intents`）+ `set_authorizer` 二次校验 +
5s 进度中断 + `fetchmany(100)` + `phone` 脱敏。

**画像**：每轮 `done` 后用后台线程抽取，白名单字段合并；`intent_level=hot` 且 `goal` 非空自动置
`qualified`；`new → contacted` 由 ≥3 轮对话自动流转。

**回答策略**：按 `stage` / `intent_level` / `profile` 渲染策略指令写入 system prompt
（新客先确认处境、contacted 给两个选项、qualified 直接给下一步、won/lost 只答不推销）。

**限流**：进程内滑动窗口，单手机号 10 次/60s、全局 60 次/60s、KB 写操作 20 次/60s。
仅单进程有效；多 worker 需换 Redis（SDD 9.4 明确本期不做）。

---

## 6. 与 SDD 的差异 / 扩展（实现期补充）

1. **`GET /api/customer-card`**（无 Key）：聊天页刷新客户卡与恢复历史用。手机号即身份，与 `/api/chat` 同级鉴权；不返回完整号码以外的敏感信息。
2. **`GET /api/customers/{id}/contact`**（Key）：SDD 9.3 的 P1「查看完整」审计接口，供抽屉「打开对话」使用；列表与详情仍只返回脱敏号码。
3. **`GET /api/kb/documents/{doc_id}`**（Key）：编辑弹窗取全文，列表接口不返回 `content`。
4. **离线降级链路**：SDD 假设外部服务可用；本项目补充了无密钥可运行的 `EchoClient` 与本地向量，仅用于开发/演示/单测，不代表线上能力。
5. **离线检索阈值**：本地哈希向量与真实 embedding 分布不同，离线模式改用相对阈值（top × 0.6，下限 0.05）；线上仍为 `0.35`。
6. **生成阶段不再二次流式**：工具循环拿到最终文本后按 10 字切片下发，避免多一次生成（用户可见效果一致，首字更快）。
7. **问候语**：模板优先，`GREETING_USE_LLM=true` 且已配置模型时，老客户额外走一次 5s 短调用（失败静默降级）。
8. **静态 JS 用原生 `fetch` + `ReadableStream`** 解析 SSE（`EventSource` 不支持 POST body）。
9. **依赖精简**：用官方 `openai` SDK 直连 DeepSeek / 百炼的 OpenAI 兼容端点，保留 `langchain-text-splitters` 做切片，未引入完整 LangChain 链路（SDD 4.4 的 LangChain 属于选型建议，接口契约与行为不变）。

---

## 7. 测试与验收

```bash
# 单元测试（无需外部服务）
.venv\Scripts\python.exe -m pytest -q

# 端到端冒烟：健康检查 → 会话 → 课程问答（含 citation）→ SQL Agent → 落库 → 鉴权
.venv\Scripts\python.exe scripts/smoke_test.py
```

冒烟覆盖（当前全部 PASS）：

- 新手机号进入返回问候语；流式回答含 `meta` / `delta` / `citation` / `done`
- 「AI 产品经理课程多少钱」→ `intent_code=2`、`route=rag_course`、带引用
- 「今天新增了几个客户」→ 触发 `query_crm_db` 工具事件
- 画像「我现在做行政，想转 AI 产品，预算一万以内」→ `current_role` / `goal` / `budget` 写入，`intent_level=hot`，`stage=qualified`
- 消息落库；管理端无 Key 返回 401

手工验收（`curl` 或页面）：

```bash
curl -N -X POST localhost:5000/api/chat -H 'Content-Type: application/json' \
  -d '{"phone":"13800138000","message":"课程多少钱"}'

curl -s -X POST localhost:5000/api/kb/documents \
  -H 'Content-Type: application/json' -H 'X-API-Key: change-me-please' \
  -d '{"doc_id":"course_pricing","collection":"course","title":"价格表","content":"实战班 9800 元"}'

curl -s -X POST localhost:5000/api/kb/upload \
  -H 'X-API-Key: change-me-please' \
  -F 'collection=course' -F 'file=@课程大纲.pdf'
```

---

## 8. 已知限制

- 手机号即身份，任何人可冒充（SDD R4，明确仅本机/内网演示）。
- 限流与重建锁为进程内实现，多 worker 失效；部署请用 `--threads 4 --workers 1`。
- 不做暗色模式、不做多租户/RBAC。文件上传支持 `.pdf` / `.docx` / `.txt` / `.md`；旧版 `.doc` 与无文本层的扫描版 PDF 不支持（需先转 `.docx` 或 OCR）。
- 外部服务均为「超时 + 重试 1 次」，未做熔断与降级缓存；上游长时间不可用时回答会以 `error` 事件结束。
- 离线模式的回答质量仅用于链路演示，不用于内容验收。
