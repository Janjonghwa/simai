-- 작업 시작(LIVE) 때 현장 세팅 정식 굽기 회수(S15P31A101-597, 명세 3.2 5항, docs/design/S15P31A101-597-setup-bake-live-reclaim.md).
-- site_setup 1.7.0 결과가 정식 굽기를 끝내지 못하고 회수했다고 알리면(bake_skipped) 세션은 COMPLETED 로 끝나되 scene_version 은 비운다.
-- 관제는 READY_FOR_ZONES 때 현재 Scene 이 된 미리보기로 한다. 값은 추가만 하고 덮어쓰지 않는다(서버가 COALESCE 로 둔다).

ALTER TABLE setup_session ADD COLUMN bake_skipped text
    CHECK (bake_skipped IS NULL OR (bake_skipped = 'LIVE_WORK_STARTED' AND status = 'COMPLETED' AND scene_version IS NULL));

COMMENT ON COLUMN setup_session.bake_skipped IS
    '정식 굽기를 건너뛴 이유(site_setup 1.7.0). LIVE_WORK_STARTED = 같은 현장 작업 시작으로 회수. 있으면 COMPLETED·scene_version 없음';
