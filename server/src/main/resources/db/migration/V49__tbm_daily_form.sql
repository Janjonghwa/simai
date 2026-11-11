-- S15P31A101-609: 날짜 작업 TBM 보고서의 양식 칸과 리더 계정(docs/design/tbm-daily-form.md).
-- 리더는 이름 글자를 저장하지 않고 계정을 참조한다. 소속·직책은 계정에 둔다.
ALTER TABLE app_user
    ADD COLUMN org text CHECK (org IS NULL OR char_length(org) BETWEEN 1 AND 40),
    ADD COLUMN title text CHECK (title IS NULL OR char_length(title) BETWEEN 1 AND 40);

-- form: 사람이 고치는 양식 칸(정본은 서버). form_revision: 고칠 때마다 1씩 올려 동시 수정을 409 로 막는다.
-- 양식 수정은 새 report_version 을 만들지 않는다(서명이 버전에 묶여 있다).
ALTER TABLE report
    ADD COLUMN form jsonb,
    ADD COLUMN form_revision integer NOT NULL DEFAULT 0 CHECK (form_revision >= 0),
    ADD COLUMN leader_user_id bigint REFERENCES app_user(user_id);

-- PENDING: 날짜 작업과 함께 만든 빈 보고서. AI 초안을 시작하면 GENERATING → SUCCEEDED·FAILED 로 간다.
ALTER TABLE report DROP CONSTRAINT report_status_check;
ALTER TABLE report ADD CONSTRAINT report_status_check
    CHECK (status IN ('PENDING', 'GENERATING', 'SUCCEEDED', 'FAILED'));
ALTER TABLE report ADD CONSTRAINT report_pending_tbm_check CHECK (status <> 'PENDING' OR report_kind = 'TBM');
