-- simAI V65: 사람 표시 이름과 워치 기기 표시 이름 추가 전용 이력 (S15P31A101-658, 명세 5.2·11.4-5·12.4·13.2~13.3·부록 C.6)
-- 덮어쓰지 않고 행만 추가한다(AGENTS 규칙 6): 대상별 가장 큰 history_id 행이 현재 유효 상태다.
-- 우선순위(현장·날짜 Asia/Seoul 기준):
--   1) person_world_name_history (관리자가 현재 World ID 에 지정한 이름, 당일 한정, 병합 승계)
--   2) watch_display_name_history (배정된 워치 기기 표시 이름, 현장 단위로 지속)
--   3) person_unidentified_number_history ("미확인 N", 현장·날짜별 1부터 단조 증가, 병합 승계·분할 시 새 번호)

CREATE TABLE watch_display_name_history (
    history_id bigint GENERATED ALWAYS AS IDENTITY NOT NULL,
    site_id varchar(64) NOT NULL REFERENCES site (site_id),
    device_id text NOT NULL REFERENCES device (device_id),
    display_name text,
    action text NOT NULL CHECK (action IN ('SET', 'CLEAR')),
    changed_by_user_id bigint NOT NULL REFERENCES app_user (user_id),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (history_id),
    CONSTRAINT watch_display_name_history_value_check CHECK (
        (action = 'CLEAR' AND display_name IS NULL)
        OR (action = 'SET' AND display_name IS NOT NULL AND char_length(btrim(display_name)) BETWEEN 1 AND 20)
    )
);

CREATE INDEX watch_display_name_history_lookup_idx
    ON watch_display_name_history (site_id, device_id, history_id DESC);

COMMENT ON TABLE watch_display_name_history IS
    '워치 기기 표시 이름 변경 이력(추가 전용, 현장 단위 지속, S15P31A101-658). 최신 history_id 행이 유효하다';
COMMENT ON COLUMN watch_display_name_history.action IS
    'SET 표시 이름 지정·변경, CLEAR 표시 이름 비움(이력은 유지)';

CREATE TABLE person_world_name_history (
    history_id bigint GENERATED ALWAYS AS IDENTITY NOT NULL,
    site_id varchar(64) NOT NULL REFERENCES site (site_id),
    site_date date NOT NULL,
    world_id text NOT NULL CHECK (char_length(world_id) BETWEEN 1 AND 64),
    display_name text,
    action text NOT NULL CHECK (action IN ('SET', 'MERGE', 'CLEAR')),
    source_world_id text,
    changed_by_user_id bigint NOT NULL REFERENCES app_user (user_id),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (history_id),
    CONSTRAINT person_world_name_history_value_check CHECK (
        (action = 'CLEAR' AND display_name IS NULL)
        OR (action IN ('SET', 'MERGE') AND display_name IS NOT NULL AND char_length(btrim(display_name)) BETWEEN 1 AND 20)
    ),
    CONSTRAINT person_world_name_history_merge_source_check CHECK (
        (action = 'MERGE') = (source_world_id IS NOT NULL)
    )
);

CREATE INDEX person_world_name_history_lookup_idx
    ON person_world_name_history (site_id, site_date, world_id, history_id DESC);

COMMENT ON TABLE person_world_name_history IS
    '워치 없는 사람의 익명 World ID 에 관리자가 붙인 당일 표시 이름과 병합 승계 이력(추가 전용, S15P31A101-658)';
COMMENT ON COLUMN person_world_name_history.site_date IS
    '현장 기준 날짜(Asia/Seoul). 다음 날에는 이어지지 않는다';
COMMENT ON COLUMN person_world_name_history.source_world_id IS
    'MERGE 일 때 흡수된 옛 World ID';

CREATE TABLE person_unidentified_number_history (
    history_id bigint GENERATED ALWAYS AS IDENTITY NOT NULL,
    site_id varchar(64) NOT NULL REFERENCES site (site_id),
    site_date date NOT NULL,
    world_id text NOT NULL CHECK (char_length(world_id) BETWEEN 1 AND 64),
    seq_no integer NOT NULL CHECK (seq_no >= 1),
    reason text NOT NULL CHECK (reason IN ('INITIAL', 'MERGE', 'SPLIT')),
    source_world_id text,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (history_id),
    CONSTRAINT person_unidentified_number_source_check CHECK (
        (reason IN ('MERGE', 'SPLIT')) = (source_world_id IS NOT NULL)
    )
);

CREATE INDEX person_unidentified_number_lookup_idx
    ON person_unidentified_number_history (site_id, site_date, world_id, history_id DESC);

CREATE INDEX person_unidentified_number_seq_idx
    ON person_unidentified_number_history (site_id, site_date, seq_no DESC);

COMMENT ON TABLE person_unidentified_number_history IS
    '현장·날짜별 미확인 N 번호 배정·병합 승계·분할 새 번호 이력(추가 전용, S15P31A101-658)';

CREATE TABLE person_world_merge_log (
    merge_id bigint GENERATED ALWAYS AS IDENTITY NOT NULL,
    site_id varchar(64) NOT NULL REFERENCES site (site_id),
    site_date date NOT NULL,
    old_world_id text NOT NULL CHECK (char_length(old_world_id) BETWEEN 1 AND 64),
    survivor_world_id text NOT NULL CHECK (char_length(survivor_world_id) BETWEEN 1 AND 64),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (merge_id),
    CONSTRAINT person_world_merge_log_distinct_check CHECK (old_world_id <> survivor_world_id),
    CONSTRAINT person_world_merge_log_unique UNIQUE (site_id, site_date, old_world_id, survivor_world_id)
);

COMMENT ON TABLE person_world_merge_log IS
    'world_state.tracks[].aliases 병합 처리 멱등 기록(추가 전용, S15P31A101-658)';
