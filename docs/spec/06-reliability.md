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
- Risk Engine의 시계는 영상이 모두 멈춰도 진행한다. 그래서 전체 중단 시 lag가 커진다.
- 동기화 오차가 큰 카메라는 융합에서 제외하거나 신뢰도를 낮춘다.
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

System Health의 lag 기준 초기값은 DEGRADED 600ms(6.6 capture_to_risk p95), UNSAFE_UNKNOWN 2000ms이고, 불확실성 확대에 쓰는 사람 수평 이동 속도 상한 초기값은 2.0 m/s다(교실 보행 1.2~1.5 m/s에 빠른 걸음 여유, 수정 6). 임계값(200·500ms, 지연 예산)은 실험용 초기값이다. 객체 유형(사람·장비), 카메라 주기, 구역에 따라 조정한다.

## 6.2 상태 계약

| 영역 | 상태 | 의미 |
|---|---|---|
| System Health | NOMINAL / DEGRADED / UNSAFE_UNKNOWN | 카메라, 시간 동기화, 보정, 게이트웨이·EC2 연결의 운영 신뢰도 |
| Object Observation | OBSERVED / PREDICTED / UNKNOWN | 객체의 관측 여부와 위치 신뢰도 |
| Region Geometry | VALID / CHANGING / UNKNOWN | 특정 공간의 정적 형상이 현재 유효한지 |
| Scene Candidate | CANDIDATE / VALIDATING / COMMITTED / REJECTED | 정적 3D 버전의 생명주기 |
| Calibration | UNCALIBRATED / PROVISIONAL / CALIBRATED / DRIFTED | 4.4 참조 |
| RiskEvent Lifecycle | RAISED / UPDATED / RESOLVED | 위험 이벤트의 진행 |
| RiskEvent Verification | VERIFIED / UNVERIFIABLE | 해소 여부를 관측으로 확인할 수 있는지 |
| Alert Channel | BOUND / UNBOUND / LOST | 개인 알림 채널 상태(9장) |
| PPE | WORN / NOT_WORN / UNKNOWN | 5.5 참조 |
| Config Sync | CURRENT / PENDING / UNVERIFIED / STALE | CURRENT 일치 확인, PENDING 새 묶음 전환 중, UNVERIFIED 연결 단절로 최신 여부 확인 불가, STALE 불일치가 확인됨(3.2). 어느 상태든 판정은 계속 |
| World Track | TENTATIVE / CONFIRMED / OCCLUDED / LOST (+ IDENTITY_UNCERTAIN 표시) | 추적 관리 생명주기(5.2). 위험 판단용 관측 상태와 별도 |

Region 상태는 Scene 상태와 이름이 겹치지 않게 VALID/CHANGING을 쓴다. 카메라 1대가 끊겨도 나머지가 관측하면 시스템 전체를 UNSAFE_UNKNOWN으로 만들지 않고, 그 카메라만 보던 구역의 감시 능력 저하를 표시한다(R7).

## 6.3 변경 구역과 임시 형상

정적 공간이 바뀐 구역은 3D 갱신을 기다리지 않고 보수적으로 판단한다.

**구현 범위.** MVP는 보정으로 확정한 바닥 평면과 사전에 등록한 검증된 정적 객체·위험 구역으로만 판단한다(수정 6). 새 물체의 변화 감지, Possible Occupancy Volume, Temporary Collider, UNKNOWN_OCCUPANCY, Scene 자동 갱신은 이 절에서 설계를 확정하되 구현은 확장 트랙(5.7, R10)이다. MVP에서 공간이 바뀌면 관리자가 정적 객체를 다시 등록하고 새 구성 묶음을 발행한다.

| 상황 | 동작 |
|---|---|
| 새 장애물 후보 | 아래 임시 형상 정책으로 즉시 반영 |
| 기존 장애물이 사라진 것처럼 보임 | 제거 검증 전까지 기존 Collider 유지 |
| 물체가 이동 중 | 정적 맵 대신 동적·준정적 객체로 추적 |
| 조명·그림자 변화 의심 | 물리 장애물로 즉시 확정하지 않음 |
| 변화는 감지했으나 형상 불명 | UNKNOWN_OCCUPANCY, 필요 시 구역 UNKNOWN |
| 새 형상 검증 완료 | Scene 버전 원자적 전환 |

**임시 형상 정책.** 한 번의 계산(팽창 실루엣 원뿔의 교집합 ∩ 방 형상. 방 형상은 BEV 평면도의 방 외곽을 바깥으로 넉넉히 넓혀 쓴다, 수정 6)을 기본으로 하고, 실패하면 제약을 완화해 다시 계산한다.

