CREATE TABLE IF NOT EXISTS {chat_conversations} (
    conversation_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    revision BIGINT NOT NULL DEFAULT 0,
    state_json LONGTEXT NOT NULL,
    messages_json LONGTEXT NOT NULL,
    summary LONGTEXT NOT NULL,
    summary_epoch BIGINT NOT NULL DEFAULT 0,
    legacy_imported BOOLEAN NOT NULL DEFAULT FALSE,
    lease_token VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
    lease_until DATETIME(6) NULL,
    CONSTRAINT {ck_chat_state} CHECK (JSON_VALID(state_json)),
    CONSTRAINT {ck_chat_messages} CHECK (JSON_VALID(messages_json))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS {chat_requests} (
    conversation_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    request_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    message_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    message_text LONGTEXT NULL,
    status VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    base_revision BIGINT NOT NULL,
    result_json LONGTEXT NULL,
    state_json LONGTEXT NULL,
    memory_revision BIGINT NULL,
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (conversation_key, request_hash),
    CONSTRAINT {ck_request_status} CHECK (status IN ('pending', 'computed', 'committed')),
    CONSTRAINT {ck_request_result} CHECK (result_json IS NULL OR JSON_VALID(result_json)),
    CONSTRAINT {ck_request_state} CHECK (state_json IS NULL OR JSON_VALID(state_json))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
