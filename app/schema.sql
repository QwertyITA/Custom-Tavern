-- Personal Tavern schema (§11). SQLite, WAL mode, single write queue.

CREATE TABLE IF NOT EXISTS characters (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    version     INTEGER NOT NULL DEFAULT 1,
    data        TEXT NOT NULL,          -- json: persona, state_schema, pfp_set, backgrounds, ...
    persona_id  TEXT NOT NULL DEFAULT '',  -- the persona new chats with them use
    favourite   INTEGER NOT NULL DEFAULT 0,  -- starred; sorts to the top (§11)
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS chats (
    id           TEXT PRIMARY KEY,
    character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    title        TEXT NOT NULL DEFAULT '',
    version      INTEGER NOT NULL DEFAULT 1,
    settings     TEXT NOT NULL DEFAULT '{}',   -- json: colours, toggle overrides
    persona_id   TEXT NOT NULL DEFAULT '',     -- who {{user}} is here; '' = default
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chats_character ON chats(character_id);

-- Message stores RAW text only; dialogue/action is render-time markup (§8).
CREATE TABLE IF NOT EXISTS messages (
    id             TEXT PRIMARY KEY,
    chat_id        TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    turn           INTEGER NOT NULL,
    role           TEXT NOT NULL,               -- user | assistant | system
    active_variant TEXT,                        -- -> message_variants.id
    edited         INTEGER NOT NULL DEFAULT 0,
    stage          TEXT NOT NULL DEFAULT 'verbatim',  -- verbatim | summarized | dropped (§7.2)
    hidden         INTEGER NOT NULL DEFAULT 0,        -- on screen, out of the prompt
    speaker_id     TEXT NOT NULL DEFAULT '',          -- which character said it
    created_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, turn);

CREATE TABLE IF NOT EXISTS message_variants (
    id         TEXT PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    idx        INTEGER NOT NULL,
    text       TEXT NOT NULL,
    provider   TEXT NOT NULL DEFAULT '',
    model      TEXT NOT NULL DEFAULT '',
    -- The other language (roadmap 23). `text` is always what was actually
    -- written; this is the same thing said in the language the other side of
    -- the conversation uses. Empty when translation is off.
    translation TEXT NOT NULL DEFAULT '',
    -- What the model thought before it answered (§5.6), when it thinks out
    -- loud: a `<think>` block in the stream, or Ollama's separate reasoning
    -- channel. Never displayed inline — it is not what the character said —
    -- and kept per variant, because a re-roll thought different things.
    thinking   TEXT NOT NULL DEFAULT '',
    -- What "Cut excess paragraphs" (§ app/reply_length.py) removed, kept so
    -- one message can be restored to what the model actually wrote. Empty
    -- means this variant was never cut.
    full_text  TEXT NOT NULL DEFAULT '',
    -- What post_process (§ app/reply_polish.py) rewrote away, kept so one
    -- message can be restored to the model's own first draft. Independent of
    -- full_text above — a reply can be edited by post_process, then still cut
    -- by the length backstop, and each has its own undo. Empty means
    -- post_process never changed this variant.
    draft_text TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_variants_message ON message_variants(message_id, idx);

-- Current value per slice. `source_turn` powers stale-write rejection (§5.5).
CREATE TABLE IF NOT EXISTS state_slices (
    chat_id     TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    slice_name  TEXT NOT NULL,
    value       TEXT NOT NULL,          -- json
    source_turn INTEGER NOT NULL,
    source_pass TEXT NOT NULL DEFAULT '',
    provisional INTEGER NOT NULL DEFAULT 0,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (chat_id, slice_name)
);

-- Append-only write log. Carries prev_value so a discarded swipe can be rolled
-- back exactly (§9), and doubles as the state audit trail.
CREATE TABLE IF NOT EXISTS state_writes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     TEXT NOT NULL,
    slice_name  TEXT NOT NULL,
    value       TEXT NOT NULL,
    prev_value  TEXT,
    source_turn INTEGER NOT NULL,
    source_pass TEXT NOT NULL DEFAULT '',
    variant_id  TEXT,
    rolled_back INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_state_writes_scope
    ON state_writes(chat_id, source_turn, variant_id);

CREATE TABLE IF NOT EXISTS memories (
    id           TEXT PRIMARY KEY,
    character_id TEXT NOT NULL,
    chat_id      TEXT,
    text         TEXT NOT NULL,
    keys         TEXT NOT NULL DEFAULT '[]',   -- json array
    created_turn INTEGER NOT NULL DEFAULT 0,
    source       TEXT NOT NULL DEFAULT 'memory_pass',
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_character ON memories(character_id);

CREATE TABLE IF NOT EXISTS chat_summaries (
    chat_id        TEXT PRIMARY KEY REFERENCES chats(id) ON DELETE CASCADE,
    text           TEXT NOT NULL DEFAULT '',
    covered_turn   INTEGER NOT NULL DEFAULT 0,
    updated_at     REAL NOT NULL
);

-- Powers the HUD and the cost dashboard (§12, §14).
CREATE TABLE IF NOT EXISTS pass_runs (
    id          TEXT PRIMARY KEY,
    chat_id     TEXT NOT NULL,
    turn        INTEGER NOT NULL,
    pass_id     TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'custom',
    tier        TEXT NOT NULL,
    model       TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL,            -- pending|running|done|failed|stale|skipped
    tokens_in   INTEGER NOT NULL DEFAULT 0,
    tokens_out  INTEGER NOT NULL DEFAULT 0,
    attempts    INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    variant_id  TEXT,
    started_at  REAL,
    finished_at REAL,
    -- The itemised prompt as JSON (§15): what each section actually held for
    -- this run. Kept only for recent turns -- see prune_prompt_records.
    prompt      TEXT
);
CREATE INDEX IF NOT EXISTS idx_pass_runs_chat ON pass_runs(chat_id, turn);

CREATE TABLE IF NOT EXISTS pass_defs (
    id      TEXT PRIMARY KEY,
    kind    TEXT NOT NULL DEFAULT 'custom',
    enabled INTEGER NOT NULL DEFAULT 1,
    data    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS toggles (
    id   TEXT PRIMARY KEY,
    data TEXT NOT NULL
);

-- Toggle on/off per scope: ('global',''), ('per_character',<id>), ('per_chat',<id>).
CREATE TABLE IF NOT EXISTS toggle_state (
    scope     TEXT NOT NULL,
    scope_id  TEXT NOT NULL DEFAULT '',
    toggle_id TEXT NOT NULL,
    enabled   INTEGER NOT NULL,
    PRIMARY KEY (scope, scope_id, toggle_id)
);

CREATE TABLE IF NOT EXISTS lorebooks (
    id      TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    entries TEXT NOT NULL DEFAULT '[]'   -- json array of entries (§7.4)
);

-- Who is in a chat (roadmap 8). Every chat has at least one member -- its own
-- character -- so a solo chat is simply a group of one and there is no second
-- shape to reason about.
CREATE TABLE IF NOT EXISTS chat_members (
    chat_id       TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    character_id  TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    muted         INTEGER NOT NULL DEFAULT 0,   -- present, but never speaks
    talkativeness REAL NOT NULL DEFAULT 1.0,    -- weight when nobody is named
    joined_at     REAL NOT NULL,
    PRIMARY KEY (chat_id, character_id)
);

-- Files attached to a message (§19). Text is stored inline because the text
-- *is* the file; images are stored on disk and `stored_as` names them.
CREATE TABLE IF NOT EXISTS attachments (
    id         TEXT PRIMARY KEY,
    -- NULL while staged: a file is uploaded before the message it belongs to
    -- exists, and the turn claims it once the message has been created.
    message_id TEXT REFERENCES messages(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,              -- image | text
    name       TEXT NOT NULL,              -- as shown, never as a path
    stored_as  TEXT NOT NULL DEFAULT '',   -- filename on disk, images only
    mime       TEXT NOT NULL DEFAULT '',
    size       INTEGER NOT NULL DEFAULT 0,
    text       TEXT NOT NULL DEFAULT '',   -- extracted, text files only
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attachments_message ON attachments(message_id);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Who "you" are. A person can keep several — one for each way they like to
-- play — and {{user}} resolves to whichever is active. Deleting a persona
-- leaves chats alone: they carry the id, and an id with nothing behind it
-- falls back to the default the same way an unset one does.
CREATE TABLE IF NOT EXISTS personas (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    avatar      TEXT NOT NULL DEFAULT '',
    is_default  INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
