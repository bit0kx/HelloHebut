CREATE TABLE IF NOT EXISTS {memory_owners} (
    owner_id char(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    revision bigint NOT NULL DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
CREATE TABLE IF NOT EXISTS {memory_records} (
    memory_id char(36) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    owner_id char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    fact_key varchar(40) NOT NULL,
    value varchar(120) NOT NULL,
    version bigint NOT NULL,
    status varchar(16) NOT NULL,
    verification_status varchar(20) NOT NULL,
    source_kind varchar(16) NOT NULL,
    source_message_id varchar(255) NOT NULL,
    source_run_id varchar(255) NOT NULL,
    source_conv_id varchar(255) NOT NULL,
    source_excerpt text NOT NULL,
    base_revision bigint NOT NULL,
    created_at datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    confirmed_at datetime(6),
    valid_until datetime(6) NOT NULL,
    active_key varchar(40) GENERATED ALWAYS AS (CASE WHEN status='active' THEN fact_key ELSE NULL END) STORED,
    UNIQUE KEY memory_version (owner_id, fact_key, version),
    UNIQUE KEY memory_one_active (owner_id, active_key),
    KEY memory_owner_status (owner_id, status),
    CONSTRAINT {fk_memory_records} FOREIGN KEY (owner_id) REFERENCES {memory_owners}(owner_id),
    CONSTRAINT {ck_memory_records_key} CHECK (fact_key IN ('major_preference','interests','budget_preference','location_preference')),
    CONSTRAINT {ck_memory_records_value} CHECK (char_length(value) BETWEEN 1 AND 120),
    CONSTRAINT {ck_memory_records_status} CHECK (status IN ('pending','active','superseded','expired')),
    CONSTRAINT {ck_memory_records_verification} CHECK (verification_status IN ('source_checked','user_confirmed')),
    CONSTRAINT {ck_memory_records_source} CHECK (source_kind IN ('chat','user_input'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
CREATE TABLE IF NOT EXISTS {memory_operations} (
    owner_id char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    operation_id varchar(100) NOT NULL,
    fingerprint char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    PRIMARY KEY(owner_id, operation_id),
    CONSTRAINT {fk_memory_operations} FOREIGN KEY (owner_id) REFERENCES {memory_owners}(owner_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
CREATE TABLE IF NOT EXISTS {memory_captures} (
    owner_id char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    capture_id char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    PRIMARY KEY(owner_id, capture_id),
    CONSTRAINT {fk_memory_captures} FOREIGN KEY (owner_id) REFERENCES {memory_owners}(owner_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
