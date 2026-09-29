-- JARVIS Datenbankschema
-- PostgreSQL 16 + pgvector (>= 0.7) + pgcrypto
-- Konvention: IDs sind Text mit Präfix (usr_, dev_, mem_, act_, …), Zeitstempel immer timestamptz (UTC).

BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS jarvis;
SET search_path = jarvis, public;

-- ---------------------------------------------------------------------------
-- Aufzählungstypen
-- ---------------------------------------------------------------------------
CREATE TYPE risk_class      AS ENUM ('R0', 'R1', 'R2', 'R3', 'R4');
CREATE TYPE trust_level     AS ENUM ('system', 'trusted_user', 'household', 'guest', 'external_untrusted');
CREATE TYPE user_role       AS ENUM ('admin', 'adult', 'child', 'guest', 'service');
CREATE TYPE memory_kind     AS ENUM ('episodic', 'semantic', 'procedural', 'preference');
CREATE TYPE sensitivity     AS ENUM ('public', 'personal', 'sensitive');
CREATE TYPE decision_effect AS ENUM ('allow', 'confirm', 'deny');
CREATE TYPE action_status   AS ENUM ('pending_confirmation', 'approved', 'rejected', 'denied', 'executing',
                                     'succeeded', 'failed', 'cancelled', 'timed_out', 'compensated');
CREATE TYPE task_type       AS ENUM ('todo', 'reminder', 'timer', 'job', 'plan');
CREATE TYPE task_status     AS ENUM ('pending', 'scheduled', 'running', 'waiting_confirmation', 'done', 'failed', 'cancelled');
CREATE TYPE confirmation_status AS ENUM ('pending', 'approved', 'rejected', 'expired');

-- ---------------------------------------------------------------------------
-- Identität
-- ---------------------------------------------------------------------------
CREATE TABLE users (
    id              text PRIMARY KEY,
    display_name    text NOT NULL,
    role            user_role NOT NULL DEFAULT 'adult',
    locale          text NOT NULL DEFAULT 'de-DE',
    oidc_subject    text UNIQUE,
    persona_id      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    deleted_at      timestamptz
);

