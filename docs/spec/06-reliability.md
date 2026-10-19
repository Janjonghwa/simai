# 6. 신뢰성 명세

시스템은 시간·상태·형상의 불확실성을 명시적으로 표현하고, 확인할 수 없는 것을 안전하다고 판단하지 않는다. 아래 정의는 모델 선택과 무관하게 고정한다.

## 6.1 시간 모델

모든 시각은 동기화된 공통 기준(UTC ms)으로 비교한다.

```javascript
T_gap       = t_latest_global - t_last_valid_object
T_lag       = t_risk_now      - t_latest_global
T_staleness = T_gap + T_lag
```

- `latest_global`: 시간 동기화가 유효한 카메라들 중 실제 처리된 최신 촬영 시각. 카메라별 기준이 아니다(카메라 A가 멈춰도 B·C·D의 시계로 gap이 커져야 하기 때문).
- 촬영 시각은 노트북 시계로 붙고 `t_risk_now`는 Tier 0 시계이므로, T_lag 는 노트북–Pod 시계 차이 추정값(3.9)으로 촬영 시각을 Tier 0 시계로 옮겨 계산하고, 그 불확실성을 함께 쓴다. 옮긴 촬영 시각 = 노트북 촬영 시각 − `health.laptop_clock_offset.offset_ms`(노트북 − Tier 0)이고, 불확실성은 촬영 시각 불확실성(`timestamp_uncertainty_ms`)에 `laptop_clock_offset.uncertainty_ms` 를 더한다. `health.pipeline_lag_ms` 는 이렇게 보정한 T_lag 다.
- Tier 0 시계는 단조 시계로 잰다(벽시계가 뛰거나 거꾸로 가도 시계 차이 표본과 왕복을 버리지 않는다). Risk Engine 판정 시각은 뒤로 가지 않는다: 벽시계 기준점을 뒤로 다시 잡으면 직전 값에 고정하고, 그동안 lag 는 보수적으로 크게 나온다.
- 시계 차이를 쓸 수 있는 동안: 노트북 제어 채널이 살아 있고 교환만 늦으면 마지막 표본 뒤 60초(초기값)까지 마지막 추정을 쓰고 불확실성을 표류(100ppm × 경과)만큼 키운다(3.9). 시계 차이 불확실성이 100ms(초기값)를 넘으면 DEGRADED 다(6.5 시간 동기화 이탈).
- **시계 차이를 모를 때**(표본 없음, 유지 시간 초과, 제어 채널 끊김. `laptop_clock_offset` = null): 0 으로 채워 보정하지 않는다. 판정 시각 기준 지연을 알 수 없으므로 System Health 는 UNSAFE_UNKNOWN 이다(노트북 ‘관제 중단’ 경로). 보정한 lag 가 불확실성 밖으로 음수가 되는 경우(마지막 표본 뒤 노트북 시계 점프 등)도 같이 다룬다.
  - `health.pipeline_lag_ms` 는 null 이다(처리한 유효 프레임이 없어 `t_latest_global` 이 없을 때도). 0 이나 하한을 T_lag 처럼 싣지 않는다. 처리 진행이 멈춘 뒤 Tier 0 단조 시계로 잰 경과(참 T_lag 의 하한)를 알면 `pipeline_lag_lower_bound_ms` 로 따로 싣는다.
  - 객체 관측 상태는 막지 않는다: OBSERVED·PREDICTED·UNKNOWN 은 gap(촬영 시각끼리, 시계 차이와 무관)으로 그대로 정하고, 위치·불확실성은 `t_latest_global` 시점까지만 예측한다(현재로 옮기지 않음). 새 위험 판정도 계속한다.
  - 현재 시각이 필요한 판단 중 해소 확인은 막는다: 그동안 활성 이벤트는 UNVERIFIABLE 로 두고 해소를 확정하지 않는다(6.4). 시간 대응이 복구되면 유효 관측으로 해소 유지 시간을 처음부터 다시 채운다.
