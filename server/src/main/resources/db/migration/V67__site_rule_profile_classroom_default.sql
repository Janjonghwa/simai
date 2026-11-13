-- S15P31A101-669. 규칙 기준을 교실 시험(CLASSROOM_TEST, 고소 0.35 m)으로 고정한다(사용자 결정 2026-11-13: 시연만 한다).
-- 관리자 메뉴의 규칙 기준 선택을 없애므로 새 현장·기존 현장 모두 교실 시험이다. 이후 묶음(세팅 완료·작업 시작·구역 저장·발행)이
-- 이 값으로 규칙 판(R2·R3·R5 v1001)을 고른다. CHECK 제약은 그대로 두어 운영 값(OPERATION)도 계약상 남긴다.
-- 이미 발행한 묶음(config_bundle.rule_profile)은 기록이라 고치지 않는다(덮어쓰지 않는다).
ALTER TABLE site ALTER COLUMN rule_profile SET DEFAULT 'CLASSROOM_TEST';

UPDATE site SET rule_profile = 'CLASSROOM_TEST' WHERE rule_profile <> 'CLASSROOM_TEST';

COMMENT ON COLUMN site.rule_profile IS
    '규칙 기준(OPERATION 운영 2.0 m · CLASSROOM_TEST 교실 시험 0.35 m, 명세 7.2·7.4). 기본 교실 시험(S15P31A101-669). 구성 묶음은 이 값으로 규칙 판을 고른다(S15P31A101-654)';