-- Sprecher-Embeddings (ECAPA-TDNN: 192 Dimensionen), mehrere Enrollments pro Person
CREATE TABLE voice_profiles (
    id              bigserial PRIMARY KEY,
    user_id         text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    model           text NOT NULL,
    embedding       vector(192) NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX voice_profiles_user_idx ON voice_profiles (user_id);

CREATE TABLE areas (
    id              text PRIMARY KEY,               -- z. B. wohnzimmer
    name            text NOT NULL,
    aliases         text[] NOT NULL DEFAULT '{}',
    floor           text
);

CREATE TABLE devices (
    id              text PRIMARY KEY,               -- dev_… / sat_…
    kind            text NOT NULL CHECK (kind IN ('satellite', 'app', 'desktop', 'web', 'service')),
    name            text NOT NULL,
    user_id         text REFERENCES users(id) ON DELETE SET NULL,
    area_id         text REFERENCES areas(id) ON DELETE SET NULL,
    cert_fingerprint text UNIQUE,
    last_seen_at    timestamptz,
    revoked_at      timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Smart-Home-Spiegel (Quelle: Home Assistant Registry)
-- ---------------------------------------------------------------------------
CREATE TABLE home_entities (
    entity_id       text PRIMARY KEY,               -- light.kueche
    domain          text GENERATED ALWAYS AS (split_part(entity_id, '.', 1)) STORED,
    area_id         text REFERENCES areas(id) ON DELETE SET NULL,
    friendly_name   text NOT NULL,
    aliases         text[] NOT NULL DEFAULT '{}',
    capabilities    jsonb NOT NULL DEFAULT '{}'::jsonb,   -- unterstützte Farbmodi, Min/Max usw.
    critical        boolean NOT NULL DEFAULT false,       -- z. B. Kühlschrank-Steckdose -> R2
    exposed         boolean NOT NULL DEFAULT true,        -- für JARVIS sichtbar?
    last_state      jsonb,
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX home_entities_area_idx ON home_entities (area_id);
CREATE INDEX home_entities_domain_idx ON home_entities (domain);
CREATE INDEX home_entities_aliases_idx ON home_entities USING gin (aliases);

-- ---------------------------------------------------------------------------
-- Konversationen
-- ---------------------------------------------------------------------------
CREATE TABLE conversations (
    id              text PRIMARY KEY,
    user_id         text REFERENCES users(id) ON DELETE CASCADE,
    channel         text NOT NULL,
    device_id       text REFERENCES devices(id) ON DELETE SET NULL,
    started_at      timestamptz NOT NULL DEFAULT now(),
    ended_at        timestamptz,
    summary         text
);
CREATE INDEX conversations_user_idx ON conversations (user_id, started_at DESC);

CREATE TABLE messages (
    id              bigserial PRIMARY KEY,
    conversation_id text NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            text NOT NULL CHECK (role IN ('user', 'assistant', 'tool', 'system')),
    content         jsonb NOT NULL,                 -- anbieterneutrales Transkript-Element
    provider        text,
    model           text,
    correlation_id  text,
    tainted         boolean NOT NULL DEFAULT false,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX messages_conversation_idx ON messages (conversation_id, id);

-- ---------------------------------------------------------------------------
-- Gedächtnis
-- ---------------------------------------------------------------------------
CREATE TABLE memory_items (
    id              text PRIMARY KEY,
    user_id         text REFERENCES users(id) ON DELETE CASCADE,  -- NULL = Haushaltswissen
    kind            memory_kind NOT NULL,
    content         text NOT NULL CHECK (length(content) BETWEEN 1 AND 4000),
    subject         text,
    predicate       text,
    object          text,
    importance      real NOT NULL CHECK (importance BETWEEN 0 AND 1),
    confidence      real NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    sensitivity     sensitivity NOT NULL DEFAULT 'personal',
    source_type     text NOT NULL,
    source_ref      text,
    tags            text[] NOT NULL DEFAULT '{}',
    embedding       vector(1024),
    embedding_model text,
    valid_from      timestamptz NOT NULL DEFAULT now(),
    valid_until     timestamptz,
    expires_at      timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    last_accessed_at timestamptz,
    access_count    integer NOT NULL DEFAULT 0
);
CREATE INDEX memory_items_embedding_idx ON memory_items USING hnsw (embedding vector_cosine_ops);
CREATE INDEX memory_items_user_kind_idx ON memory_items (user_id, kind) WHERE valid_until IS NULL;
CREATE INDEX memory_items_tags_idx ON memory_items USING gin (tags);
-- Pro Person (bzw. Haushalt) höchstens ein gültiger Fakt je Subjekt/Prädikat
CREATE UNIQUE INDEX memory_items_active_triple_uq
    ON memory_items (user_id, subject, predicate) NULLS NOT DISTINCT
    WHERE valid_until IS NULL AND subject IS NOT NULL AND predicate IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Aufgaben, Automationen
-- ---------------------------------------------------------------------------
CREATE TABLE tasks (
    id              text PRIMARY KEY,
    type            task_type NOT NULL,
    title           text NOT NULL,
    details         jsonb NOT NULL DEFAULT '{}'::jsonb,
    status          task_status NOT NULL DEFAULT 'pending',
    priority        smallint NOT NULL DEFAULT 2 CHECK (priority BETWEEN 0 AND 4),
    owner_user_id   text REFERENCES users(id) ON DELETE CASCADE,
    assignee_user_id text REFERENCES users(id) ON DELETE SET NULL,
    list_name       text,                           -- z. B. einkauf
    due_at          timestamptz,
    trigger         jsonb,                          -- Zeit/Ort/Ereignis für Erinnerungen
    area_id         text REFERENCES areas(id),      -- Timer-Ansage im Raum
    parent_id       text REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on      text[] NOT NULL DEFAULT '{}',
    budget          jsonb,                          -- Jobs: max_tokens, max_seconds
    attempts        smallint NOT NULL DEFAULT 0,
    max_attempts    smallint NOT NULL DEFAULT 3,
    last_error      jsonb,
    result          jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    completed_at    timestamptz
);
CREATE INDEX tasks_due_idx ON tasks (due_at) WHERE status IN ('pending', 'scheduled');
CREATE INDEX tasks_owner_idx ON tasks (owner_user_id, status);

CREATE TABLE automations (
    id              text PRIMARY KEY,
    name            text NOT NULL,
    definition      jsonb NOT NULL,                 -- validiert gegen automation.schema.json
    enabled         boolean NOT NULL DEFAULT false,
    origin          text NOT NULL CHECK (origin IN ('user', 'jarvis_suggested', 'imported')),
    owner_user_id   text REFERENCES users(id) ON DELETE CASCADE,
    approval_status text NOT NULL DEFAULT 'pending'
                    CHECK (approval_status IN ('not_required', 'pending', 'approved', 'rejected')),
    version         integer NOT NULL DEFAULT 1,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE automation_runs (
    id              bigserial PRIMARY KEY,
    automation_id   text NOT NULL REFERENCES automations(id) ON DELETE CASCADE,
    automation_version integer NOT NULL,
    status          text NOT NULL CHECK (status IN ('running', 'succeeded', 'failed', 'skipped', 'aborted')),
    trigger         jsonb NOT NULL,
    trace           jsonb NOT NULL DEFAULT '[]'::jsonb,  -- Bedingungen/Aktionen mit Ergebnis
    dry_run         boolean NOT NULL DEFAULT false,
    correlation_id  text,
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz
);
CREATE INDEX automation_runs_automation_idx ON automation_runs (automation_id, started_at DESC);

-- ---------------------------------------------------------------------------
-- Aktionen & Bestätigungen
-- ---------------------------------------------------------------------------
CREATE TABLE actions (
    id              text PRIMARY KEY,
    capability      text NOT NULL,
    arguments       jsonb NOT NULL,
    actor           text NOT NULL,
    trust           trust_level NOT NULL,
    via             text NOT NULL,
    risk_class      risk_class NOT NULL,
    decision_effect decision_effect NOT NULL,
    decision_rule   text,
    decision_reason text,
    status          action_status NOT NULL,
    result          jsonb,
    error           jsonb,
    verification    jsonb,
    undo            jsonb,
    tainted         boolean NOT NULL DEFAULT false,
    idempotency_key text UNIQUE,
    correlation_id  text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    started_at      timestamptz,
    finished_at     timestamptz
);
CREATE INDEX actions_correlation_idx ON actions (correlation_id);
CREATE INDEX actions_capability_time_idx ON actions (capability, created_at DESC);

CREATE TABLE confirmations (
    id              text PRIMARY KEY,
    action_id       text NOT NULL REFERENCES actions(id) ON DELETE CASCADE,
    method          text NOT NULL CHECK (method IN ('voice', 'app', 'app_biometric', 'pin')),
    status          confirmation_status NOT NULL DEFAULT 'pending',
    requested_for   text REFERENCES users(id),
    session_id      text,
    prompt          text NOT NULL,
    expires_at      timestamptz NOT NULL,
    resolved_at     timestamptz,
    resolved_by     text,
    resolved_via    text,                           -- device_id / Kanal
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX confirmations_pending_idx ON confirmations (session_id) WHERE status = 'pending';

-- ---------------------------------------------------------------------------
-- Event-Store (monatlich partitioniert, Aufbewahrung per Partition-Drop)
-- ---------------------------------------------------------------------------
CREATE TABLE events (
    id              text NOT NULL,
    type            text NOT NULL,
    source          text NOT NULL,
    subject         text,
    time            timestamptz NOT NULL,
    actor           text,
    trust           trust_level NOT NULL,
    priority        text NOT NULL DEFAULT 'normal',
    correlation_id  text,
    causation_id    text,
    data            jsonb NOT NULL,
    PRIMARY KEY (id, time)
) PARTITION BY RANGE (time);
CREATE TABLE events_2026_09 PARTITION OF events FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');
CREATE TABLE events_2026_10 PARTITION OF events FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');
CREATE TABLE events_default PARTITION OF events DEFAULT;
CREATE INDEX events_type_time_idx ON events (type, time DESC);
CREATE INDEX events_subject_time_idx ON events (subject, time DESC);
CREATE INDEX events_correlation_idx ON events (correlation_id);

-- ---------------------------------------------------------------------------
-- Plugins, Webhooks, Benachrichtigungen, Proaktiv-Feedback, LLM-Nutzung
-- ---------------------------------------------------------------------------
CREATE TABLE plugins (
    id              text PRIMARY KEY,               -- org.example.weather
    version         text NOT NULL,
    manifest        jsonb NOT NULL,
    enabled         boolean NOT NULL DEFAULT false,
    approved_permissions jsonb,
    approved_by     text REFERENCES users(id),
    installed_at    timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE webhooks (
    id              text PRIMARY KEY,               -- whk_…
    name            text NOT NULL,
    secret_ref      text NOT NULL,                  -- vault:kv/jarvis/webhooks/whk_…#secret
    event_type      text NOT NULL,                  -- erzeugter Event-Typ
    trust           trust_level NOT NULL DEFAULT 'external_untrusted',
    enabled         boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE notifications (
    id              text PRIMARY KEY,
    user_id         text REFERENCES users(id) ON DELETE CASCADE,
    channel         text NOT NULL CHECK (channel IN ('voice', 'push', 'dashboard', 'text')),
    title           text,
    body            text NOT NULL,
    priority        text NOT NULL DEFAULT 'normal',
    suggestion_kind text,
    status          text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'delivered', 'read', 'dismissed', 'failed')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    delivered_at    timestamptz,
    read_at         timestamptz
);
CREATE INDEX notifications_user_idx ON notifications (user_id, created_at DESC);

CREATE TABLE suggestion_feedback (
    id              bigserial PRIMARY KEY,
    user_id         text REFERENCES users(id) ON DELETE CASCADE,
    suggestion_kind text NOT NULL,
    accepted        boolean NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX suggestion_feedback_idx ON suggestion_feedback (user_id, suggestion_kind, created_at DESC);

CREATE TABLE llm_usage (
    id              bigserial PRIMARY KEY,
    ts              timestamptz NOT NULL DEFAULT now(),
    provider        text NOT NULL,
    model           text NOT NULL,
    route           text NOT NULL,
    input_tokens    integer NOT NULL DEFAULT 0,
    output_tokens   integer NOT NULL DEFAULT 0,
    cache_read_tokens  integer NOT NULL DEFAULT 0,
    cache_write_tokens integer NOT NULL DEFAULT 0,
    latency_ms      integer,
    stop_reason     text,
    correlation_id  text
);
CREATE INDEX llm_usage_ts_idx ON llm_usage (ts DESC);

-- ---------------------------------------------------------------------------
-- Audit-Log: append-only, hash-verkettet
-- ---------------------------------------------------------------------------
CREATE TABLE audit_log (
    seq             bigserial PRIMARY KEY,
    ts              timestamptz NOT NULL DEFAULT clock_timestamp(),
    actor           text NOT NULL,
    event           text NOT NULL,                  -- z. B. policy.decision, action.executed, plugin.approved
    target          text,
    effect          text,
    details         jsonb NOT NULL DEFAULT '{}'::jsonb,
    correlation_id  text,
    prev_hash       bytea,
    hash            bytea NOT NULL
);

CREATE FUNCTION audit_log_chain() RETURNS trigger
LANGUAGE plpgsql
SET search_path = jarvis, public
AS $$
DECLARE
    last_hash bytea;
BEGIN
    -- Einfügungen serialisieren, damit die Kette eindeutig bleibt
    PERFORM pg_advisory_xact_lock(hashtext('jarvis.audit_log'));
    SELECT hash INTO last_hash FROM jarvis.audit_log ORDER BY seq DESC LIMIT 1;
    NEW.prev_hash := last_hash;
    NEW.hash := digest(
        coalesce(encode(last_hash, 'hex'), '') || '|' ||
        extract(epoch FROM NEW.ts)::text || '|' || NEW.actor || '|' || NEW.event || '|' ||
        coalesce(NEW.target, '') || '|' || coalesce(NEW.effect, '') || '|' ||
        NEW.details::text || '|' || coalesce(NEW.correlation_id, ''),
        'sha256');
    RETURN NEW;
END;
$$;

CREATE TRIGGER audit_log_chain_trg
    BEFORE INSERT ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_chain();

-- Prüffunktion: liefert die erste Sequenznummer, an der die Kette bricht (NULL = intakt)
CREATE FUNCTION audit_log_verify() RETURNS bigint
LANGUAGE plpgsql
SET search_path = jarvis, public
AS $$
DECLARE
    r record;
    expected_prev bytea := NULL;
    recomputed bytea;
BEGIN
    FOR r IN SELECT * FROM jarvis.audit_log ORDER BY seq LOOP
        recomputed := digest(
            coalesce(encode(expected_prev, 'hex'), '') || '|' ||
            extract(epoch FROM r.ts)::text || '|' || r.actor || '|' || r.event || '|' ||
            coalesce(r.target, '') || '|' || coalesce(r.effect, '') || '|' ||
            r.details::text || '|' || coalesce(r.correlation_id, ''),
            'sha256');
        IF r.prev_hash IS DISTINCT FROM expected_prev OR r.hash <> recomputed THEN
            RETURN r.seq;
        END IF;
        expected_prev := r.hash;
    END LOOP;
    RETURN NULL;
END;
$$;

-- ---------------------------------------------------------------------------
-- Rollen: Anwendung darf Audit-Log nur anhängen
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'jarvis_app') THEN
        CREATE ROLE jarvis_app NOLOGIN;
    END IF;
END;
$$;

GRANT USAGE ON SCHEMA jarvis TO jarvis_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA jarvis TO jarvis_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA jarvis TO jarvis_app;
REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM jarvis_app;

COMMIT;
