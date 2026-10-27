# EC2 시스템 지표 수집·조회

S15P31A101-70. 명세 3.11(구간별 지연·FPS·큐 길이·GPU 사용률·프레임 손실을 `system_metric` 에 기록, EC2 는 Actuator + Micrometer, 외부 비공개), 6.6(지연 목표), 11.2(`system_metric`), 12.6(`GET /api/metrics` 관리자), G0·G6 측정 근거.
코드는 `server/.../domain/metric/`(HealthMetrics·Ec2MetricSampler·SystemMetricRepository·MetricController).

## 흐름

1. Tier 0 가 I1 로 `health`(1 Hz)를 보내면 기존 수신(`HealthIngestor`)이 `system_health` 에 원문을 저장하고 `Tier0HealthStored` 를 낸다.
2. `HealthMetrics` 가 그 이벤트를 받아 전용 스레드(대기열 600)에서 수치를 지표 하나씩 `system_metric`(긴 형식)으로 푼다(I1 수신 스레드를 막지 않게). 시각은 봉투 `sent_at`(Tier 0 시계, `system_health` 와 같음), 출처는 봉투 `source_id`.
3. `Ec2MetricSampler` 가 10초마다 EC2 자체 지표(Micrometer)를 `source_id = ec2` 로 남긴다.
4. 관리자 화면·측정은 `GET /api/metrics` 로 계열(출처·지표·카메라)별 시간 순서 점을 읽는다. `latest=true` 면 계열마다 가장 늦은 한 점(상태 패널).

```
GET /api/metrics?metric=pipeline_lag_ms,fps&source_id=tier0-01&camera_id=cam_A&from=<ms>&to=<ms>&latest=false   (관리자)
→ 200 { "from", "to", "truncated", "series": [ { "source_id", "metric", "camera_id"(카메라 무관이면 null), "unit",
                                                 "points": [ { "t", "value", "labels"? } ] } ] }
→ 400 INVALID_METRIC_QUERY(from > to, 기간 24시간 초과, 숫자가 아니거나 범위 밖 시각)
```

## 지표

| 출처 | 지표(`metric`) | 카메라 | 단위 | 근거 칸 |
|---|---|---|---|---|
| Tier 0 | `pipeline_lag_ms`, `pipeline_lag_lower_bound_ms` | 없음 | ms | `health.pipeline_lag_ms`·`pipeline_lag_lower_bound_ms`(6.1) |
| Tier 0 | `capture_to_risk_ms` | 없음 | ms | `health.capture_to_risk_ms`(6.6). 값 p95, labels p50·p95·p99 |
| Tier 0 | `gpu_util`, `gpu_memory_used_ratio` | 없음 | ratio | `health.gpu_*` |
| Tier 0 | `ec2_world_state_drops_superseded`, `…_disconnected` | 없음 | count | `health.ec2_world_state_drops`(누적, labels `since_ms`) |
| Tier 0 | `laptop_clock_offset_ms` | 없음 | ms | `health.laptop_clock_offset.offset_ms`(labels 불확실도·왕복·표본 수) |
| Tier 0 | `fps`, `sync_offset_ms`, `timestamp_uncertainty_ms`, `max_result_gap_ms` | 있음 | fps·ms | `health.cameras[]` |
| Tier 0 | `frame_age_ms`, `queue_wait_ms` | 있음 | ms | `health.cameras[]`(5.1). 값 p95, labels 백분위 |
| Tier 0 | `frames_<칸>`(received·gateway_to_runpod_loss·skipped_by_policy·laptop_skipped·unattributed_gap·late_dropped), `camera_to_gateway_gap_max_ms` | 있음 | count·ms | `health.cameras[].frames`(보고 구간 집계) |
| EC2 | `ec2_process_cpu_usage`, `ec2_jvm_heap_used_bytes`, `ec2_jvm_heap_used_ratio`, `ec2_db_connections_active`, `ec2_i1_messages_total` | 없음 | ratio·bytes·count | Micrometer `process.cpu.usage`·`jvm.memory.*`(heap)·`hikaricp.connections.active`·`simai.i1.messages` |

## 명세가 비워 둔 곳에서 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 형식 | 긴 형식(한 행에 지표 하나), 카메라 무관이면 `camera_id` 빈 문자열(응답은 null) | V1 `system_metric` 키(measured_at, source_id, metric, camera_id). 키에 NULL 을 쓰지 않는다 |
| 백분위 | 값은 p95, labels 에 p50·p95·p99 | 지연 목표는 p95(6.6). db-tables "labels: 백분위 등" |
| null | 잴 수 없음(null)·없는 칸은 쓰지 않는다. 0 은 쓴다(받지 못한 카메라 FPS 0) | 잴 수 없음과 0 을 섞지 않게 |
| 재전송 | 같은 키는 다시 쓰지 않는다(`ON CONFLICT DO NOTHING`) | health 재전송은 같은 sent_at 이라 같은 측정 |
| 실패·지연 | 지표 저장은 전용 스레드 하나(대기열 600 = 10분어치)에서 하고, 실패하거나 대기열이 차면 그 지표만 버린다(경고) | 지표는 관측 보조이고 health 원문(`system_health`)이 정본. 수신 스레드에서 동기로 쓰면 같은 연결의 world_state(LIVE)가 기다린다(코드 리뷰 반영) |
| EC2 지표 | 10초 주기(`simai.metrics.ec2-interval`), 없는 계측·모름(음수) 값은 건너뜀 | Actuator 엔드포인트는 외부에 열지 않는다(14장, Nginx 는 `/api` 만). 관제는 이 기록으로 본다 |
| 조회 | 기간 기본 최근 10분(`latest` 는 1시간)·최대 24시간, 행 상한 50,000. 넘으면 늦은 점부터 상한까지(`truncated`, 계열은 남고 오래된 점이 빠짐). 빈 필터는 필터 없음, 관리자만 | 12.6 권한 관리자. 카메라 4대면 health 하나에 지표 59개(전체 7 + 카메라당 13), 분당 약 3,540행이라 10분 ≈ 35,400행. 24시간 전체는 구간을 나눠 받는다. `latest` 1시간은 잠깐 끊긴 출처도 마지막 값과 그 시각을 남긴다 |
| 마이그레이션 | 없음(V14 는 쓰지 않음) | V1 의 hypertable·압축·30일 보관 정책 그대로. 시간 범위 조회는 키 첫 칸이 시각이라 그대로 쓴다 |

## 시험

`SystemMetricIntegrationTest`(실제 포트·I1 WebSocket): 계약 예시 health 두 개 → 지표 21개씩(값·백분위 labels·null 은 건너뜀·FPS 0 은 씀) → 관리자 조회(계열 순서·카메라 null·단위·점 2개)·`latest`(계열마다 늦은 한 점) / 같은 health 재전송은 두 번 쓰지 않음 /
열람·반장 403·토큰 없음 401·기간 400·기본 10분 / EC2 지표가 Micrometer 에서 읽힘.

## 남은 일

- 노트북 지표: `laptop_heartbeat`(device_heartbeat, -262)의 수치도 같은 방식으로 풀 수 있다(명세 3.11 "노트북은 health·laptop_heartbeat").
- 관제 상태 패널(front)이 `latest=true` 로 붙는다.
- G0·G6 측정 스크립트가 이 API 로 구간을 받아 p95 를 낸다.
- `latest` 를 긴 기간으로 부르면 기간 안 모든 행을 정렬한다. 상태 패널이 자주 부르면 계열별 색인(source_id, metric, camera_id, measured_at DESC)을 더한다.
