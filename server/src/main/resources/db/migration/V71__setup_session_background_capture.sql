-- 세팅 배경 촬영 진행(S15P31A101-686, docs/design/S15P31A101-686-fresh-setup-background.md).
-- site_setup 1.16.0 COLLECTING_BACKGROUND 하트비트의 background_capture(세팅 시작 뒤 새로 모은 표본 수, 카메라별
-- READY·INSUFFICIENT_SAMPLES·PERSON_OCCLUDED·NO_VIDEO)를 세션에 남긴다. 값을 실은 마지막 스냅샷 것으로 바꾸고,
-- 값이 없는 스냅샷은 같은 대여 안에서 이전 값을 둔다(재대여는 처음부터 다시 찍으므로 지운다).
-- 화면의 배경 촬영 창 표시용이다. Tier 0 위치·위험 판정에는 쓰지 않는다.

ALTER TABLE setup_session ADD COLUMN background_capture jsonb;

COMMENT ON COLUMN setup_session.background_capture IS
    '세팅 배경 촬영 진행(site_setup background_capture 의 마지막 값). 받은 적 없으면 NULL';
