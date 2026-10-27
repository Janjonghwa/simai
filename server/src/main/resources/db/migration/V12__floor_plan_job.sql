-- S15P31A101-240: BEV 평면도 작업 등록·결과 저장.
-- bev_frame_set: 관리자가 등록한 카메라별 사람 없는 프레임 묶음(bev_floorplan 입력 frames). 명세에는 EC2 가 프레임을 고른다고만 있고
-- 프레임 주소를 EC2 에 주는 계약이 없어 관리 명령으로 받는다(사용자 결정 2026-10-27). 추가만 하고 가장 늦은 묶음을 쓴다.
-- floor_plan: 등록 단위는 (보정 버전, 프레임 묶음) 하나에 한 번이다. 결과(job_callback result)는 그대로 둔다.
-- V9 는 -69, V10·V11 은 -263·-261, V13·V14 는 -123·-72 가 쓴다.
CREATE TABLE bev_frame_set (
    frame_set_id bigint GENERATED ALWAYS AS IDENTITY NOT NULL,
    site_id text NOT NULL,
    frame_source text NOT NULL CHECK (frame_source IN ('LIVE', 'FILE', 'SIM', 'DATASET')),
    frames jsonb NOT NULL,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (frame_set_id)
);
ALTER TABLE bev_frame_set ADD CONSTRAINT bev_frame_set_site_id_fkey FOREIGN KEY (site_id) REFERENCES site (site_id);

ALTER TABLE floor_plan ADD COLUMN frame_set_id bigint;
ALTER TABLE floor_plan ADD COLUMN result jsonb;
ALTER TABLE floor_plan ADD COLUMN error text;
ALTER TABLE floor_plan ADD COLUMN finished_at timestamptz;
ALTER TABLE floor_plan ADD CONSTRAINT floor_plan_frame_set_id_fkey FOREIGN KEY (frame_set_id) REFERENCES bev_frame_set (frame_set_id);
-- 같은 보정 버전·프레임 묶음은 한 번만 등록한다(대조기와 관리 명령이 동시에 돌아도)
CREATE UNIQUE INDEX floor_plan_request_key ON floor_plan (calibration_version, frame_set_id);
CREATE INDEX floor_plan_job_id_idx ON floor_plan (job_id);

COMMENT ON TABLE bev_frame_set IS 'BEV 평면도 입력 프레임 묶음(관리 명령으로 등록, 추가만)';
COMMENT ON COLUMN bev_frame_set.frames IS 'bev_floorplan input.frames 와 같은 모양(camera_id·frame_uri·촬영 시각)';
COMMENT ON COLUMN floor_plan.result IS 'bev_floorplan 결과(job_callback result) 원문';
COMMENT ON COLUMN floor_plan.error IS 'FAILED 이유(작업 등록 거부·작업 실패 코드)';
