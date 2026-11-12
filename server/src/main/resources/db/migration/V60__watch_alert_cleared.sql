-- S15P31A101-634. 해소된 이벤트의 워치 알림을 지우라는 통지(I4 watch_alert_clear 1.0.0)를 그 워치에 보낸 시각.
-- 이벤트·워치마다 한 번만 보내고(행마다 같은 시각을 적는다), 끊긴 동안 해소된 것은 다시 이어질 때 보낸다. 덮어쓰지 않고 처음 시각만 남긴다.
-- V59 는 S15P31A101-632(!582, lookup_indexes) 몫이라 V60 을 쓴다. outOfOrder 를 켜지 않았으므로 V60 이 먼저 배포되면 뒤에 들어온 V59 는
-- 검증에서 막힌다. !582 가 먼저 머지되지 않으면 머지 직전에 번호를 다시 고른다.
ALTER TABLE watch_alert ADD COLUMN cleared_at timestamptz;

COMMENT ON COLUMN watch_alert.cleared_at IS
    '이 알림의 이벤트가 해소돼 워치에 watch_alert_clear 를 보낸 시각(EC2 시계). NULL 이면 아직 안 보냄(해소 전이거나 워치가 끊겨 있었다)';

-- 워치가 다시 이어질 때 그 워치의 아직 지우지 않은 이벤트 알림을 찾는다
CREATE INDEX watch_alert_uncleared_idx ON watch_alert (device_id) WHERE cleared_at IS NULL AND origin = 'EVENT';
