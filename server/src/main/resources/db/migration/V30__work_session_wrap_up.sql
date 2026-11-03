-- S15P31A101-439: 작업 세션과 append-only 마무리 보고서 버전.
ALTER TABLE work_unit
    ADD COLUMN status text NOT NULL DEFAULT 'PLANNED' CHECK (status IN ('PLANNED', 'IN_PROGRESS', 'DONE')),
    ADD COLUMN actual_started_at timestamptz,
    ADD COLUMN actual_ended_at timestamptz,
    ADD CONSTRAINT work_session_times CHECK (
        (status = 'PLANNED' AND actual_started_at IS NULL AND actual_ended_at IS NULL) OR
        (status = 'IN_PROGRESS' AND actual_started_at IS NOT NULL AND actual_ended_at IS NULL) OR
        (status = 'DONE' AND actual_started_at IS NOT NULL AND actual_ended_at IS NOT NULL
            AND actual_ended_at >= actual_started_at));
CREATE UNIQUE INDEX work_one_in_progress_per_site ON work_unit(site_id) WHERE status = 'IN_PROGRESS';

ALTER TABLE report
    ADD COLUMN IF NOT EXISTS wrap_up_summary text,
    ADD COLUMN wrap_up_status text CHECK (wrap_up_status IN ('GENERATING', 'SUCCEEDED', 'FAILED')),
    ADD COLUMN wrap_up_facts jsonb,
    ADD COLUMN wrap_up_fallback boolean,
    ADD COLUMN wrap_up_model text;
-- -440(V29)와 함께 쓰는 응답 필드. 독립 시험에서도 적용 가능하게 한다.
ALTER TABLE report ADD COLUMN IF NOT EXISTS locked_at timestamptz;
ALTER TABLE report ADD COLUMN IF NOT EXISTS content_sha256 text;