- **시계 점프 뒤 해소.** 해소 유지 시간은 노트북 시계의 촬영 시각으로 잰다. 교환이 노트북 시계 점프를 감지했거나 Tier 0 시계 기준점을 다시 잡은 뒤에는 각 활성 이벤트의 해소 유지 시간을 처음부터 다시 잰다(점프 폭만큼 유지 시간을 건너뛰지 않게).
- System Health 의 lag 판정은 보정한 lag 의 상한(lag + 불확실성)과, 처리 진행이 멈춘 뒤 Tier 0 단조 시계로 잰 경과 중 큰 값으로 한다.
- Risk Engine의 시계는 영상이 모두 멈춰도 진행한다. 그래서 전체 중단 시 lag가 커진다.
- 동기화 오차가 큰 카메라는 융합에서 제외하거나 신뢰도를 낮춘다. `health.cameras[].sync_valid` 는 동기화 측정(`sync_offset_ms`)으로 확인했을 때만 true, 무효로 판정했으면 false, 판정할 근거가 없으면 null 이다(근거 없이 true 로 채우지 않음).
- 지연 프레임과 역순 도착을 처리한다. 예측 위치를 관측처럼 취급하거나 시각을 갱신해 유효기간을 늘리지 않는다.
- OBSERVED도 최신이라는 뜻이 아니다. Risk Engine은 항상 staleness로 현재 위치를 예측하거나 불확실성을 키운다.

| observation_gap | 객체 관측 상태 | 동작 |
|---|---|---|
| 0\~200ms | OBSERVED | 최신 관측 사용 |
| 200\~500ms | PREDICTED | 단기 예측, 불확실성 확대 |
| 500ms 초과 | UNKNOWN | 정확한 위치 사용 중단 |
| 사례 | 객체 상태 | System Health |
|---|---|---|
| 사람 정상 검출, 처리만 느림 | OBSERVED | lag 기준 초과 시 DEGRADED |
| 영상 정상, 사람이 300ms 미검출 | PREDICTED | NOMINAL 가능 |
| 카메라 통신 중단 | 예측 후 UNKNOWN | 커버리지에 따라 DEGRADED 또는 UNSAFE_UNKNOWN |

System Health의 lag 기준 초기값은 DEGRADED 600ms(6.6 capture_to_risk p95), UNSAFE_UNKNOWN 2000ms이고, 불확실성 확대에 쓰는 사람 수평 이동 속도 상한 초기값은 2.0 m/s다(교실 보행 1.2~1.5 m/s에 빠른 걸음 여유). 임계값(200·500ms, 지연 예산)은 실험용 초기값이다. 객체 유형(사람·장비), 카메라 주기, 구역에 따라 조정한다.

## 6.2 상태 계약

| 영역 | 상태 | 의미 |
|---|---|---|
| System Health | NOMINAL / DEGRADED / UNSAFE_UNKNOWN | 카메라, 시간 동기화, 보정, 게이트웨이·EC2 연결의 운영 신뢰도 |
| Object Observation | OBSERVED / PREDICTED / UNKNOWN | 객체의 관측 여부와 위치 신뢰도 |
| Region Geometry | VALID / CHANGING / UNKNOWN | 특정 공간의 정적 형상이 현재 유효한지(변화 감지는 확장, 부록 C) |
| Scene Candidate | CANDIDATE / VALIDATING / COMMITTED / REJECTED | 정적 3D 버전의 생명주기 |
| Calibration | UNCALIBRATED / PROVISIONAL / CALIBRATED / DRIFTED | 4.4 참조 |
| RiskEvent Lifecycle | RAISED / UPDATED / RESOLVED | 위험 이벤트의 진행 |
| RiskEvent Verification | VERIFIED / UNVERIFIABLE | 해소 여부를 관측으로 확인할 수 있는지 |
| Alert Channel | BOUND / UNBOUND / LOST | 개인 알림 채널 상태(부록 C, 워치) |
| PPE | WORN / NOT_WORN / UNKNOWN | 5.5 참조(확장) |
| Config Sync | CURRENT / PENDING / UNVERIFIED / STALE | CURRENT: EC2 가 마지막으로 알린 묶음과 활성 묶음이 같음이 확인됨. PENDING: 알린 더 새 묶음을 검증·전환 중. UNVERIFIED: 연결 단절 또는 (재)연결 뒤 알림을 아직 못 받아 최신 여부 확인 불가. STALE: EC2 가 알린 묶음과 다르다는 것이 확인됨(경과 시간 기준이 아님, 거부한 묶음 포함, 3.2). 어느 상태든 판정은 계속 |
| World Track | TENTATIVE / CONFIRMED / OCCLUDED / LOST (+ IDENTITY_UNCERTAIN 표시) | 추적 관리 생명주기(5.2). 위험 판단용 관측 상태와 별도 |

