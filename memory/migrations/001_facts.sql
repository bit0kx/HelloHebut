CREATE TABLE IF NOT EXISTS memory_owners (
    owner_id text PRIMARY KEY,
    revision bigint NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS memory_records (
    memory_id uuid PRIMARY KEY,
    owner_id text NOT NULL REFERENCES memory_owners(owner_id),
    fact_key text NOT NULL CHECK (fact_key IN ('major_preference','interests','budget_preference','location_preference')),
    value text NOT NULL CHECK (length(value) BETWEEN 1 AND 120),
    version bigint NOT NULL,
    status text NOT NULL CHECK (status IN ('pending','active','superseded','expired')),
    verification_status text NOT NULL CHECK (verification_status IN ('source_checked','user_confirmed')),
    source_kind text NOT NULL CHECK (source_kind IN ('chat','user_input')),
    source_message_id text NOT NULL,
    source_run_id text NOT NULL,
    source_conv_id text NOT NULL,
    source_excerpt text NOT NULL,
    base_revision bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    confirmed_at timestamptz,
    valid_until timestamptz NOT NULL,
    UNIQUE(owner_id, fact_key, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS memory_one_active ON memory_records(owner_id, fact_key) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS memory_owner_status ON memory_records(owner_id, status);
-- Receipts intentionally contain no preference value or original user text.
CREATE TABLE IF NOT EXISTS memory_operations (
    owner_id text NOT NULL REFERENCES memory_owners(owner_id),
    operation_id text NOT NULL,
    fingerprint text NOT NULL,
    PRIMARY KEY(owner_id, operation_id)
);
CREATE TABLE IF NOT EXISTS memory_captures (
    owner_id text NOT NULL REFERENCES memory_owners(owner_id),
    capture_id text NOT NULL,
    PRIMARY KEY(owner_id, capture_id)
);
