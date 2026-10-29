# EC2 최소 리플레이 조회

S15P31A101-136. 명세 8.5(리플레이), 11.4(리플레이 질의), 12.6(`GET /api/world?at={t}`·`GET /api/replay?from={t1}&to={t2}`, 열람), FR-28.
질의는 [db-verification.md](db-verification.md) 5장 Q1~Q3 을 따른다(Q4 그 시각의 구역·규칙·Scene 은 확장이라 뺐다).
코드는 `server/.../domain/replay/`(`ReplayController`·`ReplayRepository`), 시험은 `ReplayIntegrationTest`.
쓰는 쪽은 관제 앱 히스토리 화면(S15P31A101-320, -164)이고, 3D 재생은 Spatial 재생 엔진(`front/.../viewer3d/replay`, -187)이 한다.

새로 저장하는 것은 없다. -103 이 쌓는 `world_track_state`·`skeleton_3d`·`system_health` 와 -106 이 쌓는 `risk_event_update` 를 읽기만 한다.

## 응답

메시지는 관제 앱이 STOMP 로 받는 것과 같은 봉투(명세 12.1)이고, 재생 엔진의 `parseReplayLog` 가 줄마다 받는 모양과 같다.
world_state 는 그대로 넣을 수 있다. 이벤트는 저장된 출처 `LIVE` 로 오므로 화면이 `source` 를 `REPLAY` 로 바꾼 뒤 넣는다
(엔진은 REPLAY 가 아닌 이벤트를 `NOT_REPLAY_SOURCE` 로 버린다. -320 작업 항목 "기록된 LIVE 이벤트는 REPLAY 출처로 바꿔 넣어").

`GET /api/replay?from=ms&to=ms&step_ms=100`

```json
{
  "from": 1760342400000, "to": 1760342700000, "step_ms": 100,
  "world_states": [
    { "schema_version": "2.1.0", "msg_type": "world_state", "msg_id": "<Tier 0 msg_id>", "sent_at": 1760342400130,
      "source_id": "ec2", "payload": { "t_latest_global": 1760342400100, "calibration_version": "cal-7", "scene_version": "scene-3",
                                         "config_version": "cfg-12", "input_provenance": { }, "tracks": [ ] } }
  ],
  "initial_events": [ { "schema_version": "2.0.0", "msg_type": "risk_event", "msg_id": "...", "sent_at": 0, "source_id": "ec2",
                        "payload": { "event_id": "...", "source": "LIVE", "lifecycle": "RAISED", "event_sequence": 1, "raised_at": 0 } } ],
  "events": [ ],
  "gaps": [ { "from": 1760342401500, "to": 1760342408000 } ]
}
```

- `world_states`: 구간 [from, to] 의 tick 을 시간순으로. payload 는 Tier 0 가 보낸 값 그대로다(시험이 보낸 payload 와 같음을 본다).
- `initial_events`: from 시각에 활성인 이벤트의 그 시각 상태(Q2). 구간 전에 시작한 경보를 놓치지 않게 한다.
- `events`: (from, to] 의 생명주기 전이마다 그 직후 이벤트 상태, 관측 시각순(Q3).
- `gaps`: 기록이 없던 구간. 이 안에서 world 가 없으면 "데이터 없음"이고, 밖에서 world 가 없으면 사람이 없던 것이다.

`GET /api/world?at=ms` → `{ "at": ms, "world_state": {봉투} | null, "events": [봉투] }`