Region 상태는 Scene 상태와 이름이 겹치지 않게 VALID/CHANGING을 쓴다. 카메라 1대가 끊겨도 나머지가 관측하면 시스템 전체를 UNSAFE_UNKNOWN으로 만들지 않고, 그 카메라만 보던 구역의 감시 능력 저하를 표시한다(R7).

## 6.3 변경 구역과 임시 형상

MVP는 보정으로 확정한 바닥 평면과 사전에 등록한 정적 객체·작업면·위험 구역으로만 판단하고, 공간이 바뀌면 관리자가 다시 등록해 새 구성 묶음을 발행한다(7.5). 변화 감지와 임시 형상 정책(Visual Hull·Temporary Collider·UNKNOWN_OCCUPANCY·TTL)은 이동(v1.3) → 부록 C.

## 6.4 RiskEvent 생명주기

- RAISED → UPDATED(거리·TTC 변화) → RESOLVED. 해소는 유효 관측으로 위험 조건이 사라진 것을 일정 시간 확인했을 때만이다.
- **발생 지속 조건.** 순간 오검출로 이벤트가 생기지 않도록 발생에 짧은 지속 조건을 둔다. 초기값은 10Hz 기준 최근 3프레임 중 2프레임에서 조건이 성립할 때이며, 이로 늘어나는 지연(+100\~200ms)은 G6 진입 → 이벤트 900ms 예산 안이다.
- **이벤트 재개.** 같은 사람·같은 구역(규칙)이 해소 후 10초 안에 다시 조건을 만족하면 새 이벤트를 만들지 않고 기존 이벤트를 재개한다. 재개는 같은 `event_id` 의 UPDATED 전이(`resumed` = true)로 보내고 `event_sequence` 를 이어 올린다. 재개는 새 위험 발생처럼 다룬다: 이전 확인은 기록으로 남지만 다시 확인하기 전까지 미확인이고, CRITICAL·HIGH 면 다시 울린다(3.10).
- 관측이 사라져 해소를 확인할 수 없으면 이벤트는 활성으로 남고 검증 상태만 UNVERIFIABLE이 된다. 자동 해소하지 않는다.
- ACKNOWLEDGED는 운영자가 확인했다는 속성이며 해소가 아니다. 반장의 “조치 완료”도 해소가 아니다(8.4).
- 발생·해소 임계값을 분리하고(히스테리시스), 해소는 일정 시간 유지 후 확정한다.
- 발생과 해소 시점 모두 근거(카메라, 좌표, 보정·Scene·규칙 버전)를 기록한다.

## 6.5 실패 대응

| 실패 | 감지 | 동작 |
|---|---|---|
| 카메라 1대 중단 | 프레임 수신 중단 | 해당 구역 감시 저하 표시(R7, 관제 화면 안내), 객체는 시간 모델대로 전환 |
| 시간 동기화 이탈 | 카메라 간 오프셋 증가, 노트북–Pod 시계 차이 불확실성 증가(100ms 초과) | 해당 카메라 융합 제외, DEGRADED |
| 노트북–Pod 시계 차이 모름 | 교환 표본 없음·유지 시간(60초) 초과·제어 채널 끊김, 보정 lag 가 불확실성 밖 음수 | UNSAFE_UNKNOWN, 관측 상태는 gap 기준 유지, 해소 확정 금지(6.1) |
| 캘리브레이션 틀어짐 | 재투영 오차 증가, 해상도 변경 | DRIFTED, 자동 재보정(원인 카메라를 가렸고 해상도·화각이 그대로일 때만). 사람이 없으면 ‘감시 근거 없음’으로 상태 유지(4.4) |
| 노트북 → RunPod 업로드 단절·지연 | 하트비트, 프레임 도착 지연 | 판정 불가 구간은 UNSAFE_UNKNOWN, 노트북이 현장 “관제 중단” 5초 이내 |
| RunPod Tier 0 중단 | 노트북·EC2 측 하트비트, `t_latest_global` 정체 | 위치 추적·판정 정지, UNSAFE_UNKNOWN, 현장 “관제 중단” 5초 이내, PWA·관리자 “관제 정지” 10초 이내 |
| RunPod Tier 0 → EC2 단절 | 하트비트, 노트북 → EC2 하트비트 | 판정·현장 경보 유지(노트북 표시 “원격 관제 장애”), 이벤트 outbox 저장 후 재동기화, PWA·관제 화면 전달은 지연. EC2는 노트북 하트비트로 Tier 0 정상을 확인하면 PWA·관리자에 “원격 전달 단절(현장 경보 동작 중)” 10초 이내 |
| 노트북 정지 | RunPod·EC2 측 하트비트 | 영상 입력 없음 → UNSAFE_UNKNOWN, PWA·관리자 “현장 장비 응답 없음” 10초 이내 |
| EC2 정지 | PWA·뷰어의 STOMP 하트비트, 노트북 → EC2 하트비트 실패 | 판정·현장 경보 유지(노트북 “원격 관제 장애”), PWA·뷰어에 연결 끊김 표시. 그 구간의 world_state 는 리플레이에서 “데이터 없음”으로 표시하고 보간하지 않음 |
| RunPod 분석 워커 중단 | 작업 시간 초과 | VLM 근거·보고서만 지연, 위치·경보는 영향 없음 |
| Tier 0 내부 기록 장애 | 체크포인트 정체·쓰기 오류, 저널 쓰기 정체·실패, EC2 전송 보류(outbox), 재동기화 스냅샷 접기 중단(Tier 0 정리 주기 1초에 각 모듈 상태를 읽음) | 판정·현장 경보는 계속(저널 쓰기 실패면 EC2 로 전이가 나가지 않고 재시작으로 복구). `health.tier0_warnings` 에 경고 code 와 처음 확인한 시각을 실어 관제 화면에 알림. 원인이 풀리면 다음 health 에서 빠지고, 상태를 읽지 못하면 필드를 싣지 않음(경고 없음과 구분) |

