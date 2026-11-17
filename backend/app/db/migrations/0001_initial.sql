-- SMART SPACE schema v1 (SDD section 5.1).
-- Additions vs. the SDD: files.kind may be 'other' (unsupported files), jobs.run_after (retry backoff).
-- PRAGMA journal_mode / foreign_keys are set per connection in app/db/database.py.

CREATE TABLE workspace (
    id            INTEGER PRIMARY KEY CHECK (id = 1),     -- single workspace
    root_path     TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'idle',           -- idle | scanning | indexing | paused | error
    created_at    TEXT NOT NULL,
    last_scan_at  TEXT
);

CREATE TABLE exclusions (
    id       INTEGER PRIMARY KEY,
    pattern  TEXT NOT NULL UNIQUE,                        -- glob, relative to root, e.g. "node_modules/**"
    builtin  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE files (
    id            INTEGER PRIMARY KEY,
    rel_path      TEXT NOT NULL UNIQUE,                   -- relative to workspace root, forward slashes
    name          TEXT NOT NULL,
    ext           TEXT NOT NULL,                          -- lowercase, no dot
    kind          TEXT NOT NULL,                          -- 'document' | 'image' | 'other'
    size_bytes    INTEGER NOT NULL,
    mtime_ns      INTEGER NOT NULL,
    content_hash  TEXT,                                   -- xxh3_128 hex
    language      TEXT,                                   -- 'de' | 'en' | NULL
    page_count    INTEGER,
    status        TEXT NOT NULL DEFAULT 'pending',        -- pending | processing | indexed | skipped | error | deleted
    skip_reason   TEXT,                                   -- too_large | unsupported | empty | encrypted | excluded | no_text
    error_message TEXT,
    summary_state TEXT NOT NULL DEFAULT 'none',           -- none | pending | done | failed
    indexed_at    TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX idx_files_status ON files(status);
CREATE INDEX idx_files_ext ON files(ext);
CREATE INDEX idx_files_mtime ON files(mtime_ns);

CREATE TABLE chunks (
    id              INTEGER PRIMARY KEY,
    file_id         INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    chunk_index     INTEGER NOT NULL,
    source          TEXT NOT NULL,                        -- 'text' | 'ocr' | 'summary'
    page_start      INTEGER,
    page_end        INTEGER,
    text            TEXT NOT NULL,
    char_count      INTEGER NOT NULL,
    embedding_model TEXT NOT NULL,                        -- models.id
    UNIQUE (file_id, chunk_index, source)
);
CREATE INDEX idx_chunks_file ON chunks(file_id);

CREATE VIRTUAL TABLE chunks_fts USING fts5(
    text, content='chunks', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TABLE summaries (
    file_id        INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    summary        TEXT NOT NULL,                         -- ~80-150 words
    doc_type       TEXT,                                  -- invoice | contract | cv | lecture | note | report | letter | form | photo | other
    language       TEXT,
    model_id       TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    generated_at   TEXT NOT NULL
);

CREATE TABLE tags (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);

CREATE TABLE file_tags (
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    tag_id  INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    source  TEXT NOT NULL DEFAULT 'ai',                   -- 'ai' | 'user'
    PRIMARY KEY (file_id, tag_id)
);

CREATE TABLE jobs (
    id          INTEGER PRIMARY KEY,
    file_id     INTEGER REFERENCES files(id) ON DELETE CASCADE,
    type        TEXT NOT NULL,                            -- extract | embed | summarize | delete_vectors
    state       TEXT NOT NULL DEFAULT 'queued',           -- queued | running | done | failed | cancelled
    priority    INTEGER NOT NULL DEFAULT 5,               -- 1 high ... 9 low (summaries = 8)
    attempts    INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    run_after   TEXT,                                     -- retry backoff (not before this UTC time)
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT
);
CREATE INDEX idx_jobs_queue ON jobs(state, priority, id);
CREATE INDEX idx_jobs_file ON jobs(file_id);

CREATE TABLE models (
    id            TEXT PRIMARY KEY,                       -- catalog id, e.g. 'llm-3b'
    role          TEXT NOT NULL,                          -- embedding | llm | ocr
    display_name  TEXT NOT NULL,
    file_name     TEXT,
    quant         TEXT,
    size_bytes    INTEGER,
    sha256        TEXT,
    dim           INTEGER,                                -- embeddings only
    status        TEXT NOT NULL DEFAULT 'not_installed',  -- not_installed | downloading | installed | failed
    is_active     INTEGER NOT NULL DEFAULT 0,             -- one active per role
    downloaded_at TEXT
);

CREATE TABLE hardware_profile (
    id                 INTEGER PRIMARY KEY CHECK (id = 1),
    os                 TEXT,
    cpu_name           TEXT,
    cpu_cores_physical INTEGER,
    cpu_cores_logical  INTEGER,
    cpu_avx2           INTEGER,
    ram_total_mb       INTEGER,
    ram_available_mb   INTEGER,
    gpus_json          TEXT,                              -- [{name, vendor, vram_mb, driver, backend}]
    disk_free_mb       INTEGER,
    tier               INTEGER,                           -- 0..4
    scanned_at         TEXT
);

CREATE TABLE chat_sessions (
    id         INTEGER PRIMARY KEY,
    title      TEXT NOT NULL DEFAULT 'New chat',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE chat_messages (
    id             INTEGER PRIMARY KEY,
    session_id     INTEGER NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role           TEXT NOT NULL,                         -- user | assistant
    content        TEXT NOT NULL,
    citations_json TEXT,                                  -- [{n, file_id, chunk_id, page, score, name}]
    model_id       TEXT,
    tokens_in      INTEGER,
    tokens_out     INTEGER,
    created_at     TEXT NOT NULL
);
CREATE INDEX idx_msgs_session ON chat_messages(session_id, id);

CREATE TABLE settings (
    key        TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE purge_log (
    id            INTEGER PRIMARY KEY,
    scope         TEXT NOT NULL,                          -- index | file | chats | models | everything
    target        TEXT,
    items_deleted INTEGER NOT NULL,
    bytes_freed   INTEGER,
    performed_at  TEXT NOT NULL
);

-- Built-in exclusions (SDD 5.3). Hidden files/folders are skipped in code.
INSERT INTO exclusions (pattern, builtin) VALUES
    ('.git/**', 1),
    ('node_modules/**', 1),
    ('__pycache__/**', 1),
    ('.venv/**', 1),
    ('$RECYCLE.BIN/**', 1),
    ('*.tmp', 1),
    ('~$*', 1),
    ('*.crdownload', 1),
    ('*.part', 1);
