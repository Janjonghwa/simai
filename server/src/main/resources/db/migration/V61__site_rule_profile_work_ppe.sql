-- S15P31A101-654. 현장 규칙 기준(운영·교실 시험)과 작업별 안전모·조끼 요구.
-- 1) site.rule_profile: 이후 모든 구성 묶음이 이 값으로 규칙 판을 고른다(이전 묶음 상속보다 우선, docs/design/S15P31A101-654-ppe-r3-demo.md).
--    지금 동작이 바뀌지 않게 가장 최근 묶음의 프로필로 채우고, 묶음이 없으면 운영이다.
-- 2) work_unit.helmet_required·vest_required: 작업 시작 때 ppe_policy 에 싣는 요구. 기본은 필수(끌 수 있다). 예전 작업도 필수로 채운다
--    (지금까지 진행 중 작업이면 늘 둘 다 REQUIRED 였다).
ALTER TABLE site ADD COLUMN rule_profile text NOT NULL DEFAULT 'OPERATION'
    CHECK (rule_profile IN ('OPERATION', 'CLASSROOM_TEST'));

UPDATE site s SET rule_profile = COALESCE((
    SELECT b.rule_profile FROM config_bundle b
    WHERE b.site_id = s.site_id AND b.rule_profile IN ('OPERATION', 'CLASSROOM_TEST')
    ORDER BY b.issued_at DESC LIMIT 1), 'OPERATION');

COMMENT ON COLUMN site.rule_profile IS
    '규칙 기준(OPERATION 운영 2.0 m · CLASSROOM_TEST 교실 시험 0.35 m, 명세 7.2·7.4). 구성 묶음은 이 값으로 규칙 판을 고른다(S15P31A101-654)';

ALTER TABLE work_unit
    ADD COLUMN helmet_required boolean NOT NULL DEFAULT true,
    ADD COLUMN vest_required boolean NOT NULL DEFAULT true;

COMMENT ON COLUMN work_unit.helmet_required IS '작업 중 안전모 필수(ppe_policy.helmet REQUIRED). 기본 필수(S15P31A101-654)';
COMMENT ON COLUMN work_unit.vest_required IS '작업 중 조끼 필수(ppe_policy.vest REQUIRED). 기본 필수(S15P31A101-654)';
