-- S15P31A101-611: TBM 서명은 작업 전에 받고, 잠금은 제출 때 한다(docs/design/tbm-sign-submit.md).
-- submitted_at·submitted_by: 작업 종료 뒤 제출. 이때 locked_at·content_sha256(양식 + 서명 목록)을 기록한다.
ALTER TABLE report
    ADD COLUMN submitted_at timestamptz,
    ADD COLUMN submitted_by bigint REFERENCES app_user(user_id),
    ADD CONSTRAINT report_submit_pair_check CHECK ((submitted_at IS NULL) = (submitted_by IS NULL));

-- 서명 화면에 고지 체크가 없어졌다. 받은 값(없으면 false)을 그대로 남긴다.
ALTER TABLE report_signature DROP CONSTRAINT report_signature_legal_notice_ack_check;
