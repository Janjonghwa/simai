-- S15P31A101-612: 세팅 중 구역은 세션의 목록으로 저장하고, 세팅 완료(C17) 때 한 번에 관제 구역으로 적용한다
-- (docs/design/setup-session-zones.md). 세팅 중에는 관제 구역(zone)에 쓰지 않는다.
-- items: [{zone_id|null, source AI|MANUAL, candidate_id|null, zone(구역 본문|null), surface(작업면 본문|null)}]
-- revision: 저장마다 1 증가. 다른 PC 와 동시에 고치면 409 로 막는다.
CREATE TABLE setup_session_zone_plan (
    setup_session_id uuid PRIMARY KEY REFERENCES setup_session(setup_session_id),
    items jsonb NOT NULL DEFAULT '[]',
    revision integer NOT NULL DEFAULT 0 CHECK (revision >= 0),
    updated_by bigint NOT NULL REFERENCES app_user(user_id),
    updated_at timestamptz NOT NULL
);