1. **Visual Hull 후보:** 캘리브레이션된 2대 이상에서 같은 물체를 안정적으로 볼 때, 마스크 오차와 재투영 오차 상한만큼 실루엣을 팽창시켜 교차한다. 오차가 그 여유 안에 있을 때만 물체를 포함한다고 본다(무조건 외접하지 않는다).
2. **Temporary Collider:** 관측 일관성 검사(2대 이상 완전 관측, 마스크 누락 없음, 재투영 잔차 기준 이내)를 통과하면 생성한다.
3. **UNKNOWN_OCCUPANCY:** 검사를 통과하지 못하면 실패한 Visual Hull을 그대로 쓰지 않는다. 잘린 부분은 이름을 바꿔도 돌아오지 않기 때문이다. 실루엣이 불완전하거나 가려진 카메라의 제약을 빼고(그 방향으로는 깎지 않음), 팽창 여유를 키워 다시 계산한 더 큰 볼륨을 불확실 점유 영역으로 둔다. 무한 기둥이 아니라 방 형상으로 제한된 Possible Occupancy Volume이다.

**TTL.** UNKNOWN_OCCUPANCY가 정해진 시간 안에 해소되지 않으면 재관측·재구성 우선순위를 올리고, 그래도 실패하면 구역을 UNKNOWN으로 유지한 채 운영자 확인을 요청한다. TTL은 불확실성을 없애는 타이머가 아니라 해결 절차를 격상하는 타이머다.

## 6.4 RiskEvent 생명주기

- RAISED → UPDATED(거리·TTC 변화) → RESOLVED. 해소는 유효 관측으로 위험 조건이 사라진 것을 일정 시간 확인했을 때만이다.
- 관측이 사라져 해소를 확인할 수 없으면 이벤트는 활성으로 남고 검증 상태만 UNVERIFIABLE이 된다. 자동 해소하지 않는다.
- ACKNOWLEDGED는 운영자가 확인했다는 속성이며 해소가 아니다. 반장의 “조치 완료”도 해소가 아니다(8.4).
- 발생·해소 임계값을 분리하고(히스테리시스), 해소는 일정 시간 유지 후 확정한다.
- 발생과 해소 시점 모두 근거(카메라, 좌표, 보정·Scene·규칙 버전)를 기록한다.

## 6.5 실패 대응

| 실패 | 감지 | 동작 |
|---|---|---|
| 카메라 1대 중단 | 프레임 수신 중단 | 해당 구역 감시 저하 표시(R7), 객체는 시간 모델대로 전환 |
| 시간 동기화 이탈 | 카메라 간 오프셋 증가 | 해당 카메라 융합 제외, DEGRADED |
| 캘리브레이션 틀어짐 | 재투영 오차 증가, 해상도 변경 | DRIFTED, 자동 재보정 |
| 노트북 → RunPod 업로드 단절·지연 | 하트비트, 프레임 도착 지연 | 판정 불가 구간은 UNSAFE_UNKNOWN, 노트북이 현장 “관제 중단” 5초 이내 |
| RunPod Tier 0 중단 | 노트북·EC2 측 하트비트 | 위치 추적·판정 정지, UNSAFE_UNKNOWN, 현장 “관제 중단”과 PWA·관리자 “관제 정지” 10초 이내 |
| RunPod Tier 0 → EC2 단절 | 하트비트, 노트북 → EC2 하트비트 | 판정·현장 경보 유지(노트북 표시 “원격 관제 장애”), 이벤트 outbox 저장 후 재동기화, PWA·관제 화면 전달은 지연. EC2는 노트북 하트비트로 Tier 0 정상을 확인하면 PWA·관리자에 “원격 전달 단절(현장 경보 동작 중)” 10초 이내 |
| 노트북 정지 | RunPod·EC2 측 하트비트 | 영상 입력 없음 → UNSAFE_UNKNOWN, PWA·관리자 “현장 장비 응답 없음” 10초 이내 |
| RunPod 분석 워커 중단 | 작업 시간 초과 | VLM 근거·보고서만 지연, 위치·경보는 영향 없음 |
| 워치 연결 끊김 | 하트비트 | 채널 LOST, 구역 경보로 대체 |

## 6.6 지연 예산

LIVE 판정이 RunPod Tier 0에 있으므로 지연을 세 구간으로 나눠 잰다. 노트북 → RunPod 업로드 구간이 포함되어 RunPod 데이터센터 위치가 지연을 크게 좌우한다(19.1). 구간별 p95의 합을 전체 p95로 해석하지 않고 각 구간을 끝에서 끝까지 잰다. 수치는 초기값이며 G0 실측 후 다시 정한다. 무거운 작업(정적 3D, VLM, SAM 3D Body)은 분석 워커 프로세스로 분리하고, 같은 GPU에서 Tier 0가 먼저 자원을 확보한다(3.2).

| 지표 | 구간 | 용도 | 초기 목표 |
|---|---|---|---|
| capture_to_risk | 촬영 → RunPod Tier 0 판정 | 실시간 판정 성능 | p95 600ms |
| capture_to_site_alert | 촬영 → 노트북 실제 소리·화면 | 현장 경보 성능 | p95 900ms |
| capture_to_remote_delivery | 촬영 → 관제 화면 / 반장 PWA | 원격 관제·알림 성능 | p95 1.2초 / 2.5초 |

이벤트 → 소리 구간만 재면 업로드·추론 지연을 놓치므로, capture_to_site_alert는 노트북이 붙인 촬영 시각부터 잰다.

지연 목표의 판정은 운영 리전·운영 GPU(일본 AP-JP-1 H100, 3.12)에서 잰 값으로 한다. 다른 GPU·리전 측정값은 병목 분석용이다.
