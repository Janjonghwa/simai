# 11. 데이터

Spatial DB는 EC2의 PostgreSQL + TimescaleDB(시계열) + PostGIS(공간)로 구성한다. 영상·사진·3D 자산은 객체 저장소에 두고 DB에는 주소만 저장한다.

## 11.1 구성·기준 데이터

자주 바뀌지 않으며, 수정 대신 버전을 추가한다.

| 테이블 | 주요 내용 |
|---|---|
| `site` | 평면도 참조, World 좌표계 정의, 좌표계 이전 이력 |
| `camera` | 카메라 ID, 모델, 하드웨어·펌웨어 버전, 해상도·fps |
| `calibration_version` | 카메라별 K·왜곡·R·t·높이, 재투영 오차, 스케일 근거, 상태 |
| `scene_version` | 정적 3D 자산 주소, 사용한 보정 버전, 상태 |
| `zone` | 다각형 + 높이 범위, 판정 유형(GROUND_FOOTPRINT/VOLUME_3D), 유형, 심각도, 요구 조건, 유효 시작·종료 |
| `rule_version` | 규칙 ID, 임계값 세트, 적용 기간 |
| `equipment_asset` | GLB 주소, 반경·형상 파라미터 |
| `layout_plan` | 가상 레이아웃·계획 궤적(②·①), 출처 PLANNED |
| config_bundle | 구역·규칙·보정·형상 버전 묶음, 해시, 발행 시각, 현장 적용 시각(RunPod Tier 0 ACK) |

## 11.2 시계열 관측

| 테이블 | 주요 내용 | 빈도 | 보관 |
|---|---|---|---|
| `observation_2d` | 촬영 시각, 카메라, 카메라 내 트랙, bbox, keypoint 배열 | 저장 5\~15 Hz(처리는 카메라당 12 FPS 이상, 5.1) | 7\~14일(보정 재계산용) |
| `world_track_state` | World ID, XYZ, 불확실성, 속도, 관측 상태, gap, 기여 카메라, 보정·Scene 버전 | 10 Hz | 장기(리플레이 본체) |
| `skeleton_3d` | World ID, 3D 관절, 관절 가시성, 계산 방식 | 5\~10 Hz | 중기 |
| `system_health` | 시스템 상태, 카메라별 상태, lag, 보정 상태, 하트비트 | 1 Hz | 장기 |
| `ppe_state` | 안전모·조끼 상태, 신뢰도, 마지막 관측 | 1\~2 Hz | 장기(확장) |
| `channel_state` | BOUND/UNBOUND/LOST, 워치 ID(가명) | 상태 변화 시 | 장기(확장) |
| `watch_feature` | 걸음 특징 | 1\~5 Hz | 단기(확장) |
| region_state | 구역 ID, VALID/CHANGING/UNKNOWN, 근거, Scene 버전 | 상태 변화 시 | 장기(확장) |
| system_metric | 구간별 지연, FPS, 큐 길이, GPU 사용률, 프레임 손실 | 1\~5 Hz | 중기 |

## 11.3 이벤트·조치

| 테이블 | 주요 내용 |
|---|---|
| `risk_event` | 규칙·버전, World ID·구역, 출처, 생명주기·검증 상태, severity·confidence, 발생·해소 시각, 근거 스냅샷, 보존 표시 |
| `risk_event_update` | RunPod Tier 0 전이의 시간순 기록(event_sequence, config_version, 지연 전달·감사 전용 여부) |
| `vlm_assessment` | 이벤트, 모델·버전, 입력 참조, 근거 문장, 인용 조항 ID, 상향 의견(proposed_severity), 지연 |
| `notification` | 이벤트, 채널, 대상, 발송·ACK 시각 (LIVE 이벤트만 연결 가능) |
| `action_ticket` | 조치 상태, 사진 주소, 해소 상태와의 불일치 표시 |
| `watch_binding` | World ID, 워치 ID, 신뢰도, 시간 구간 |
| `media_clip` | 이벤트 연결 영상 구간 주소, 삭제 예정일, 보존 표시 |
| `report` | TBM 보고서·일일 일지, 입력 참조, 인용 목록 |

PLANNED·WHAT_IF 결과는 별도 스키마 `sim.*`에 저장한다.

## 11.4 스키마 원칙

1. 시계열·이벤트는 추가만 한다. 구역·규칙·보정·Scene은 유효 기간이 있는 새 버전을 추가한다.
2. 모든 계산 결과는 출처를 안다: 위치(보정·Scene 버전), 이벤트(규칙 버전), 근거(모델 버전).
3. `notification`은 출처가 LIVE인 `risk_event`만 참조할 수 있게 한다. 구현: notification에 source 컬럼을 두고 risk_event(event_id, source)를 복합 FK로 참조하며 source = ‘LIVE’ 체크를 건다(일반 CHECK는 다른 테이블을 볼 수 없으므로). 발송 워커도 LIVE만 소비하고, DB 기록 차단과 실제 발송 차단을 각각 테스트한다.
4. 촬영·수신·판정 시각을 모두 UTC로 저장한다.
5. 위치 기록에는 이름이 없다. 워치 등록자 이름은 접근 제한된 별도 테이블에만 둔다.

**용량.** 10명 기준 `world_track_state`는 하루 8시간에 약 300만 행(압축 전 수백 MB). 가장 무거운 `observation_2d`는 하루 수 GB까지 가능하므로 보관을 짧게 두고 필요하면 5 Hz로 낮춘다. EC2 디스크 용량은 미결 사항이다.

**리플레이 질의.** 시각 T의 `world_track_state`·`skeleton_3d`, T에 유효한 `zone`·`rule_version`, T에 COMMITTED인 `scene_version`, T에 활성인 `risk_event`.

## 11.5 보관과 개인정보

| 데이터 | 보관 |
|---|---|
| 원본 영상 | 관제 중 RunPod로 상시 전송(TLS), 저장하지 않음(링 버퍼와 이벤트 클립만 예외) |
| 이벤트 영상 클립 | 7\~30일 후 자동 삭제, 사고·아차사고 연결분은 보존 표시 + 접근 기록 |
| PPE crop | 판정 직후 삭제, 저장 없음 |
| 2D 관측 | 7\~14일 |
| 좌표·상태·이벤트 로그 | 장기(얼굴 없음, 리플레이 유지) |
| 워치 특징 | 단기 |
| 시나리오 데이터셋 | 참여자 별도 동의, 보관 기간 명시 |
| HMR 외형/텍스처·추론 결과(실험) | 별도 참여자 동의, 접근 범위·저장 위치·보관/삭제 기간을 실험 시작 전 확정. 실제 얼굴 재식별 정보 생성 금지 |

- 자동 삭제는 객체 저장소 수명 주기 규칙과 TimescaleDB 보관 정책으로 구현한다.
- 참여자 동의서에 촬영 사실, 외부 클라우드(AWS 서울 리전, RunPod 일본 리전(개발·시연, 3.12. 일본 GPU를 못 구하면 미국 리전으로 바뀔 수 있음)) 전송 범위(관제 중 카메라 영상 상시 전송 포함), 보관 기간, 삭제 요청 방법을 명시한다.
- 전송 구간은 모두 암호화한다(14장).
