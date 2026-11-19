-- simAI V85: 워치 배정 자리 이어받기 (S15P31A101-793, 확장 부록 C.6, docs/design/server-watch-match.md)
-- 배정(수동·대조)된 트랙이 사라진 자리에 짧은 시간 안에 새 트랙 하나만 나타나면 배정을 새 World ID 로 옮긴다.
-- 옮긴 행은 handoff_from 에 옛 ID 를 적고, 걸음 대조가 같은 짝을 다시 뽑으면 handoff_confirmed_at 으로 확정한다.
-- 확정 전(임시)에는 수동 배정이라도 대조가 반대 근거로 풀 수 있다. 옛 행은 지우지 않고 HANDOFF 로 닫는다(V42 병합과 같은 방식).
ALTER TABLE watch_binding
    ADD COLUMN handoff_from text CHECK (handoff_from IS NULL OR char_length(handoff_from) BETWEEN 1 AND 64),
    ADD COLUMN handoff_confirmed_at timestamptz,
    ADD CONSTRAINT watch_binding_handoff_confirm_check CHECK (handoff_confirmed_at IS NULL OR handoff_from IS NOT NULL),
    ADD CONSTRAINT watch_binding_handoff_self_check CHECK (handoff_from IS NULL OR handoff_from <> world_id);

ALTER TABLE watch_binding DROP CONSTRAINT watch_binding_unbind_reason_check;
ALTER TABLE watch_binding ADD CONSTRAINT watch_binding_unbind_reason_check
    CHECK (unbind_reason IN ('RELEASED', 'REPLACED', 'MATCH_LOST', 'IDENTITY_CHANGED', 'ID_MERGED', 'HANDOFF'));

COMMENT ON COLUMN watch_binding.handoff_from IS '자리 이어받기로 옮겨 온 배정이면 사라진 옛 World ID(S15P31A101-793). 아니면 NULL';
COMMENT ON COLUMN watch_binding.handoff_confirmed_at IS '옮겨 온 배정을 걸음 대조가 같은 짝으로 다시 뽑아 확정한 시각. NULL 이면 임시(대조가 반대 근거로 풀 수 있다)';
COMMENT ON COLUMN watch_binding.unbind_reason IS 'RELEASED 관리자가 풂, REPLACED 같은 워치·World ID 의 새 배정, MATCH_LOST 대조 점수가 떨어지거나 트랙이 사라짐(임시 이어받기의 반대 근거 포함), IDENTITY_CHANGED World ID 병합·분할로 대조 배정을 풂, ID_MERGED World ID 병합으로 살아남은 ID 로 옮김, HANDOFF 사라진 자리의 새 ID 로 옮김(V85)';
