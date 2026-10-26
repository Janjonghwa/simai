-- S15P31A101-106: Tier 0 저널 세대(journal_epoch).
-- Tier 0 Pod 가 바뀌어 저널이 비면(명세 3.12 컨테이너 디스크) journal_offset 이 0 부터 다시 시작한다.
-- 세대 없이 (gateway_id, journal_offset) 로만 묶으면 새 저널의 전이가 옛 기록과 겹쳐 조용히 버려진다.
-- EC2 는 같은 offset 에 다른 msg_id 가 오거나 스냅샷이 새 저널을 알리면 세대를 올린다(docs/design/server-event-journal.md).
ALTER TABLE journal_cursor ADD COLUMN journal_epoch integer NOT NULL DEFAULT 0;

ALTER TABLE risk_event_update ADD COLUMN journal_epoch integer NOT NULL DEFAULT 0;
ALTER TABLE risk_event_update DROP CONSTRAINT risk_event_update_journal_key;
ALTER TABLE risk_event_update
    ADD CONSTRAINT risk_event_update_journal_key UNIQUE (gateway_id, journal_epoch, journal_offset);
