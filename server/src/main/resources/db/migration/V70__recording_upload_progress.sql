-- S15P31A101-693: 등록 수만으로 알 수 없는 녹화별 조각 계획과 최신 진행 상태.
-- 로컬 origin/dev 최대 V69 확인. 공개 전 Lead가 번호를 재확인한다.
CREATE TABLE recording_upload_progress (
    gateway_id text NOT NULL REFERENCES device(device_id) ON DELETE CASCADE,
    recording_id varchar(128) NOT NULL,
    work_id bigint NOT NULL REFERENCES work_unit(work_id) ON DELETE CASCADE,
    expected_segments integer NOT NULL CHECK (expected_segments >= 0),
    failed_segments integer NOT NULL CHECK (failed_segments >= 0 AND failed_segments <= expected_segments),
    sealed boolean NOT NULL,
    received_at timestamptz NOT NULL,
    PRIMARY KEY (gateway_id, recording_id, work_id)
);
CREATE INDEX recording_upload_progress_work_idx ON recording_upload_progress(work_id);
CREATE INDEX video_segment_recording_work_idx ON video_segment(work_id, gateway_id, recording_id)
    WHERE origin = 'GATEWAY_RECORDING';
