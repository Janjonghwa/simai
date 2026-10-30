-- S15P31A101-329: 병합 이벤트당 빠른 경로 작업 하나, 결과는 추가만 한다.
CREATE UNIQUE INDEX analysis_job_vlm_event_key ON analysis_job (event_id)
    WHERE job_type = 'vlm_rationale' AND event_id IS NOT NULL;

ALTER TABLE analysis_job ADD COLUMN deadline_at timestamptz;
COMMENT ON COLUMN analysis_job.deadline_at IS 'VLM 빠른 경로 이벤트 관측 시각 + 5초. 이 시각부터 신규 대여·재시도를 하지 않는다';

CREATE TABLE vlm_assessment (
    assessment_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id uuid NOT NULL REFERENCES risk_event (event_id),
    event_sequence integer NOT NULL,
    job_id text NOT NULL UNIQUE REFERENCES analysis_job (job_id),
    job_type text NOT NULL CHECK (job_type IN ('vlm_rationale', 'incident_summary')),
    model_version text NOT NULL,
    input_ref jsonb,
    rule_severity text NOT NULL,
    text text NOT NULL,
    clause_ids text[],
    proposed_severity text,
    latency_ms integer,
    decision text NOT NULL CHECK (decision IN ('NONE', 'PENDING', 'APPROVED', 'REJECTED')),
    decided_by bigint REFERENCES app_user (user_id),
    decided_at timestamptz,
    event_observed_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL,
    event_to_rationale_ms bigint NOT NULL
);
COMMENT ON TABLE vlm_assessment IS 'VLM 근거 정본(추가만). risk_event.rationale은 최신값 투영';
COMMENT ON COLUMN vlm_assessment.created_at IS 'EC2 저장 시각(워커 finished_at이 아님)';
COMMENT ON COLUMN vlm_assessment.event_to_rationale_ms IS 'G7: 이벤트 관측 시각부터 EC2 근거 저장 시각까지, UTC ms';
