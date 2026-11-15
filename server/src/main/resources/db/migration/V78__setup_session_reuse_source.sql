-- 이전 맵 재사용 = 고른 맵을 만들 때의 세팅 묶음(보정·카메라 높이·구역·Scene)을 그대로 쓴다(S15P31A101-714,
-- docs/design/S15P31A101-714-reuse-chosen-map.md). 재사용은 더 이상 워커 작업(배경·Stage A 재계산)을 만들지 않는다.
-- reuse_source_session_id: 고른 맵을 만든 NEW 세팅 세션(세팅 완료한 맵). reuse_scene_version: 그때 고른 그 세션의 맵(뒤에 다시 굽기가
-- 맵을 바꿔도 이 세션이 쓴 맵을 남긴다). reuse_view_check: 시작 때 통과한 1단계 시점 확인(coverage_guide reuse_view_check 와 같은 카메라별
-- 결과 + received_at_ms). 재사용 세션은 워커 작업이 없어 job_id 가 NULL 이다(새 맵 세팅은 그대로 작업이 있어야 한다).

ALTER TABLE setup_session ALTER COLUMN job_id DROP NOT NULL;

ALTER TABLE setup_session
    ADD COLUMN reuse_source_session_id uuid REFERENCES setup_session(setup_session_id),
    ADD COLUMN reuse_scene_version text REFERENCES scene_version(scene_version),
    ADD COLUMN reuse_view_check jsonb CHECK (reuse_view_check IS NULL OR jsonb_typeof(reuse_view_check) = 'object');

ALTER TABLE setup_session ADD CONSTRAINT setup_session_reuse_source_check CHECK (
    (reuse_source_session_id IS NULL AND reuse_scene_version IS NULL AND reuse_view_check IS NULL AND job_id IS NOT NULL)
    OR (reuse_source_session_id IS NOT NULL AND reuse_scene_version IS NOT NULL AND reuse_view_check IS NOT NULL
        AND job_id IS NULL AND mode = 'REUSE' AND requested_mode = 'REUSE'));

CREATE INDEX setup_session_reuse_source ON setup_session(reuse_source_session_id) WHERE reuse_source_session_id IS NOT NULL;

COMMENT ON COLUMN setup_session.reuse_source_session_id IS '이전 맵 재사용(-714): 고른 맵을 만든 세팅 세션. 새 맵 세팅·옛 재사용은 NULL';
COMMENT ON COLUMN setup_session.reuse_scene_version IS '이전 맵 재사용(-714): 이 세션이 쓴 맵(고른 세션의 그때 scene_version)';
COMMENT ON COLUMN setup_session.reuse_view_check IS '이전 맵 재사용(-714): 시작 때 통과한 1단계 시점 확인(카메라별 결과·받은 시각)';
