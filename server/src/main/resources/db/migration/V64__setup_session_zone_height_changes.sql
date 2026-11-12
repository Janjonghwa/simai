-- 세팅 구역 상면 높이 차이 판정(S15P31A101-630, docs/design/S15P31A101-630-zone-height-change.md).
-- site_setup 1.15.0 워커가 미리보기·정식·다시 굽기 단계에서 이전 관제 Scene 과 이번 세팅 Scene 의 상면 높이를 비교해 보낸
-- zone_height_changes 요약을 세션에 남긴다. 표시용이며 구역 확인(needs_review) 자동 처리나 위험 판정에는 쓰지 않는다.
ALTER TABLE setup_session
    ADD COLUMN IF NOT EXISTS zone_height_changes jsonb;
