-- S15P31A101-696: 작업 1건(하루) 안의 실행(run) 여러 번(사용자 결정 2026-11-14 방식 B, 리허설 뒤 본 시연).
-- TBM·세팅은 하루 한 번, 「작업 시작 ~ 작업 종료」만 실행 1·2… 로 반복한다. 실행마다 시작·종료 시각이 따로 남고
-- 녹화 업로드 구간·다시 보기·배포 보류가 실행 단위로 이것을 읽는다. 설계 docs/design/S15P31A101-696-work-runs.md.
-- 추가 전용: 행은 시작 때 하나 넣고, 종료 때 ended_at·ended_by 를 한 번만 채운다(서버 코드가 ended_at IS NULL 인 행만 고친다).
-- work_unit 은 하루 요약으로 남는다: actual_started_at = 첫 실행 시작, actual_ended_at = 마지막 실행 종료(실행 중이면 NULL),
-- status 는 실행 중이면 IN_PROGRESS, 끝난 실행만 있으면 DONE(V30 CHECK 그대로).
-- 번호는 Lead 지정(V72 는 -690). 공개 전 Lead 가 번호를 다시 확인한다.
CREATE TABLE work_run (
    work_id bigint NOT NULL REFERENCES work_unit (work_id) ON DELETE CASCADE,
    run_no integer NOT NULL CHECK (run_no >= 1),
    site_id text NOT NULL REFERENCES site (site_id),
    started_at timestamptz NOT NULL,
    ended_at timestamptz,
    started_by bigint,
    ended_by bigint,
    -- 시작 전 확인 스냅샷: 참석자 서명 수·서명이 묶인 보고서 판·안전모·조끼 요구(시작 때 값, 이후 바뀌어도 고치지 않는다)
    preflight jsonb,
    PRIMARY KEY (work_id, run_no),
    CHECK (ended_at IS NULL OR ended_at >= started_at),
    CHECK (ended_at IS NOT NULL OR ended_by IS NULL)
);

COMMENT ON TABLE work_run IS '작업 실행(작업 시작 ~ 작업 종료 한 번). 하루 작업(work_unit) 하나에 여러 번, 추가 전용';

-- 한 현장에 진행 중 실행은 하나(work_one_in_progress_per_site 와 같은 뜻을 실행 표에서도 지킨다)
CREATE UNIQUE INDEX work_run_one_open_per_site ON work_run (site_id) WHERE ended_at IS NULL;
-- 녹화 구간 조회(범위와 겹치는 끝난 실행)
CREATE INDEX work_run_span_idx ON work_run (started_at, ended_at);

-- 지금까지의 작업은 실행 1 하나다(시작한 작업만). 시작·종료한 사람은 기록이 없어 비운다
INSERT INTO work_run (work_id, run_no, site_id, started_at, ended_at)
SELECT work_id, 1, site_id, actual_started_at, actual_ended_at FROM work_unit WHERE actual_started_at IS NOT NULL;

-- 실행 구간을 읽는 곳(작업 응답 runs·녹화 구간·조각 등록·업로드 상태·배포 보류)은 이 뷰를 쓴다. 실행 기록이 없는 시작한 작업
-- (실행 표 밖에서 work_unit 시각을 쓴 경우: 시험 데이터·옛 복구 경로)은 work_unit 시각을 실행 1로 본다
CREATE VIEW work_run_window AS
SELECT r.work_id, r.run_no, r.site_id, r.started_at, r.ended_at FROM work_run r
UNION ALL
SELECT w.work_id, 1, w.site_id, w.actual_started_at, w.actual_ended_at FROM work_unit w
WHERE w.actual_started_at IS NOT NULL AND NOT EXISTS (SELECT 1 FROM work_run r WHERE r.work_id = w.work_id);

-- 날짜 작업 종료 미팅(review.ending)을 서버가 마지막으로 채운 글. 다음 실행 종료 때 사람이 고치지 않았으면(같은 글) 모든 실행으로 다시 쓴다
ALTER TABLE report ADD COLUMN wrap_up_ending text;
