-- Engram v2 schema. SQLite + sqlite-vec.
-- One DB, one file, plain-text-readable with `sqlite3 ~/.engram/engram.db`.
--
-- Design notes:
-- * Every table has explicit indexes on lookup columns. We do not rely on the
--   storage engine's whims; explicit > implicit.
-- * Risk tier (green/orange/red) is a first-class column on Resource and File,
--   not a derived view. Blast-radius queries must be cheap.
-- * Every row that Engram auto-generates downstream artifacts from has a
--   `content_hash` so signing is straightforward.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;

-- ---------------------------------------------------------------------------
-- Projects: a directory containing a project marker (.git, Chart.yaml, etc.)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS project (
    path           TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    project_type   TEXT NOT NULL DEFAULT '',  -- terraform, helm, k8s-manifests, app, etc.
    description    TEXT NOT NULL DEFAULT '',
    last_indexed   TEXT NOT NULL              -- ISO8601 UTC
);

CREATE INDEX IF NOT EXISTS idx_project_name ON project(name);

-- ---------------------------------------------------------------------------
-- Files: every file we touched, hashed for skip-on-rerun.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS file (
    path           TEXT PRIMARY KEY,
    project_path   TEXT REFERENCES project(path) ON DELETE CASCADE,
    name           TEXT NOT NULL,
    extension      TEXT NOT NULL DEFAULT '',
    size_bytes     INTEGER NOT NULL DEFAULT 0,
    content_hash   TEXT NOT NULL DEFAULT '',
    modified_at    TEXT NOT NULL,
    -- Computed risk tier for the FILE itself (a prod-tagged k8s manifest is red).
    -- Resources within may have different tiers; this is a fast-path hint.
    risk_tier      TEXT NOT NULL DEFAULT 'green' CHECK (risk_tier IN ('green','orange','red'))
);

CREATE INDEX IF NOT EXISTS idx_file_project   ON file(project_path);
CREATE INDEX IF NOT EXISTS idx_file_name      ON file(name);
CREATE INDEX IF NOT EXISTS idx_file_ext       ON file(extension);
CREATE INDEX IF NOT EXISTS idx_file_modified  ON file(modified_at);
CREATE INDEX IF NOT EXISTS idx_file_risk_tier ON file(risk_tier);

-- ---------------------------------------------------------------------------
-- Resources: the structural units of infrastructure.
-- A k8s Deployment, a Terraform resource, a Helm chart, a docker-compose
-- service, a Jenkinsfile stage. Each Resource is anchored to a File but
-- represents one logical unit of infra.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS resource (
    uid            TEXT PRIMARY KEY,            -- sha256-derived stable id
    file_path      TEXT NOT NULL REFERENCES file(path) ON DELETE CASCADE,
    kind           TEXT NOT NULL,                -- e.g. 'k8s:Deployment', 'tf:aws_rds_instance', 'docker:service', 'helm:chart'
    name           TEXT NOT NULL,                -- the user-facing name
    namespace      TEXT NOT NULL DEFAULT '',     -- k8s namespace, tf module, helm release, etc.
    environment    TEXT NOT NULL DEFAULT '',     -- 'production' | 'staging' | 'dev' | '' (inferred)
    risk_tier      TEXT NOT NULL DEFAULT 'green' CHECK (risk_tier IN ('green','orange','red')),
    properties     TEXT NOT NULL DEFAULT '{}',   -- JSON of arbitrary props
    context_snippet TEXT NOT NULL DEFAULT ''     -- a short string for provenance
);

CREATE INDEX IF NOT EXISTS idx_resource_file        ON resource(file_path);
CREATE INDEX IF NOT EXISTS idx_resource_kind        ON resource(kind);
CREATE INDEX IF NOT EXISTS idx_resource_name        ON resource(name);
CREATE INDEX IF NOT EXISTS idx_resource_environment ON resource(environment);
CREATE INDEX IF NOT EXISTS idx_resource_risk_tier   ON resource(risk_tier);

