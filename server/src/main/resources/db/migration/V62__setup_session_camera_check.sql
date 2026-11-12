-- 세팅 보정 품질 게이트·「카메라 확인」(S15P31A101-646, docs/design/S15P31A101-646-setup-camera-check.md).
-- site_setup 1.14.0 하트비트·결과의 quality_gate(Stage A 쌍별 홀드아웃 절대 상한·최소 홀드아웃·카메라 높이 차 판정)와
-- camera_check(보정 뒤 사람 걷기로 관제와 같은 계산: 쌍 통과율·잔차, 한 카메라 제외 재투영, 4대 관측)를 세션에 남긴다.
-- 값을 실은 마지막 스냅샷 것으로 바꾸고, 값이 없는 스냅샷은 이전 값을 둔다.
-- camera_check.status 가 PASSED 이고 camera_check.calibration_version 이 세션 calibration_version 과 같을 때만
-- 구역 목록 저장·채우기와 세팅 완료(C17)를 받는다. Tier 0 위치·위험 판정에는 쓰지 않는다.

ALTER TABLE setup_session ADD COLUMN quality_gate jsonb;
ALTER TABLE setup_session ADD COLUMN camera_check jsonb;

COMMENT ON COLUMN setup_session.quality_gate IS
    '보정 절대 품질 게이트(site_setup quality_gate 의 마지막 값). 받은 적 없으면 NULL';
COMMENT ON COLUMN setup_session.camera_check IS
    '세팅 카메라 확인(site_setup camera_check 의 마지막 값). PASSED 이고 보정 버전이 같아야 구역 단계·세팅 완료를 받는다';
