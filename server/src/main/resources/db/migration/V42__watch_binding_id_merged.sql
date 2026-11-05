-- World ID 병합(world_state aliases)으로 옛 ID 의 배정을 살아남은 ID 로 옮길 때, 옛 배정 행의 마감 사유(S15P31A101-508).
-- 옮긴 새 행은 같은 워치·방식(MOTION_MATCH·MANUAL)으로 새로 더하고 옛 행은 지우지 않고 닫는다.
ALTER TABLE watch_binding DROP CONSTRAINT watch_binding_unbind_reason_check;
ALTER TABLE watch_binding ADD CONSTRAINT watch_binding_unbind_reason_check
    CHECK (unbind_reason IN ('RELEASED', 'REPLACED', 'MATCH_LOST', 'IDENTITY_CHANGED', 'ID_MERGED'));
COMMENT ON COLUMN watch_binding.unbind_reason IS 'RELEASED 관리자가 풂, REPLACED 같은 워치·World ID 의 새 배정, MATCH_LOST 대조 점수가 떨어지거나 트랙이 사라짐, IDENTITY_CHANGED World ID 병합·분할로 대조 배정을 풂, ID_MERGED World ID 병합으로 살아남은 ID 로 옮김(또는 살아남은 ID 에 이미 수동 배정이 있어 옛 배정을 닫음)';