-- ---------------------------------------------------------------------------
-- Entities: code-level entities (functions, classes, env_var defs/refs,
-- imports, packages). Kept separate from Resources because they have
-- different lifecycles and properties.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS entity (
    uid            TEXT PRIMARY KEY,
    file_path      TEXT NOT NULL REFERENCES file(path) ON DELETE CASCADE,
    name           TEXT NOT NULL,
    entity_type    TEXT NOT NULL,                -- function, class, env_var, env_ref, package, ...
    value          TEXT NOT NULL DEFAULT '',
    context_snippet TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_entity_file ON entity(file_path);
CREATE INDEX IF NOT EXISTS idx_entity_name ON entity(name);
CREATE INDEX IF NOT EXISTS idx_entity_type ON entity(entity_type);

-- ---------------------------------------------------------------------------
-- Services: a logical service, possibly spanning many repos and resources.
-- Inferred from naming conventions (e.g. all resources matching `payments-*`
-- belong to service `payments`) and from explicit user declarations.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS service (
    name           TEXT PRIMARY KEY,
    description    TEXT NOT NULL DEFAULT '',
    primary_repo   TEXT NOT NULL DEFAULT '',
    owner_team     TEXT NOT NULL DEFAULT ''
);

-- Resource-to-Service mapping. A resource belongs to at most one service.
CREATE TABLE IF NOT EXISTS resource_service (
    resource_uid   TEXT PRIMARY KEY REFERENCES resource(uid) ON DELETE CASCADE,
    service_name   TEXT NOT NULL REFERENCES service(name)   ON DELETE CASCADE,
    confidence     REAL NOT NULL DEFAULT 1.0       -- 0..1, low for inferred mappings
);

CREATE INDEX IF NOT EXISTS idx_resource_service_service ON resource_service(service_name);

-- ---------------------------------------------------------------------------
-- Edges: typed relationships between Resources / Files / Services / Entities.
-- A single edge table covers everything; rel_type discriminates.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS edge (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    src_kind       TEXT NOT NULL CHECK (src_kind IN ('file','resource','entity','service','project')),
    src_id         TEXT NOT NULL,
    dst_kind       TEXT NOT NULL CHECK (dst_kind IN ('file','resource','entity','service','project','technology')),
    dst_id         TEXT NOT NULL,
    rel_type       TEXT NOT NULL,                -- DEPENDS_ON, CONFIGURES, EXPOSES_PORT, MOUNTS, USES_ENV, etc.
    properties     TEXT NOT NULL DEFAULT '{}',   -- JSON for edge attrs
    UNIQUE (src_kind, src_id, dst_kind, dst_id, rel_type)
);

CREATE INDEX IF NOT EXISTS idx_edge_src_lookup ON edge(src_kind, src_id, rel_type);
CREATE INDEX IF NOT EXISTS idx_edge_dst_lookup ON edge(dst_kind, dst_id, rel_type);
CREATE INDEX IF NOT EXISTS idx_edge_rel_type   ON edge(rel_type);

-- ---------------------------------------------------------------------------
-- Technologies: a catalog of named techs (postgres, redis, nginx, helm, etc.)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS technology (
    name           TEXT PRIMARY KEY,
    category       TEXT NOT NULL DEFAULT 'unknown'  -- database, runtime, tool, framework, ...
);

-- ---------------------------------------------------------------------------
-- Memory: user/agent-authored observations, decisions, conventions, errors.
-- Same shape as v1 but scoped tighter to DevOps decisions in practice.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS memory (
    id             TEXT PRIMARY KEY,
    content        TEXT NOT NULL,
    memory_type    TEXT NOT NULL CHECK (memory_type IN ('observation','decision','convention','error','runbook')),
    source         TEXT NOT NULL DEFAULT '',
    project_path   TEXT NOT NULL DEFAULT '',
    file_path      TEXT NOT NULL DEFAULT '',
    service_name   TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    accessed_at    TEXT NOT NULL,
    access_count   INTEGER NOT NULL DEFAULT 0,
    importance     REAL NOT NULL DEFAULT 1.0
);

CREATE INDEX IF NOT EXISTS idx_memory_type    ON memory(memory_type);
CREATE INDEX IF NOT EXISTS idx_memory_project ON memory(project_path);
CREATE INDEX IF NOT EXISTS idx_memory_service ON memory(service_name);
CREATE INDEX IF NOT EXISTS idx_memory_created ON memory(created_at);

-- ---------------------------------------------------------------------------
-- Codified-context emissions: tracks every AGENTS.md / CLAUDE.md / INFRA.md
-- Engram has written, with a content hash so verify_context() can check
-- whether the file on disk matches what Engram emitted.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS emission (
    path           TEXT PRIMARY KEY,             -- file Engram wrote
    kind           TEXT NOT NULL,                -- agents-md, claude-md, infra-md, etc.
    content_hash   TEXT NOT NULL,                -- sha256 of the auto-generated section
    signature      TEXT NOT NULL,                -- HMAC over the section, keyed off the DB
    emitted_at     TEXT NOT NULL,
    scope          TEXT NOT NULL DEFAULT '{}'    -- JSON: which projects/services contributed
);

CREATE INDEX IF NOT EXISTS idx_emission_kind ON emission(kind);

-- ---------------------------------------------------------------------------
-- Infrastructure annotations: every label/tag Engram has written onto live
-- infrastructure. Required for safe rollback (`engram unlabel`).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS annotation (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    target_kind    TEXT NOT NULL,                -- 'k8s', 'terraform', 'docker', 'helm'
    target_id      TEXT NOT NULL,                -- cluster/namespace/resource path
    label_key      TEXT NOT NULL,                -- e.g. 'engram.io/blast-radius'
    label_value    TEXT NOT NULL,
    applied_at     TEXT NOT NULL,
    UNIQUE (target_kind, target_id, label_key)
);

CREATE INDEX IF NOT EXISTS idx_annotation_target ON annotation(target_kind, target_id);

-- ---------------------------------------------------------------------------
-- Vector tables (sqlite-vec). Created lazily by db.py because they require
-- the extension to be loaded; this file documents the intended shape.
--
--   CREATE VIRTUAL TABLE vec_file_chunks USING vec0(
--       chunk_id TEXT PRIMARY KEY,
--       embedding FLOAT[384]
--   );
--
--   CREATE VIRTUAL TABLE vec_memory USING vec0(
--       memory_id TEXT PRIMARY KEY,
--       embedding FLOAT[384]
--   );
--
-- Companion non-vector table for chunk text (vec0 cannot hold text):
CREATE TABLE IF NOT EXISTS file_chunk (
    chunk_id       TEXT PRIMARY KEY,
    file_path      TEXT NOT NULL REFERENCES file(path) ON DELETE CASCADE,
    chunk_index    INTEGER NOT NULL,
    content        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_file_chunk_file ON file_chunk(file_path);

-- FTS5 full-text index over file chunks. Lexical search is the primary signal
-- for DevOps content (env var names, image tags, hostnames) — vector search
-- is supplementary, not foundational.
CREATE VIRTUAL TABLE IF NOT EXISTS fts_file_chunk USING fts5(
    chunk_id UNINDEXED,
    file_path UNINDEXED,
    content,
    tokenize = 'unicode61'
);

-- ---------------------------------------------------------------------------
-- Schema version. Bump when migrating.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('schema_version', '2');
INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('created_at', datetime('now'));
