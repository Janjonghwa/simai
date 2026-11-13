-- S15P31A101-673: V2 시드 카메라 ID(classroom-1 의 cam1~cam4)를 게이트웨이·Tier 0·녹화(S15P31A101-668)와 같은 cam_A~cam_D 로 통일한다.
-- 이미 적용된 V1·V2 는 Flyway 체크섬 유지를 위해 고치지 않고 V69 에서 바꾼다.
-- 조건:
-- 1) site_id = 'classroom-1' 인 시드 카메라 cam1~cam4 행만 대상이다(운영 DB 의 lobby / cam_A~cam_D 는 건드리지 않는다).
-- 2) 새 카메라 ID(cam_A~cam_D)가 이미 있으면 바꾸지 않는다.
-- 3) 그 카메라를 참조하는 기록(FK 표 calibration_camera·coverage_guide 및 camera_id 열이 있는 표)이 없을 때만 바꾼다.

UPDATE camera c
SET camera_id = m.new_id
FROM (VALUES
    ('cam1', 'cam_A'),
    ('cam2', 'cam_B'),
    ('cam3', 'cam_C'),
    ('cam4', 'cam_D')
) AS m(old_id, new_id)
WHERE c.camera_id = m.old_id
  AND c.site_id = 'classroom-1'
  AND NOT EXISTS (SELECT 1 FROM camera existing WHERE existing.camera_id = m.new_id)
  AND NOT EXISTS (SELECT 1 FROM calibration_camera cc WHERE cc.camera_id = c.camera_id)
  AND NOT EXISTS (SELECT 1 FROM coverage_guide cg WHERE cg.camera_id = c.camera_id)
  AND NOT EXISTS (SELECT 1 FROM media_clip mc WHERE mc.camera_id = c.camera_id)
  AND NOT EXISTS (SELECT 1 FROM video_segment vs WHERE vs.camera_id = c.camera_id)
  AND NOT EXISTS (SELECT 1 FROM system_metric sm WHERE sm.camera_id = c.camera_id)
  AND NOT EXISTS (SELECT 1 FROM setup_session_artifact ssa WHERE ssa.camera_id = c.camera_id)
  AND NOT EXISTS (SELECT 1 FROM setup_zone_opinion szo WHERE szo.camera_id = c.camera_id);

COMMENT ON COLUMN camera.camera_id IS '예: cam_A';
