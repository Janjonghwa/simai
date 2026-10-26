-- S15P31A101-222: 기기·워커 토큰 확인(요청마다 token_hash 로 찾는다).
-- token_hash 를 UNIQUE 로 둬 순차 탐색을 없애고, 같은 해시가 두 주체에 걸려 한 기기가 다른 기기로 인증되는 일을 막는다.
-- worker 에도 rotated_at 을 둬 회전해도 처음 발급 시각(issued_at)이 남게 한다(device 와 같게).
CREATE UNIQUE INDEX device_token_hash_key ON device (token_hash);
CREATE UNIQUE INDEX worker_token_hash_key ON worker (token_hash);

ALTER TABLE worker ADD COLUMN rotated_at timestamptz;
COMMENT ON COLUMN worker.rotated_at IS '토큰 회전(14장)';
