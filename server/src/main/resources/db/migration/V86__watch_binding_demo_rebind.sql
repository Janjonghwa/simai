-- simAI V86: 시연 워치 1명 자동 배정 방식 추가 (S15P31A101-826, 확장 부록 C.6, docs/design/S15P31A101-826-demo-watch-rebind.md)
-- 배정은 시간이 지나도 풀지 않는다(Re-ID 재입장 시 원래 배정·이름 유지).
-- 배정된 World ID 가 지금 보이지 않을 때(>10초 미관측, 수동 확정 포함)만 새 후보(걸음 대조 또는 시연 스위치 1명 자동 배정)가
-- 옛 행을 기존 REPLACED 사유로 닫고 갈아 끼운다(추가 전용).
-- 시연 스위치(simai.watch.demo-device)가 켜져 있고 LIVE 추적 사람이 정확히 1명이며 5초 넘게 이어지면
-- 임시 성격(DEMO_SINGLE, 걸음 대조 반대 근거로 해제·일치 시 MOTION_MATCH 승격)으로 잇는다.
ALTER TABLE watch_binding DROP CONSTRAINT watch_binding_method_check;
ALTER TABLE watch_binding ADD CONSTRAINT watch_binding_method_check
    CHECK (method IN ('MANUAL', 'MOTION_MATCH', 'DEMO_SINGLE'));

COMMENT ON COLUMN watch_binding.method IS 'MANUAL 관리자 수동 배정, MOTION_MATCH 움직임 대조 배정, DEMO_SINGLE 시연 스위치 1명 자동 배정(임시 성격, V86)';
COMMENT ON COLUMN watch_binding.score IS '움직임 대조 점수(0~1). MANUAL 은 1, DEMO_SINGLE 은 0';
