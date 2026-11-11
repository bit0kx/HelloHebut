CREATE TABLE IF NOT EXISTS {graph_runs} (
    conversation_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    request_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    base_revision BIGINT NOT NULL,
    workflow_version VARCHAR(80) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    trace_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    usage_json LONGTEXT NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (conversation_key, request_hash),
    CONSTRAINT {ck_graph_usage} CHECK (JSON_VALID(usage_json))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS {graph_checkpoints} (
    conversation_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    request_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    checkpoint_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    parent_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
    checkpoint_json LONGTEXT NOT NULL,
    metadata_json LONGTEXT NOT NULL,
    PRIMARY KEY (conversation_key, request_hash, checkpoint_id),
    CONSTRAINT {ck_graph_checkpoint} CHECK (JSON_VALID(checkpoint_json)),
    CONSTRAINT {ck_graph_metadata} CHECK (JSON_VALID(metadata_json))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS {graph_writes} (
    conversation_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    request_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    checkpoint_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    task_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    write_index INT NOT NULL,
    channel VARCHAR(128) NOT NULL,
    value_json LONGTEXT NOT NULL,
    PRIMARY KEY (conversation_key, request_hash, checkpoint_id, task_id, write_index),
    CONSTRAINT {ck_graph_write} CHECK (JSON_VALID(value_json))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
