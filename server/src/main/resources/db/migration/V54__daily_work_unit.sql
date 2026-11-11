-- S15P31A101-610: 날짜 작업(하루 = 작업 하나, docs/design/daily-work-units.md).
-- daily: 날짜 작업 표시. 하루에 여러 개인 예전 작업(daily=false)은 그대로 둔다.
-- setup_completed_at: 세팅 완료(C17) 시각. 달력 진행 칩 SETUP_DONE 의 근거다(쓰는 쪽은 세팅 완료).
ALTER TABLE work_unit
    ADD COLUMN daily boolean NOT NULL DEFAULT false,
    ADD COLUMN setup_completed_at timestamptz;

-- 날짜 작업 이름은 고른 작업내용으로 서버가 만든다. 고르기 전에는 비어 있다(예전 작업은 계속 필수).
ALTER TABLE work_unit ALTER COLUMN name DROP NOT NULL;
ALTER TABLE work_unit ADD CONSTRAINT work_unit_name_required_check CHECK (daily OR name IS NOT NULL);

CREATE UNIQUE INDEX work_unit_one_daily_per_date ON work_unit (site_id, work_date) WHERE daily;
CREATE INDEX work_unit_site_date_idx ON work_unit (site_id, work_date);