## 명세가 비워 둔 곳에서 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 메시지 모양 | 실시간과 같은 봉투(world_state 계약, I2 risk_event 2.0.0). `source_id` 는 `ec2`, `msg_id` 는 Tier 0 가 붙인 원래 값, `sent_at` 은 EC2 가 받은 시각 | 화면이 재생 엔진(-187)을 그대로 쓴다(-320). 원래 msg_id 라 같은 구간을 다시 읽어도 같고 엔진의 중복 검사에 걸리지 않는다 |
| 이벤트 출처 | 저장된 그대로 `LIVE` 로 준다 | 기록의 사실을 바꾸지 않는다. 화면이 REPLAY 로 바꿔 넣어 실제 알림 경로와 섞지 않는다(-320 작업 항목) |
| world_state 판 | 받은 `schema_version` 은 저장하지 않으므로 값으로 고른다: UNCERTAIN 인데 `head_z` 가 null 인 트랙이 있으면 `1.4.0`, 그 밖은 `2.1.0` | 판에 따라 뜻이 갈리는 칸은 head_z 하나다(계약 설명). 2.1.0 의 split_from 은 1.x 메시지에 없어 부딪히지 않는다 |
| 칸 복원 | 필수 칸은 값이 없으면 null, 선택 칸은 값이 없으면 뺀다. real(float4) 칸은 float 의 가장 짧은 표기로 되돌린다(0.08 → 0.08) | 저장할 때 빠진 칸과 null 을 나누지 않았다. 선택 칸은 빼는 쪽이 어느 판에서도 계약에 맞다 |
| 한 tick | 같은 `observation_at` 의 행들. 나중 메시지로 일부 트랙이 덮였으면 가장 늦게 받은 행의 msg_id·판 정보를 쓴다 | -103 은 같은 시각을 나중 상태로 덮는다 |
| `step_ms` | 기본·최소 100(world_state 10 Hz), 최대 60000. 100 이면 솎지 않고 모든 tick, 그보다 크면 구간을 step_ms 칸으로 나눠 칸마다 가장 이른 tick 하나 | 요청서(-320). Tier 0 촬영 시각이 흔들려 100 ms 칸 하나에 tick 둘이 들 수 있어 기본값에서는 칸으로 솎지 않는다. 보간하지 않는다 |
| 한 번에 줄 양 | 구간 1시간, world 3000개(먼저 구간·step_ms 로 세고, 솎지 않을 때는 실제 tick 수로 다시 센다), 전이 5000개. 넘으면 400 `REPLAY_RANGE_TOO_LARGE` | 칸을 고르려면 구간의 트랙 행을 모두 읽으므로 step_ms 를 키워도 구간을 묶는다(10 Hz·10명 1시간 36만 행). world 3000개는 10 Hz 로 5분. 잘라 주면 화면이 빠진 부분을 기록 없음으로 오해하므로 거부한다. 더 긴 작업은 화면이 나눠 부른다 |
| 이벤트 후보 | 구간 전이는 (from, to] 에 전이가 있는 이벤트만, 시각 t 의 활성은 t 뒤에 전이가 있거나 지금도 끝나지 않은 이벤트만 창 함수에 넣는다 | 질의 비용이 쌓인 전이 이력 전체가 아니라 그 시각에 걸린 이벤트 수를 따르게 한다. t 에 활성이던 이벤트는 그 뒤에 끝났거나(전이가 있다) 아직 열려 있다 |
| `/api/world?at` | at 직전 1초 안의 마지막 tick 하나(Q1 의 1초 창). 없으면 `world_state: null` | 여러 tick 의 트랙을 섞어 한 메시지로 만들지 않는다. 1초 넘게 지난 상태를 끌어오지 않는다("모르면 비운다") |
| 시각 축 | world 는 `observation_at`(= t_latest_global), 이벤트는 전이의 `observation_at` | Q2·Q3 와 같은 촬영 기준 시각 |
| 이벤트 상태 칸 | 전이 행의 값에 `raised_at`(그때까지 첫 RAISED), `resume_count`(그때까지 resumed 수), `updated_at`(=occurred_at), RESOLVED 면 `resolved_at` 을 더한다. `rationale`·`proposed_severity` 는 null | `RiskEventRepository` 의 반영 규칙과 같다. VLM 설명은 전이 기록에 없다 |
| ACKNOWLEDGED | 상태 행으로 내지 않는다(창 안에는 둔다) | Q2: 생명주기를 바꾸지 않는다. 히스토리 화면은 확인을 하지 않는다(-320) |
| 기록 없음(`gaps`) | Tier 0 health(1 Hz)가 3초 넘게 없던 구간. 구간 처음·끝도 경계로 센다 | health 가 오면 Tier 0 가 돌고 있던 것이다. world 행은 트랙이 없으면 남지 않아(-103) 그것만으로는 빈 방과 기록 없음을 나눌 수 없다. 3초는 노트북 하트비트 3회 누락(FR-134)과 같은 기준 |
| 권한 | 로그인한 모든 역할. 토큰 없음 401 | 12.6 "열람" |
| 트랜잭션 | 읽기 전용·REPEATABLE READ 트랜잭션 하나에서 여러 질의를 읽는다 | 지금에 가까운 구간도 world·이벤트·빈 구간이 같은 시점의 기록이 되게 한다(READ COMMITTED 는 질의마다 스냅숏이 다르다) |

