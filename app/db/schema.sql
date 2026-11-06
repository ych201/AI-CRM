-- SeaChat AI CRM · SQLite DDL（幂等）
-- 时间统一为本地时间字符串 YYYY-MM-DD HH:MM:SS

CREATE TABLE IF NOT EXISTS customers (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  phone          TEXT    NOT NULL UNIQUE,
  name           TEXT,
  stage          TEXT    NOT NULL DEFAULT 'new',      -- new|contacted|qualified|won|lost
  intent_level   TEXT    NOT NULL DEFAULT 'cold',     -- cold|warm|hot
  tags           TEXT    NOT NULL DEFAULT '[]',       -- JSON array[string]
  profile        TEXT    NOT NULL DEFAULT '{}',       -- JSON object，见 SDD 5.3
  note           TEXT,
  created_at     TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
  last_active_at TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_customers_active ON customers(last_active_at DESC);
CREATE INDEX IF NOT EXISTS idx_customers_stage  ON customers(stage);

CREATE TABLE IF NOT EXISTS conversations (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  title       TEXT,
  created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_conv_customer ON conversations(customer_id);

CREATE TABLE IF NOT EXISTS messages (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role            TEXT    NOT NULL,                   -- user|assistant|system
  content         TEXT    NOT NULL,
  intent_code     INTEGER,
  route           TEXT,                               -- rag_market|rag_course|web_search|sql|direct|rag_miss
  citations       TEXT    NOT NULL DEFAULT '[]',      -- JSON array
  latency_ms      INTEGER,
  tokens_in       INTEGER,
  tokens_out      INTEGER,
  created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id, id);

-- 知识条目：SQLite 是真相源，向量在 Chroma，可按此重建
CREATE TABLE IF NOT EXISTS kb_documents (
  id          TEXT PRIMARY KEY,                       -- doc_id，与 Chroma metadata 一致
  collection  TEXT    NOT NULL,                       -- market|course|general
  title       TEXT    NOT NULL,
  content     TEXT    NOT NULL,
  source      TEXT,                                   -- manual|cms|import
  chunk_count INTEGER NOT NULL DEFAULT 0,
  enabled     INTEGER NOT NULL DEFAULT 1,
  created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_kb_collection ON kb_documents(collection, updated_at DESC);

-- 意图字典（可运营调整，不改代码）
CREATE TABLE IF NOT EXISTS intents (
  code        INTEGER PRIMARY KEY,
  name        TEXT NOT NULL,
  description TEXT NOT NULL,
  route       TEXT NOT NULL,
  enabled     INTEGER NOT NULL DEFAULT 1
);

INSERT OR IGNORE INTO intents (code, name, description, route) VALUES
 (0, '闲聊/其他',   '问候、感谢、无法归类的输入',                       'direct'),
 (1, '职业咨询',    '转行可行性、岗位要求、学习路径、行业前景',            'rag_market'),
 (2, '课程咨询',    '课程内容、价格、周期、师资、报名方式、就业服务',      'rag_course'),
 (3, '实时资讯',    '最新政策、薪资行情、公司动态等需要联网的信息',        'web_search'),
 (4, '数据查询',    '查询客户自己的记录或系统统计，如"我上次聊到哪"',      'sql');
