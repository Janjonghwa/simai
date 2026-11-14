-- 세팅 카메라 높이 빠른 확인(S15P31A101-690, docs/design/S15P31A101-690-height-check-in-input.md).
-- height_review: site_setup 1.17.0 INITIAL_POSE_AND_WORLD 하트비트의 height_review(카메라별 입력 대비 상대 불일치 OK·LOW·HIGH,
-- 확인 상태 PASSED·AWAITING_CONFIRMATION·CONFIRMED·CORRECTED·TIMED_OUT, 워커가 쓰는 높이)의 마지막 값. 값이 없는 스냅샷은
-- 같은 대여 안에서 이전 값을 두고 재대여는 지운다(background_capture 와 같은 규칙).
-- height_decision: 관리자가 확인 창에서 고르고 보낸 결정(CONFIRMED·CORRECTED, 진행할 높이, 이전 높이, 누가·언제). 한 세션에 한 번만
-- 받고 덮어쓰지 않는다. 워커는 하트비트 응답(job_lease_status 2.2.0 height_decision)으로 받는다.
-- 세팅 표시·대기용이다. Tier 0 위치·위험 판정에는 쓰지 않는다.

ALTER TABLE setup_session ADD COLUMN height_review jsonb;
ALTER TABLE setup_session ADD COLUMN height_decision jsonb;

COMMENT ON COLUMN setup_session.height_review IS
    '카메라 높이 빠른 확인(site_setup height_review 의 마지막 값). 받은 적 없으면 NULL';
COMMENT ON COLUMN setup_session.height_decision IS
    '관리자의 카메라 높이 확인 결정(decision·camera_heights_m·previous_camera_heights_m·decided_by·decided_at_ms). 없으면 NULL';
