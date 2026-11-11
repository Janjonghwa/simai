-- 굽기 회수 세션도 맵 다시 굽기 결과를 받는다(S15P31A101-621, docs/design/S15P31A101-621-rebake-live-reclaim.md).
-- V50(-597)은 bake_skipped 가 있으면 scene_version 이 비어 있어야 한다고 막았다. 그런데 -607 은 회수 세션(정식 맵 없음)도
-- 미리보기를 기준으로 다시 굽기를 허용하고, APPLIED 면 세션의 맵(scene_version)을 새 버전으로 옮긴다. 그 UPDATE 가 CHECK 에 걸려
-- 결과 제출 트랜잭션 전체가 되돌아갔다. bake_skipped 는 "정식 굽기를 작업 시작으로 회수했었다"는 이력으로 남기고(값은 그대로),
-- scene_version 은 다시 굽기로 채울 수 있게 조건에서 뺀다. 세팅 결과가 회수를 알릴 때 scene_version 이 비는 것은 계약(site_setup
-- 1.7.0 이후 result 의 bake_skipped ⇔ scene_version null)과 SetupService 가 그대로 지킨다. 맵이 바뀐 이력은 setup_supplement_rebake 에 있다.
-- V51(-609)·V52(-606)·V53(-607)·V54(-610)·V55(-614 세팅 모드)·V56(-611 TBM 제출)·V57(-612 세팅 구역 목록) 다음이라 V58 을 쓴다.

-- V50 은 칼럼 정의 안에서 이름 없이 만들어 Postgres 가 이름을 지었다(여러 칼럼을 보므로 setup_session_check, 겹치면 숫자 접미).
-- 환경마다 이름이 다를 수 있어 bake_skipped 를 보는 CHECK 를 정의로 찾아 지운다(정확히 하나여야 한다).
DO $$
DECLARE
    found text[];
BEGIN
    SELECT array_agg(conname) INTO found FROM pg_constraint
    WHERE conrelid = 'setup_session'::regclass AND contype = 'c' AND pg_get_constraintdef(oid) LIKE '%bake_skipped%';
    IF coalesce(array_length(found, 1), 0) <> 1 THEN
        RAISE EXCEPTION 'setup_session 의 bake_skipped CHECK 가 하나가 아니다: %', found;
    END IF;
    EXECUTE format('ALTER TABLE setup_session DROP CONSTRAINT %I', found[1]);
END $$;

ALTER TABLE setup_session ADD CONSTRAINT setup_session_bake_skipped_check
    CHECK (bake_skipped IS NULL OR (bake_skipped = 'LIVE_WORK_STARTED' AND status = 'COMPLETED'));

COMMENT ON COLUMN setup_session.bake_skipped IS
    '정식 굽기를 건너뛴 이유(site_setup 1.7.0). LIVE_WORK_STARTED = 같은 현장 작업 시작으로 회수. 있으면 COMPLETED. '
    'scene_version 은 회수 때 비고, 뒤에 맵 다시 굽기(SUPPLEMENT_REBAKE) APPLIED 로 채워질 수 있다(V58, -621)';
