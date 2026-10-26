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

## 단절 감시와 장애 기록 (S15P31A101-122)

FR-11·NFR-9·G3("단절 10초 안에 관리자 화면 알림"). 화면 알림은 위 `viewer_health` 가 하고(관제 앱 `features/layout/status.ts` 가
remote_state·laptop.link 로 "관제 정지"·"현장 장비 응답 없음"·"원격 전달 단절 · 현장 경보 동작 중"을 띄운다), -122 는 판정 기준과 기록을 보탠다.

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 끊김 시계 | 받는 쪽 단조 시계(System.nanoTime), 순서도 도착 순서. 화면에 싣는 시각만 벽시계 | FR-11 "하트비트는 받는 쪽 단조 시계로". 벽시계를 맞춰도 끊김이 생기거나 사라지지 않게 |
| Tier 0 정상 | 연결(health 3초 안) + 처리 진행(`health`·`world_state` 의 `t_latest_global` 이 3초 안에 늘어남, 뒤로 갔는데 2초 동안 더 큰 값이 없으면 그 값을 새 기준으로). health 는 오는데 처리가 멈추면 `tier0_link` 는 CONNECTED, remote_state 는 CONTROL_HALTED(계약 설명 보강) |
| 원격 단절 판정 | Tier 0 를 잃은 뒤 받은 노트북 하트비트가 Tier 0 정상을 알릴 때만 REMOTE_LINK_LOST. 그 하트비트가 오기 전(1초 안팎)은 CONTROL_HALTED | Tier 0 가 죽었는데 '현장 경보 동작 중'으로 안심시키지 않는다 | FR-11 "Tier 0 정상은 처리 진행(t_latest_global 증가)으로 판정" |
| 장애 종류 | REMOTE_LINK_LOST, CONTROL_HALTED 중 노트북 하트비트 끊김이면 DEVICE_DOWN, 그 밖 CONTROL_HALTED | 관제 앱 status.ts 와 같은 규칙 |
| 장애 기록 | V7 `system_outage`: 종류가 바뀔 때 한 행(`last_seen_at` 실패한 쪽의 마지막 정상 근거 — 노트북 끊김은 마지막 하트비트, Tier 0 끊김은 마지막 health, 처리 정체는 마지막 진행, 노트북이 알린 관제 정지는 노트북이 마지막으로 정상을 알린 때 / `detected_at` EC2 판정 / `ended_at`·`end_reason`). 열린 장애는 하나(부분 유일 색인). 화면 알림을 먼저 보내고 기록하며, 기록이 실패하면 다음 주기에 다시 시도한다 | G3 단절 감지 시간(10초 안)을 나중에 잰다 |
| 재시작 | ApplicationReady 뒤 Tier 0·노트북을 둘 다 한 번씩 받거나 30초(`simai.health.outage-startup-grace-ms`)가 지나야 기록한다. 닫히지 않은 기록은 뜰 때 EC2_RESTART 로 닫는다 | EC2 재시작·재배포를 현장 장애로 남기지 않는다 |

## 시험

`HealthIntegrationTest`(끊김 기준 1.5초): 하트비트 인증·source_id·형식·기록, 노트북이 끊기면 GET 과 `/topic/health` 가 같은 단절(CONTROL_HALTED·UNSAFE_UNKNOWN)·Scene 불일치,
Tier 0 만 끊기면 REMOTE_LINK_LOST, 노트북이 관제 정지를 알리거나 Tier 0 제어 연결이 끊기면 CONTROL_HALTED. 응답은 `contracts/` 스키마로 검사한다.

## 남은 일

- 장애를 반장 PWA 로 Web Push 알림(I3): PWA·Web Push 이슈(M1). 지금은 관제 화면(`/topic/health`)만 알린다.
- `GET /api/scenes/current`(Scene 관리)는 Scene 이슈. 지금은 `scene_version` 표를 바로 읽는다.
- 노트북이 여럿일 때 현장 노트북을 정하는 설정(지금은 모두 정상이어야 정상).
