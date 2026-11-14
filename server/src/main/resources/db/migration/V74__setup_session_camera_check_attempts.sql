-- 세팅 카메라 확인을 맵 앞으로 옮기고 확인만 다시 하기(S15P31A101-687, docs/design/S15P31A101-687-camera-check-before-map.md).
-- camera_check_request: 관리자가 걷기 창에서 「걷기 시작」·「확인만 다시」를 누른 가장 최근 요청(attempt = 이 대여 안의 시도 번호,
-- lease_id = 요청을 받은 대여, requested_by, requested_at_ms). 워커는 하트비트 응답(job_lease_status 2.3.0 camera_check_start)으로
-- 받는다. 다른 대여(재대여)에는 보내지 않는다(재대여한 워커는 처음부터 걷기 시작을 기다린다).
-- camera_check_attempts: 끝난 시도(PASSED·FAILED·INCOMPLETE)마다 한 줄씩 덧붙이는 기록(시도 ID·번호·대여·보정·시작/만료 시각·문제 사유).
-- 덮어쓰지 않는다. 세팅 표시·기록용이다. Tier 0 위치·위험 판정에는 쓰지 않는다.

ALTER TABLE setup_session ADD COLUMN camera_check_request jsonb;
ALTER TABLE setup_session ADD COLUMN camera_check_attempts jsonb NOT NULL DEFAULT '[]'::jsonb;

COMMENT ON COLUMN setup_session.camera_check_request IS
    '카메라 확인 걷기 시작·확인만 다시 요청(attempt·lease_id·requested_by·requested_at_ms). 없으면 NULL';
COMMENT ON COLUMN setup_session.camera_check_attempts IS
    '끝난 카메라 확인 시도 기록(덧붙이기만, id·no·lease_id·status·calibration_version·started_at_ms·expires_at_ms·reasons)';
