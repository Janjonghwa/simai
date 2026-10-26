# EC2 노트북 하트비트와 System Health

S15P31A101-262. 명세 3.2(이중 감시), 3.10(3회 누락), 6.2·6.5, 12.4(`/topic/health`), 12.6(`GET /api/health`), FR-5·11·12·134,
계약 `laptop_heartbeat`·`viewer_health` 1.0.0. 코드는 `server/.../domain/health/`. 받는 쪽은 관제 앱 `features/layout/`(GET)·`lib/stomp.ts`(토픽),
보내는 쪽은 노트북 `gateway/site_alert/.../heartbeat.py`.

## 흐름

1. 노트북이 1초마다 `POST /api/devices/heartbeat`(기기 토큰, S15P31A101-222)로 `laptop_heartbeat` 봉투를 보낸다. EC2 는 `device_heartbeat` 에 추가하고
   `device.last_heartbeat_at` 을 갱신한 뒤 204 로 답한다.
2. Tier 0 `health`(I1)를 저장하면 원문과 수신 시각을, `world_state` 를 저장하면 `t_latest_global`·`scene_version` 을 메모리(`HealthState`)에 둔다.
3. `viewer_health` 를 만들어 1초마다(바뀌면 즉시) `/topic/health` 로 보내고, `GET /api/health` 는 같은 메시지(봉투 포함)를 돌려준다.

## 명세가 비워 둔 곳에서 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 연결 판단 | 마지막 수신(EC2 시각)이 3초 안이면 CONNECTED(`simai.health.link-timeout-ms`) | 3.10 1초 주기 3회 누락. Tier 0 는 `health`, 노트북은 하트비트 |
| remote_state | 노트북 정상 = `tier0.control`=CONNECTED·`site_display`≠CONTROL_HALTED. 두 연결이 살아 있고 노트북 정상이면 NORMAL, Tier 0 만 끊기고 노트북 정상이면 REMOTE_LINK_LOST, 그 밖은 CONTROL_HALTED(노트북 끊김, 연결이 살아 있어도 노트북이 관제 정지를 알림) | 계약 `laptop_heartbeat`·`viewer_health` 설명. control 만 보지 않는다. NORMAL 은 'Tier 0·노트북 모두 정상' |
| system_health | remote_state 가 NORMAL 이 아니면 UNSAFE_UNKNOWN, 아니면 Tier 0 `health` 값(없으면 UNSAFE_UNKNOWN) | 3.2 이중 감시, 계약 조건 |
| 노트북 여럿 | 노트북마다 따로 두고, 살아 있는 노트북이 모두 정상일 때만 정상. 하나라도 살아 있으면 `laptop.link` CONNECTED | MVP 는 하나. 현장 노트북이 죽고 예비가 살아 있으면 구분하지 못한다(남은 일) |
| world_state 시각 | 뒤로 간 `t_latest_global` 은 버리되, 더 새 프레임 없이 2초가 지나면 받는다 | Tier 0 시계 보정·재시작 뒤 데이터 연령이 굳지 않게 |
| data_age_ms | 지금 − 마지막 `t_latest_global`(음수는 0), 받은 적 없으면 null | EC2·Tier 0 시계는 NTP 로 맞춘다고 본다. EC2↔Tier 0 시계 차이를 재는 계약이 없다 |
| calibration_state | Tier 0 가 연결돼 있으면 `health` 값, 끊겼거나 모르면 UNCALIBRATED | 모르면 정상으로 보지 않는다 |
| config | 발행: 최근 `config_bundle`, 적용: `applied_at` 이 있는 최근 묶음, sync: Tier 0 연결 중이면 Tier 0 `health.config_sync`, 끊겼으면 UNVERIFIED | 구성 묶음 발행·적용은 S15P31A101-120 |
| scene | COMMITTED·`valid_to` 없음 중 최근 `committed_at` vs 마지막 `world_state.scene_version`, 둘 다 있고 다르면 mismatch | FR-12 |
| 상태 보관 | 메모리. 서버가 다시 뜨면 다음 수신(1초)까지 끊김으로 보인다 | 하트비트 정본은 `device_heartbeat` |
| 바로 보내기 | 받은 값(Tier 0 health·하트비트·Scene 버전)으로 화면이 달라질 값이 바뀌면 1초를 기다리지 않는다. 끊김과 DB 쪽 변경(새 COMMITTED Scene·구성 적용)은 1초 주기가 잡는다 | 12.4 "바뀌면 즉시" |
| 발행 스레드 | 전용 스레드 하나(`health-publisher`)에서 1초 주기와 바뀜 알림을 모두 보낸다 | I1·하트비트 요청을 붙잡지 않고, 공유 스케줄러가 막혀도 주기가 돌며, 만든 순서대로 나간다 |
| 하트비트 응답 | 204, 본문 없음. 형식이 틀리면(선택 항목 `ec2`·`alarm`·`cameras` 포함) 또는 source_id 가 토큰의 기기와 다르면 400, 노트북이 아닌 기기는 403 | 노트북은 2xx 만 성공으로 본다 |

## 시험

`HealthIntegrationTest`(끊김 기준 1.5초): 하트비트 인증·source_id·형식·기록, 노트북이 끊기면 GET 과 `/topic/health` 가 같은 단절(CONTROL_HALTED·UNSAFE_UNKNOWN)·Scene 불일치,
Tier 0 만 끊기면 REMOTE_LINK_LOST, 노트북이 관제 정지를 알리거나 Tier 0 제어 연결이 끊기면 CONTROL_HALTED. 응답은 `contracts/` 스키마로 검사한다.

## 남은 일

- 단절 감시에 따른 장애 알림(10초 안, G3)은 S15P31A101-122.
- `GET /api/scenes/current`(Scene 관리)는 Scene 이슈. 지금은 `scene_version` 표를 바로 읽는다.
- 노트북이 여럿일 때 현장 노트북을 정하는 설정(지금은 모두 정상이어야 정상).
