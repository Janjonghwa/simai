-- simAI V36: 일일 안전 일지 (S15P31A101-33, FR-122, 기획서 8.3·12.6, docs/design/daily-report.md)
-- V27 의 report 표는 report_kind DAILY 를 이미 허용하지만 작업(work_unit) 없이 쓸 칸이 없었다.
-- 일일 일지는 작업이 아니라 현장·날짜(Asia/Seoul) 하나에 묶이고, 날짜마다 재생성하면 report_version 이 오른다.
-- V35 는 S15P31A101-338(워치 알림, MR !364)이 잡아 두었다.

ALTER TABLE report
    ADD COLUMN site_id text REFERENCES site (site_id),
    ADD COLUMN report_date date,
    -- 생성에 넘긴 그날 LIVE 이벤트 사실(사람 ID·근거 문장 없이)과 집계. 조회 화면의 근거 표다
    ADD COLUMN events jsonb NOT NULL DEFAULT '[]',
    ADD COLUMN event_summary jsonb NOT NULL DEFAULT '{}';

-- TBM 은 작업에, DAILY 는 현장·날짜에만 묶인다
ALTER TABLE report ADD CONSTRAINT report_kind_target_ck CHECK (
    (report_kind = 'TBM' AND work_id IS NOT NULL AND report_date IS NULL)
    OR (report_kind = 'DAILY' AND work_id IS NULL AND site_id IS NOT NULL AND report_date IS NOT NULL));

CREATE UNIQUE INDEX report_daily_version_uq ON report (site_id, report_date, report_version) WHERE report_kind = 'DAILY';
-- 같은 날짜를 동시에 두 번 생성하지 않는다(TBM 의 report_one_generating_per_work 와 같은 뜻)
CREATE UNIQUE INDEX report_one_generating_per_day ON report (site_id, report_date)
    WHERE report_kind = 'DAILY' AND status = 'GENERATING';

COMMENT ON COLUMN report.site_id IS '일일 일지(DAILY)의 현장. TBM 은 work_unit 을 거쳐 현장을 찾는다';
COMMENT ON COLUMN report.report_date IS '일일 일지(DAILY)의 날짜(Asia/Seoul 하루)';
COMMENT ON COLUMN report.events IS '일일 일지 생성에 넘긴 그날 LIVE risk_event 사실(rule_id·severity·zone_id·raised_at_ms·resolved·needs_check, 최대 100건)';
COMMENT ON COLUMN report.event_summary IS '일일 일지의 그날 LIVE 이벤트 집계(전체·해소·확인 필요 건수, 등급별·규칙별 건수)';
