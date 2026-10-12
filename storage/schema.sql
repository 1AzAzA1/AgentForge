-- Схема локальной БД Agent Forge (SQLite).
-- Все таблицы заведены сразу, чтобы не ломать миграциями
-- уже созданные профили пользователей.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Этап 1: локальные профили и воркспейсы
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE,
    password_hash BLOB    NOT NULL,   -- Argon2id(password, verify_salt)
    verify_salt   BLOB    NOT NULL,   -- соль для проверки пароля
    kdf_salt      BLOB    NOT NULL,   -- соль для вывода мастер-ключа шифрования
    created_at    TEXT    NOT NULL,
    settings_json TEXT    NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS workspaces (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name          TEXT    NOT NULL,
    description   TEXT    NOT NULL DEFAULT '',
    settings_json TEXT    NOT NULL DEFAULT '{}',
    archived      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ws_user ON workspaces(user_id);

-- ---------------------------------------------------------------------------
-- Этап 2: API-ключи (зашифрованы) и агенты
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS api_keys (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    label         TEXT    NOT NULL,
    provider      TEXT    NOT NULL,   -- ключ пресета: openai/anthropic/gemini/...
    base_url      TEXT    NOT NULL DEFAULT '',
    secret_blob   BLOB,               -- nonce||ciphertext||tag; NULL для Ollama
    meta_json     TEXT    NOT NULL DEFAULT '{}',
    created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_keys_user ON api_keys(user_id);

CREATE TABLE IF NOT EXISTS agents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id  INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name          TEXT    NOT NULL,
    role          TEXT    NOT NULL DEFAULT '',
    system_prompt TEXT    NOT NULL DEFAULT '',
    api_key_id    INTEGER REFERENCES api_keys(id) ON DELETE SET NULL,
    provider      TEXT    NOT NULL DEFAULT '',
    model         TEXT    NOT NULL DEFAULT '',
    params_json   TEXT    NOT NULL DEFAULT '{}',  -- temperature, max_tokens, tools
    enabled       INTEGER NOT NULL DEFAULT 1,
    status        TEXT    NOT NULL DEFAULT 'idle',
    is_supervisor INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agents_ws ON agents(workspace_id);

-- ---------------------------------------------------------------------------
-- Этап 3: задачи и подзадачи
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id  INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    title         TEXT    NOT NULL DEFAULT '',
    description   TEXT    NOT NULL DEFAULT '',
    status        TEXT    NOT NULL DEFAULT 'draft',
    result_format TEXT    NOT NULL DEFAULT 'auto',  -- auto|markdown|docx|pdf|zip
    token_limit   INTEGER,                          -- NULL = без лимита
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_ws ON tasks(workspace_id);

CREATE TABLE IF NOT EXISTS subtasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    agent_id      INTEGER REFERENCES agents(id) ON DELETE SET NULL,
    title         TEXT    NOT NULL,
    description   TEXT    NOT NULL DEFAULT '',
    status        TEXT    NOT NULL DEFAULT 'idle',
    order_index   INTEGER NOT NULL DEFAULT 0,
    depends_on    TEXT    NOT NULL DEFAULT '',   -- CSV id подзадач-предшественников
    result        TEXT    NOT NULL DEFAULT '',
    rework_count  INTEGER NOT NULL DEFAULT 0,
    tokens_in     INTEGER NOT NULL DEFAULT 0,
    tokens_out    INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL    NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_subtasks_task ON subtasks(task_id);

-- ---------------------------------------------------------------------------
-- Этап 4-5: приватная история агента, отчёты, сводки супервайзера
-- ---------------------------------------------------------------------------

-- Полностью изолированный контекст агента: каждая строка видна только
-- своему agent_id, кросс-агентных выборок в коде нет.
CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id      INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    subtask_id    INTEGER REFERENCES subtasks(id) ON DELETE CASCADE,
    role          TEXT    NOT NULL,      -- system|user|assistant|tool
    content       TEXT    NOT NULL DEFAULT '',
    tool_name     TEXT    NOT NULL DEFAULT '',
    tool_call_id  TEXT    NOT NULL DEFAULT '',
    tokens        INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_agent ON messages(agent_id, subtask_id);

CREATE TABLE IF NOT EXISTS reports (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id   INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    task_id        INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    subtask_id     INTEGER REFERENCES subtasks(id) ON DELETE CASCADE,
    agent_id       INTEGER REFERENCES agents(id) ON DELETE SET NULL,
    content        TEXT    NOT NULL,
    confidence     REAL,                       -- самооценка уверенности 0..1
    tokens_in      INTEGER NOT NULL DEFAULT 0,
    tokens_out     INTEGER NOT NULL DEFAULT 0,
    cost_usd       REAL    NOT NULL DEFAULT 0,
    reviewed       INTEGER NOT NULL DEFAULT 0,
    review_verdict TEXT    NOT NULL DEFAULT '', -- ok|rework|conflict
    review_notes   TEXT    NOT NULL DEFAULT '',
    created_at     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_ws ON reports(workspace_id, created_at);

CREATE TABLE IF NOT EXISTS summaries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id  INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    task_id       INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    content       TEXT    NOT NULL,          -- анонимизированный пересказ
    trigger       TEXT    NOT NULL DEFAULT 'timer',  -- timer|event|manual
    delivered_to  TEXT    NOT NULL DEFAULT '',       -- CSV agent_id
    created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_summaries_ws ON summaries(workspace_id, created_at);

CREATE TABLE IF NOT EXISTS incidents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id  INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    task_id       INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    subtask_id    INTEGER REFERENCES subtasks(id) ON DELETE SET NULL,
    report_id     INTEGER REFERENCES reports(id) ON DELETE SET NULL,
    kind          TEXT    NOT NULL,   -- conflict|factual_error|contradiction|off_scope
    severity      TEXT    NOT NULL DEFAULT 'medium',
    description   TEXT    NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'open',  -- open|auto_resolved|escalated|resolved
    resolution    TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL,
    resolved_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_incidents_ws ON incidents(workspace_id, created_at);

-- ---------------------------------------------------------------------------
-- Этап 7: точки human-in-the-loop
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS approvals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id  INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    task_id       INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    reason        TEXT    NOT NULL,     -- conflict|low_confidence|milestone
    payload_json  TEXT    NOT NULL DEFAULT '{}',
    decision      TEXT    NOT NULL DEFAULT '',  -- '' пока ждём пользователя
    comment       TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL,
    decided_at    TEXT
);

-- ---------------------------------------------------------------------------
-- Этап 9: бюджеты и лимиты
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS budgets (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    scope            TEXT    NOT NULL,   -- 'workspace' | 'agent' | 'task'
    scope_id         INTEGER NOT NULL,
    token_limit      INTEGER,
    cost_limit_usd   REAL,
    tokens_used      INTEGER NOT NULL DEFAULT 0,
    cost_used_usd    REAL    NOT NULL DEFAULT 0,
    alert_threshold  REAL    NOT NULL DEFAULT 0.8,
    alerted          INTEGER NOT NULL DEFAULT 0,
    updated_at       TEXT    NOT NULL,
    UNIQUE(scope, scope_id)
);

-- Журнал вызовов моделей: основа для графиков расхода на дашборде.
CREATE TABLE IF NOT EXISTS usage_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id  INTEGER REFERENCES workspaces(id) ON DELETE CASCADE,
    task_id       INTEGER,
    subtask_id    INTEGER,
    agent_id      INTEGER,
    provider      TEXT    NOT NULL DEFAULT '',
    model         TEXT    NOT NULL DEFAULT '',
    tokens_in     INTEGER NOT NULL DEFAULT 0,
    tokens_out    INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL    NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_ws ON usage_log(workspace_id, created_at);
