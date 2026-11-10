-- 새로 만든 구역만 해당 세팅에 연결한다. 기존 구역을 시간으로 추정해 가져오지 않는다.
CREATE TABLE setup_session_zone (
    setup_session_id uuid NOT NULL REFERENCES setup_session(setup_session_id),
    zone_id text NOT NULL,
    zone_version integer NOT NULL,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (setup_session_id, zone_id),
    FOREIGN KEY (zone_id, zone_version) REFERENCES zone(zone_id, zone_version)
);
