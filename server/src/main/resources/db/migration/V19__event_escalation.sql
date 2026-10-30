-- S15P31A101-276: 관리자 VLM 상향 의견 승인·거절 기록(event_escalation)과 risk_event.escalations 투영(명세 5.4·12.3·12.6, docs/design/db-tables.md E절)
-- V18(S15P31A101-329)이 아직 머지되지 않은 브랜치에서도 단독 기동·시험이 되도록 vlm_assessment 를 IF NOT EXISTS 로 함께 둔다.
CREATE TABLE IF NOT EXISTS vlm_assessment (
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
    event_observed_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL,
    event_to_rationale_ms bigint NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS vlm_assessment_event_id_event_sequence_idx
    ON vlm_assessment (event_id, event_sequence);

CREATE INDEX IF NOT EXISTS vlm_assessment_event_id_created_at_idx
    ON vlm_assessment (event_id, created_at DESC);

CREATE TABLE IF NOT EXISTS event_escalation (
    escalation_id uuid PRIMARY KEY,
    event_id uuid NOT NULL REFERENCES risk_event (event_id),
    assessment_id bigint NOT NULL REFERENCES vlm_assessment (assessment_id),
    basis text NOT NULL CHECK (basis IN ('VLM_PROPOSAL')),
    target_severity text NOT NULL CHECK (target_severity IN ('CRITICAL', 'HIGH', 'CAUTION')),
    actor_user_id bigint NOT NULL REFERENCES app_user (user_id),
    requested_at timestamptz NOT NULL,
    note text,
    result text CHECK (result IN ('ACCEPTED', 'DUPLICATE', 'REJECTED')),
    reject_reason text CHECK (reject_reason IN ('NOT_ACTIVE', 'NOT_UPWARD', 'JOURNAL_UNAVAILABLE')),
    event_sequence integer,
    decided_at timestamptz
);

CREATE INDEX IF NOT EXISTS event_escalation_event_id_requested_at_idx
    ON event_escalation (event_id, requested_at);

ALTER TABLE risk_event
    ADD COLUMN IF NOT EXISTS escalations jsonb NOT NULL DEFAULT '[]'::jsonb;

UPDATE risk_event_update u
SET escalation_id = NULL
WHERE escalation_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM event_escalation e WHERE e.escalation_id = u.escalation_id);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'risk_event_update_escalation_id_fkey'
    ) THEN
        ALTER TABLE risk_event_update
            ADD CONSTRAINT risk_event_update_escalation_id_fkey
            FOREIGN KEY (escalation_id) REFERENCES event_escalation (escalation_id);
    END IF;
END $$;