## 시험

`ReplayIntegrationTest`(실제 포트, world_state·health 는 실제 I1 으로 보냄):

- 구간의 world_state 가 보낸 payload 와 같다(필수 칸 null, 빠진 선택 칸, 골격 null 관절, float 소수). msg_id 는 원래 값, 판 2.1.0.
- step_ms 200 이면 칸마다 가장 이른 tick. 촬영 시각이 흔들려 100 ms 칸에 tick 둘이 들어도 기본 step 은 모두 주고, 계약 world_state 스키마를 통과한다.
- `/api/world?at` 는 직전 1초 안의 tick, 그 밖은 null.
- from 시각에 활성인 이벤트(구간 전에 RAISED), 구간 전에 끝난 이벤트는 빠짐, 재개(resumed) 이벤트의 resume_count·resolved_at 비움,
  ACKNOWLEDGED 는 빠짐, RESOLVED 의 raised_at·resolved_at, 이벤트 봉투가 계약 risk_event 스키마를 통과. 시각 t 의 활성 이벤트.
- health 가 3초 넘게 없던 구간, health 가 없으면 구간 전체.
- 401, 시각 없음·숫자 아님·음수·from ≥ to·step_ms 범위 밖은 `INVALID_REPLAY_QUERY`, 3000칸·1시간 경계와 넘으면 `REPLAY_RANGE_TOO_LARGE`.

## 남은 일

- 이벤트 스냅숏(`event_state_snapshot`)으로만 만든 이벤트(DELAYED_ACTIVE, 전이 기록 없이 들어온 것)는 `risk_event_update` 에 행이 없어 리플레이에 나오지 않는다.
- 받은 `schema_version` 을 저장하지 않아 값으로 고른다. 한 tick 이 1.x 메시지와 2.x 메시지의 트랙으로 섞이면(같은 시각을 다른 판의 메시지가 일부만 덮은 경우)
  어느 판으로도 계약에 맞지 않을 수 있다. Tier 0 는 한 판만 보내므로 실제로는 없다고 보고 두었다. 판마다 뜻이 갈리는 칸이 더 생기면 `world_track_state` 에 판을 저장한다.
- `initial_events`(시작 시각의 활성 이벤트)는 개수를 묶지 않았다. 동시에 열린 경보는 많아야 수십 개다.
- 이벤트 시각: 활성 판정은 `observation_at`, 응답의 `raised_at`·`updated_at` 은 Tier 0 의 `occurred_at` 이라 경계 근처에서 `updated_at` 이 from 보다 조금 늦을 수 있다.
- `gaps` 는 from 이전의 health 를 보지 않아 from 직전부터 이어진 빈 구간은 from 부터 센다.
- 3D 골격·Scene·구역의 그 시각 버전(Q4, 부록 C.1)은 확장이다.
- 관제 앱 히스토리 화면(-320)의 mock 을 이 형식에 맞추는 일은 프론트 몫이다. TBM 작업 목록(`/api/work-units`)은 별도 결정이다.