**하트비트 판정 규칙.** 모든 하트비트는 받는 쪽의 단조 시계로 재고, 1초 주기 3회 연속 누락을 장애로 본다. Tier 0 정상은 처리 진행(`t_latest_global` 증가)으로 판정하며 소켓이 열려 있다는 것만으로 정상으로 보지 않는다. 반쯤 열린 TCP 연결은 애플리케이션 하트비트 누락으로 끊고 다시 연결한다(3.2).

## 6.6 지연 예산

LIVE 판정이 RunPod Tier 0에 있으므로 지연을 세 구간으로 나눠 잰다. 노트북 → RunPod 업로드 구간이 포함되어 RunPod 데이터센터 위치가 지연을 크게 좌우한다(3.12, 일본 AP-JP-1). 구간별 p95의 합을 전체 p95로 해석하지 않고 각 구간을 끝에서 끝까지 잰다. 수치는 초기값이며 G0 실측 후 다시 정한다. 무거운 작업(정적 3D, VLM, SAM 3D Body)은 분석 워커 프로세스로 분리하고, 같은 GPU에서 Tier 0가 먼저 자원을 확보한다(3.2).

| 지표 | 구간 | 용도 | 초기 목표 |
|---|---|---|---|
| capture_to_risk | 촬영 → RunPod Tier 0 판정 | 실시간 판정 성능 | p95 600ms |
| capture_to_site_alert | 촬영 → 노트북 실제 소리·화면 | 현장 경보 성능 | p95 900ms |
| capture_to_remote_delivery | 촬영 → 관제 화면 / 반장 PWA | 원격 관제·알림 성능 | p95 1.2초 / 2.5초 |

이벤트 → 소리 구간만 재면 업로드·추론 지연을 놓치므로, capture_to_site_alert는 카메라가 보는 실제 사건부터 잰다(glass-to-alarm, 15.1 측정 방법). 운영 중 상시 지표는 노트북이 붙인 촬영 시각부터 잰다.

지연 목표의 판정은 운영 리전·운영 GPU(일본 AP-JP-1 H100, 3.12)에서 잰 값으로 한다. 다른 GPU·리전 측정값은 병목 분석용이다.

**단계 목표.** 끝에서 끝까지 목표를 지키기 위한 Tier 0 내부 단계 목표다. 판정은 끝에서 끝까지 지표로 하고 단계 목표는 병목 관리에 쓴다.

| 단계 | 내용 | 초기 목표 |
|---|---|---|
| Tier 0 기하 단계 | World Track 갱신 한 번의 왜곡 제거·카메라 간 연결·삼각측량·관절 게이트(CPU, 10 Hz) | 10명 p95 50ms, 5명 p95 25ms |

기하 단계 목표는 운영 GPU 노드의 CPU 에서 잰 값으로 판정한다(측정 방법은 `docs/design/tier0-geometry-budget.md`).
