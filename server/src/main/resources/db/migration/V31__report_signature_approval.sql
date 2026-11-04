-- S15P31A101-442: 기존 서명은 참석자, 관리자 최종 결재는 버전당 한 번이다.
ALTER TABLE report_signature
    ADD COLUMN signer_role text NOT NULL DEFAULT 'PARTICIPANT'
    CHECK (signer_role IN ('PARTICIPANT', 'APPROVER'));

CREATE UNIQUE INDEX report_signature_one_approver_idx
    ON report_signature(report_id, report_version) WHERE signer_role = 'APPROVER';

ALTER TABLE report
    ADD COLUMN approved_at timestamptz,
    ADD COLUMN approved_by bigint REFERENCES app_user(user_id),
    ADD CONSTRAINT report_approval_pair_check CHECK ((approved_at IS NULL) = (approved_by IS NULL));
