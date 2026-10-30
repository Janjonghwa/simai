-- S15P31A101-369: 사고 구간 VLM 사후 요약 요청·결과 기록(명세 5.2·5.6·12.5·12.6, docs/design/incident-summary.md)
-- 결과는 추가만 하고(AGENTS.md 6) 이벤트 등급·상향 의견을 바꾸지 않는다(AGENTS.md 4).
CREATE TABLE incident_summary (
    summary_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id uuid NOT NULL REFERENCES risk_event (event_id),
    event_sequence integer NOT NULL,
    job_id text NOT NULL UNIQUE REFERENCES analysis_job (job_id),
    status text NOT NULL CHECK (status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'TIMED_OUT')),
    requested_by bigint REFERENCES app_user (user_id),
    requested_at timestamptz NOT NULL,
    finished_at timestamptz,
    window_start_ms bigint,
    window_end_ms bigint,
    summary text,
    evidence_timestamps_ms bigint[] NOT NULL DEFAULT '{}'::bigint[],
    frames_used integer,
    model_version text,
    latency_ms integer,
    error_code text
);

COMMENT ON TABLE incident_summary IS '사고 구간 VLM 사후 요약(incident_summary) 요청·결과 정본(추가만). 이벤트 summaries 배열로 최신순 투영';
COMMENT ON COLUMN incident_summary.status IS 'QUEUED(대기) → RUNNING(워커 대여 중) → SUCCEEDED / FAILED / TIMED_OUT';

CREATE INDEX incident_summary_event_id_requested_at_idx
    ON incident_summary (event_id, requested_at DESC, summary_id DESC);

CREATE UNIQUE INDEX incident_summary_active_event_key
    ON incident_summary (event_id)
    WHERE status IN ('QUEUED', 'RUNNING');
