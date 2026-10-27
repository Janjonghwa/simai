# simAI DB 테이블 정의

S15P31A101-210. 관계 그림은 [`db-erd.md`](db-erd.md), 검증은 [`db-verification.md`](db-verification.md) 에 있다. ERD 1단계(테이블 목록)·2단계(계약 → 컬럼)의 결과다.

- **기준:** 기획서 v1.3 수정 19 11장(데이터)·부록 C, 계약 v5.3.0, API 명세 표 v11(노션, 166행)
- **범위:** 테이블 70개, 컬럼 743개. 확장 테이블도 컬럼까지 썼다
- **단계별:** MVP 34 · M1 9 · M2 2 · M3 9 · 확장 16 (M3 `rag_query_template` 은 S15P31A101-27 에서 추가)
- **작성·검토:** DB 는 EC2 에 있으므로 곽나혁이 작성한다. Tier 0·분석 워커가 채우는 테이블은 이연주가 검토한다("검토" 표시)
- **마이그레이션:** `server/src/main/resources/db/migration/` 의 V1(MVP·M1 테이블 43개), V2(현장·카메라 시드), V3(`rag_clause`·`rag_query_template`, pgvector), `R__rule_versions.sql`(ai 규칙 등록부에서 자동 생성), `R__rag_clauses.sql`(ai RAG 조항·임베딩에서 자동 생성)

## 1. 표기와 공통 규칙

| 항목 | 규칙 |
|---|---|
| 시각 | `ts` = `timestamptz`(UTC). 계약의 UTC int64 ms 시각은 EC2 수신 경계에서 변환한다. 이름의 `_ms` 는 떼고 `_at` 을 붙인다(예: `anchor_capture_ms` → `anchor_capture_at`). 길이(`*_ms` duration)는 `int` 그대로 |
| 열거값 | PostgreSQL enum 대신 `text` + CHECK(Flyway 로 값 추가가 쉬움). 값은 계약 enum 과 같게 쓴다 |
| 키 | 계약이 정한 ID(`event_id` UUIDv7, `clip_id`, `job_id`, `*_version` 문자열)는 그대로 PK. EC2 가 만드는 행은 `bigint identity` |
| 버전 테이블 | `zone`·`static_object`·`rule_version`·`calibration_version`·`scene_version` 은 수정하지 않고 새 버전을 추가하고, 리플레이용 `valid_from`·`valid_to` 를 둔다(11.4-1) |
| hypertable | `world_track_state`·`skeleton_3d`·`system_health`·`system_metric`·`device_heartbeat`(확장: `channel_state`·`watch_feature`·`ppe_state`). 다른 테이블이 hypertable 을 FK 로 참조하지 않는다. 버전 ID 는 논리 참조만 둔다 |
| 공간 | PostGIS `geometry(…, 0)`: World 로컬 미터 좌표라 SRID 0. 타입의 SRID 0 은 PostGIS 에서 "제한 없음"이라 컬럼마다 `CHECK (ST_SRID(컬럼) = 0)` 을 함께 건다(V1 점검에서 SRID 4326 이 들어가는 것을 확인). 저장·조회만 하고 판정은 Risk Engine(11장) |
| 묶음 값 | 계약에서 구조가 자주 바뀌는 값(`metrics`, 카메라별 health, `components`)은 `jsonb` |
| 이름 | 사람 이름은 위치·이벤트 기록에 넣지 않는다. 계정은 `user_id`, 기기는 `device_id`(11.4-5). 예외는 접근 제한된 `watch_registrant` 뿐(C.6) |
| 키·제약 표기 | `PK`, `PK(n)` 복합 키 순서, `FK>표.컬럼`, `UQ`, `UQ(이름)` 묶음 유일, `NN` not null, `CK(…)` 허용값 |

## 2. 테이블 목록

| 묶음 | 테이블 | 단계 | 종류 | 설명 | 11장 | 쓰는 API·토픽 | 검토 |
|---|---|---|---|---|---|---|---|
| A. 계정·기기·인증 | `app_user` | MVP | 일반 | 관제 화면·PWA 계정. 역할 3종 | 수정 16 추가 | `/api/auth/login` |  |
| A. 계정·기기·인증 | `refresh_token` | MVP | 일반 | JWT 갱신 토큰 | 수정 16 추가 | `/api/auth/login` |  |
| A. 계정·기기·인증 | `device` | MVP | 일반 | 현장 노트북(확장: 워치) 기기와 토큰 | 수정 16 추가 | `/api/devices/heartbeat` |  |
| A. 계정·기기·인증 | `device_heartbeat` | MVP | hypertable | 노트북 → EC2 1초 하트비트 기록. remote_state 판단 근거 | 수정 16 추가 | `/api/devices/heartbeat`, `/topic/health` |  |
| A. 계정·기기·인증 | `worker` | MVP | 일반 | 분석 워커와 토큰 | 수정 16 추가 | `/api/worker/jobs/lease` | 이연주 |
| B. 구성·기준 데이터 | `site` | MVP | 일반 | 현장과 World 좌표계 정의 | 있음 | 없음(다른 테이블이 참조하거나 I1·I5 수신으로 채움) |  |
| B. 구성·기준 데이터 | `camera` | MVP | 일반 | 카메라 장비 정보 | 있음 | 없음(다른 테이블이 참조하거나 I1·I5 수신으로 채움) |  |
| B. 구성·기준 데이터 | `zone` | MVP | 버전 | 위험 구역. 수정은 새 버전 | 있음 | `/api/zones`, `/api/zones/{}/versions` |  |
| B. 구성·기준 데이터 | `static_object` | MVP | 버전 | 등록 작업면·정적 객체(Operational Geometry) | 수정 16 추가 | `/api/static-objects` |  |
| B. 구성·기준 데이터 | `rule_version` | MVP | 버전 | Risk Engine 규칙 임계값 세트. 규칙 등록부에서 자동 생성한 Flyway 시드로 채운다 | 있음 | `/api/rules` | 이연주 |
| B. 구성·기준 데이터 | `config_bundle` | MVP | 버전 | 구성 묶음 발행과 현장 적용 | 있음 | `/api/config-bundles`, `/api/config-bundles/{}` | 이연주 |
| B. 구성·기준 데이터 | `config_bundle_item` | MVP | 일반 | 묶음에 들어간 구역·규칙·형상 버전 목록 | 수정 16 추가 | `/api/config-bundles` |  |
| B. 구성·기준 데이터 | `config_ack` | MVP | 일반 | Tier 0 의 구성 묶음 적용·거부 응답 | 수정 16 추가 | `/api/config-bundles/{}`, `/topic/health` |  |
| B. 구성·기준 데이터 | `tier0_endpoint` | MVP | 일반 | Tier 0 Pod 엔드포인트 등록 이력(최신 행을 노트북에 줌) | 수정 16 추가 | `/api/endpoints/tier0` | 이연주 |
| B. 구성·기준 데이터 | `layout_plan` | M3 | 일반 | 가상 레이아웃(개구부·비계·장비 주차 구역) | 있음 | `/api/layouts`, `/api/layouts/{}` |  |
| B. 구성·기준 데이터 | `equipment_asset` | 확장 2 | 일반 | 장비 GLB 와 반경·형상 파라미터 | 있음 | 없음(다른 테이블이 참조하거나 I1·I5 수신으로 채움) | 이연주 |
| C. 보정·설치 | `calibration_version` | MVP | 버전 | 보정 버전(상태·스케일 근거·검증 요약) | 있음 | `/api/calibrations/current`, `/api/calibrations/{}/confirmation` | 이연주 |
| C. 보정·설치 | `calibration_camera` | MVP | 일반 | 보정 버전의 카메라별 K·왜곡·R·t | 수정 16 추가 | 없음(다른 테이블이 참조하거나 I1·I5 수신으로 채움) | 이연주 |
| C. 보정·설치 | `calibration_run` | M1 | 일반 | 자동 보정 실행 요청(걷기 보정) | 수정 16 추가 | `/api/calibrations` | 이연주 |
| C. 보정·설치 | `calibration_verification` | M1 | 일반 | 키를 아는 사람 3~5곳 검산 결과 | 수정 16 추가 | `/api/calibrations/{}/verifications` | 이연주 |
| C. 보정·설치 | `scale_reference` | M1 | 일반 | 미리 치수를 잰 시연용 안전 장비 목록 | 수정 16 추가 | `/api/scale-references` |  |
| C. 보정·설치 | `calibration_scale_check` | M1 | 일반 | 보정에서 실측 정물별로 잰 크기와 오차 | 수정 16 추가 | 없음(다른 테이블이 참조하거나 I1·I5 수신으로 채움) | 이연주 |
| C. 보정·설치 | `coverage_guide` | MVP | 일반 | 커버리지 계산 결과와 카메라별 안내 문구 | 수정 16 추가 | `/api/cameras/coverage-guides` | 이연주 |
| C. 보정·설치 | `calibration_update_message` | MVP | 일반 | I1 calibration_update 수신 기록(추가만, msg_id 로 재전송 제거). 현재 보정 상태는 sent_at 이 가장 늦은 것(V9, S15P31A101-263) | V9 추가 | `/api/calibrations/current` |  |
| C. 보정·설치 | `floor_plan` | MVP | 일반 | BEV 평면도(보조 화면) | 수정 16 추가 | `/api/floor-plans/current` | 이연주 |
| C. 보정·설치 | `scene_version` | MVP | 버전 | 정적 3D Scene. 테이블은 MVP(버전 ID 참조), 생성은 M3 | 있음 | `/api/scenes/current`, `/api/scenes/rebuild`, `/api/scenes/{}` | 이연주 |
| D. 시계열 관측 | `world_track_state` | MVP | hypertable | World Track 상태 10 Hz(최소 리플레이 근거) | 있음 | `/api/replay`, `/api/world`, `/topic/world` | 이연주 |
| D. 시계열 관측 | `skeleton_3d` | MVP | hypertable | 3D 골격 5~10 Hz(리플레이 확장·뷰어) | 있음 | `/api/world` | 이연주 |
| D. 시계열 관측 | `system_health` | MVP | hypertable | Tier 0 health 기록(카메라별 값은 JSONB) | 있음 | `/api/health`, `/topic/health` | 이연주 |
| D. 시계열 관측 | `system_outage` | MVP | 일반 | 장애 기록(관제 정지·현장 장비 응답 없음·원격 전달 단절). 마지막 정상 근거·판정·끝 시각, 열린 장애는 하나(V7, S15P31A101-122) | V7 추가 | `/topic/health`(G3 근거) |  |
| D. 시계열 관측 | `system_metric` | MVP | hypertable | 구간별 지연·FPS·큐·손실 지표(긴 형식) | 있음 | `/api/metrics` |  |
| D. 시계열 관측 | `observation_summary` | M1 | 일반 | 2D 관측 카메라별 요약(원본은 Tier 0 저장 위치의 Parquet) | 수정 16 추가 | 없음(다른 테이블이 참조하거나 I1·I5 수신으로 채움) | 이연주 |
| E. 이벤트·조치 | `risk_event` | MVP | 일반 | LIVE·REPLAY 이벤트 현재 상태(전이 반영 결과). PLANNED·WHAT_IF 는 sim | 있음 | `/api/events`, `/api/events/{}`, `/api/replay` 외 | 이연주 |
| E. 이벤트·조치 | `risk_event_update` | MVP | 일반 | Tier 0 전이 기록(추가만). (event_id, event_sequence) 로 중복 제거 | 있음 | 없음(다른 테이블이 참조하거나 I1·I5 수신으로 채움) | 이연주 |
| E. 이벤트·조치 | `event_ack` | MVP | 일반 | 운영자 확인 기록(누가·어디서·어느 상태) | 수정 16 추가 | `/api/events/{}/ack` | 이연주 |
| E. 이벤트·조치 | `event_close` | MVP | 일반 | 관리자의 구역 대상 이벤트 종료 명령과 결과 | 수정 16 추가 | `/api/events/{}/close` | 이연주 |
| E. 이벤트·조치 | `event_escalation` | M3 | 일반 | 승인한 VLM 상향 의견의 적용 명령과 결과 | 수정 16 추가 | `/api/events/{}/escalation` | 이연주 |
| E. 이벤트·조치 | `journal_cursor` | MVP | 일반 | Tier 0 인스턴스별 저널 수신 상태 | 수정 16 추가 | 없음(다른 테이블이 참조하거나 I1·I5 수신으로 채움) | 이연주 |
| E. 이벤트·조치 | `event_frame` | M3 | 일반 | 이벤트 프레임 주소(VLM 입력) | 수정 16 추가 | 없음(다른 테이블이 참조하거나 I1·I5 수신으로 채움) | 이연주 |
| E. 이벤트·조치 | `action_ticket` | M2 | 일반 | 조치 티켓(현재 상태) | 있음 | `/api/events/{}`, `/api/tickets`, `/api/tickets/{}` 외 |  |
| E. 이벤트·조치 | `action_ticket_history` | M2 | 일반 | 티켓 상태 변경 이력 | 수정 16 추가 | `/api/tickets/{}` |  |
| E. 이벤트·조치 | `media_clip` | MVP | 일반 | 이벤트 클립 기록(카메라 하나 × 위험 구간 하나) | 있음 | `/api/clips/{}`, `/api/clips/{}/retention` | 이연주 |
| E. 이벤트·조치 | `media_clip_retention` | MVP | 일반 | 보존 표시 이력(CRITICAL 자동, 관리자 붙이기·떼기) | 있음 | `/api/clips/{}/retention` |  |
| E. 이벤트·조치 | `clip_access_log` | MVP | 일반 | 클립 열람 기록 | 수정 16 추가 | `/api/clips/{}` |  |
| E. 이벤트·조치 | `upload_grant` | MVP | 일반 | 임시 업로드 주소 발급 기록(올라온 키 검증) | 수정 16 추가 | `/api/uploads/sign` |  |
| F. 알림 | `notification` | M1 | 일반 | 실제 알림 발송 기록. LIVE 이벤트만 | 있음 | 없음(다른 테이블이 참조하거나 I1·I5 수신으로 채움) |  |
| F. 알림 | `push_subscription` | M1 | 일반 | Web Push 구독 | 수정 16 추가 | `/api/push/subscribe` |  |
| F. 알림 | `test_push` | M1 | 일반 | 시험 푸시(RiskEvent 아님, notification 에 넣지 않음) | 수정 16 추가 | `/api/push/test` |  |
| F. 알림 | `push_receipt` | M1 | 일반 | Service Worker 수신 확인 원본 | 수정 16 추가 | `/api/push/receipts` |  |
| G. 분석 워커 작업 | `analysis_job` | MVP | 일반 | I5 작업(재할당돼도 같은 job_id) | 수정 16 추가 | `/api/events/{}/summary`, `/api/reports/daily`, `/api/reports/tbm` 외 | 이연주 |
| G. 분석 워커 작업 | `job_lease` | MVP | 일반 | 작업 대여(한 시점에 유효한 대여 하나) | 수정 16 추가 | `/api/worker/jobs/lease`, `/api/worker/jobs/{}/heartbeat`, `/api/worker/jobs/{}/result` | 이연주 |
| H. AI 결과·보고서 | `vlm_assessment` | M3 | 일반 | VLM 근거·사고 구간 요약과 관리자 상향 결정 | 있음 | `/api/events/{}`, `/api/events/{}/escalation`, `/api/events/{}/summary` | 이연주 |
| H. AI 결과·보고서 | `report` | M3 | 일반 | TBM 보고서·일일 일지 | 있음 | `/api/reports/daily`, `/api/reports/tbm`, `/api/reports/tbm/{}` | 이연주 |
| H. AI 결과·보고서 | `rag_clause` | M3 | 일반 | 안전 수칙 조항과 임베딩 | 수정 16 추가 | `/api/reports/tbm` | 이연주 |
| H. AI 결과·보고서 | `rag_query_template` | M3 | 일반 | TBM 질문 템플릿과 질문 임베딩 | S15P31A101-27 추가 | `/api/reports/tbm` | 이연주 |
| I. 계획·시뮬레이션 (sim 스키마) | `sim.evaluation_run` | M3 | 일반 | EC2 Risk Engine 컨테이너 계산 실행(PLANNED·WHAT_IF) | 있음 | `/api/layouts/{}/evaluate`, `/api/layouts/{}/evaluations/{}`, `/api/reports/tbm` 외 | 이연주 |
| I. 계획·시뮬레이션 (sim 스키마) | `sim.risk_result` | M3 | 일반 | 계산된 위험 항목(실제 알림 채널로 가지 않음) | 있음 | `/api/layouts/{}/evaluate`, `/api/layouts/{}/evaluations/{}`, `/api/simulations/{}` 외 | 이연주 |
| I. 계획·시뮬레이션 (sim 스키마) | `sim.trajectory_plan` | 확장 2 | 일반 | 시간 구간이 있는 계획 궤적 | 있음 | `/api/simulations` | 이연주 |
| I. 계획·시뮬레이션 (sim 스키마) | `sim.what_if_scenario` | 확장 4 | 일반 | 과거 로그·계획에서 사람·장비·신호수 위치를 옮긴 시나리오 | 있음 | `/api/what-if-analyses` | 이연주 |
| I. 계획·시뮬레이션 (sim 스키마) | `sim.visibility_result` | 확장 4 | 일반 | 지점별 가시 카메라 수·안전모 픽셀 크기 | 있음 | `/api/placement-recommendations` | 이연주 |
| I. 계획·시뮬레이션 (sim 스키마) | `sim.placement_recommendation` | 확장 4 | 일반 | 카메라·신호수·펜스 배치 제안 | 있음 | `/api/placement-recommendations` | 이연주 |
| J. 확장: 에이전틱·공정 | `agentic_report` | 확장 3 | 일반 | 작업중지 명령서 초안·우회 동선·반장 전달문 | 수정 16 추가 | `/api/agentic-reports/{}`, `/api/agentic-reports/{}/approval` | 이연주 |
| J. 확장: 에이전틱·공정 | `operator_qa` | 확장 3 | 일반 | LLM 이 로그 조회 도구로 답한 질의응답 | 수정 16 추가 | 없음(다른 테이블이 참조하거나 I1·I5 수신으로 채움) | 이연주 |
| J. 확장: 에이전틱·공정 | `work_schedule` | 확장 5 | 일반 | 오늘의 작업·공정을 구역에 연결 | 수정 16 추가 | `/api/work-schedules` |  |
| K. 확장: 워치 | `watch_registrant` | 확장 | 일반 | 워치 등록자(관리자만 조회) | 수정 16 추가 | 없음(다른 테이블이 참조하거나 I1·I5 수신으로 채움) |  |
| K. 확장: 워치 | `watch_binding` | 확장 | 일반 | 익명 World ID ↔ 워치 배정 이력 | 있음 | 없음(다른 테이블이 참조하거나 I1·I5 수신으로 채움) | 이연주 |
| K. 확장: 워치 | `channel_state` | 확장 | hypertable | World ID 별 개인 채널 상태 | 있음 | 없음(다른 테이블이 참조하거나 I1·I5 수신으로 채움) | 이연주 |
| K. 확장: 워치 | `watch_feature` | 확장 | hypertable | 워치 가속도 특징 | 있음 | `I4 gait_features` | 이연주 |
| K. 확장: 워치 | `watch_alert` | 확장 | 일반 | 워치 진동 알림 상세(notification 의 WATCH 채널) | 수정 16 추가 | `/api/watch-alerts/{}/acks`, `I4 ack` |  |
| K. 확장: 워치 | `watch_impact` | 확장 | 일반 | 워치 충격 이벤트(낙상 추가 근거) | 수정 16 추가 | `I4 impact` |  |
| L. 확장: PPE·정적 공간 변화 | `ppe_state` | 확장 | hypertable | World ID 별 PPE 판정 | 있음 | 없음(다른 테이블이 참조하거나 I1·I5 수신으로 채움) | 이연주 |
| L. 확장: PPE·정적 공간 변화 | `region_state` | 확장 | 일반 | 정적 공간 변화 영역(임시 형상·불확실 점유) | 있음 | 없음(다른 테이블이 참조하거나 I1·I5 수신으로 채움) | 이연주 |

"수정 16 추가"는 ERD 1단계에서 새로 정해 기획서 v1.3 수정 16(S15P31A101-207)로 11장·부록 C 에 넣은 테이블이다.

### DB 에 두지 않는 것

| 데이터 | 위치 |
|---|---|
| observation_2d | Tier 0 저장 위치(3.12)의 Parquet(11.2). EC2 에는 observation_summary 만 |
| 이벤트 클립·사진·프레임·glTF·BEV 이미지 | 객체 저장소. DB 에는 키·주소만 |
| World Track 체크포인트·이벤트 저널 원본 | Tier 0 저장 위치(3.2, 3.12). EC2 는 받은 전이만 저장 |
| 녹화 세션 manifest | 저장소 docs/data/sessions 와 Tier 0 저장 위치(15.3) |
| EXP-01·02 산출물(scene_output, human_mesh_output, run_record) | Tier 0 저장 위치와 실험 기록. 제품 DB 아님(20장) |

## 3. 테이블 정의

### A. 계정·기기·인증

14장 역할 3종·기기 토큰·워커 토큰. 11장에 없어 추가.

#### `app_user` — 관제 화면·PWA 계정. 역할 3종

- 단계 **MVP** · 일반 · 근거: 14장, FR-1·2

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `user_id` | bigint identity | PK |  |  |
| `login_id` | text | UQ, NN |  | 로그인 ID |
| `password_hash` | text | NN |  |  |
| `role` | text | NN, CK(ADMIN·FOREMAN·VIEWER) |  | 관리자·반장·열람 (14장) |
| `display_label` | text |  |  | 화면 표시용 별칭. 위치·이벤트 기록에는 user_id 만 쓴다(11.4-5) |
| `created_at` | ts | NN |  |  |
| `disabled_at` | ts |  |  | 비활성 시각(삭제하지 않음) |

#### `refresh_token` — JWT 갱신 토큰

- 단계 **MVP** · 일반 · 근거: 14장 반장 PWA 긴 갱신 토큰

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `token_id` | uuid | PK |  |  |
| `user_id` | bigint | FK>app_user.user_id, NN |  |  |
| `token_hash` | text | UQ, NN |  | 원문은 저장하지 않는다 |
| `issued_at` | ts | NN |  |  |
| `expires_at` | ts | NN |  |  |
| `revoked_at` | ts |  |  |  |

#### `device` — 현장 노트북(확장: 워치) 기기와 토큰

- 단계 **MVP** · 일반 · 근거: 14장 기기 토큰, 12.6 기기 권한

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `device_id` | text | PK | `envelope.source_id` | 예: laptop-01 |
| `kind` | text | NN, CK(LAPTOP·WATCH) |  | WATCH 는 확장(C.6) |
| `token_hash` | text | NN |  | 기기마다 따로 발급 |
| `issued_at` | ts | NN |  |  |
| `rotated_at` | ts |  |  | 주기 회전(14장) |
| `revoked_at` | ts |  |  | 분실·교체 시 폐기 |
| `last_heartbeat_at` | ts |  |  | 최근 하트비트 수신 시각(조회 편의, 정본은 device_heartbeat) |

#### `device_heartbeat` — 노트북 → EC2 1초 하트비트 기록. remote_state 판단 근거

- 단계 **MVP** · hypertable · 근거: 3.2 이중 감시, laptop_heartbeat

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `received_at` | ts | PK(1), NN |  | EC2 수신 시각, 시간 축 |
| `device_id` | text | PK(2), FK>device.device_id | `envelope.source_id` |  |
| `sent_at` | ts | NN | `envelope.sent_at` | 노트북 시계 |
| `heartbeat_seq` | bigint | NN | `laptop_heartbeat.heartbeat_seq` |  |
| `tier0_control` | text | NN, CK(CONNECTED·DISCONNECTED) | `laptop_heartbeat.tier0.control` |  |
| `tier0_last_message_at` | ts |  | `laptop_heartbeat.tier0.last_message_at` |  |
| `tier0_video_cameras` | text[] | NN | `laptop_heartbeat.tier0.video_cameras` |  |
| `ec2_state` | text |  | `laptop_heartbeat.ec2.state` |  |
| `site_display` | text | NN, CK(NORMAL·CONTROL_HALTED·REMOTE_FAULT·AI_DELAY) | `laptop_heartbeat.site_display` |  |
| `alarm` | jsonb |  | `laptop_heartbeat.alarm` | 소리·점멸 중인 이벤트 목록 |
| `cameras` | jsonb |  | `laptop_heartbeat.cameras` | 카메라별 RTSP 상태·도착 간격 |
| `ec2_last_response_at` | ts |  | `laptop_heartbeat.ec2.last_response_at` |  |

#### `worker` — 분석 워커와 토큰

- 단계 **MVP** · 일반 · 근거: 12.5 워커 토큰, 14장 · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `worker_id` | text | PK | `envelope.source_id` | 예: worker-01 |
| `token_hash` | text | NN |  |  |
| `job_types` | text[] | NN | `job_lease_request.job_types` | 마지막 대여 요청의 처리 가능 유형 |
| `issued_at` | ts | NN |  |  |
| `revoked_at` | ts |  |  |  |
| `last_seen_at` | ts |  |  |  |

### B. 구성·기준 데이터

11.1. 수정하지 않고 새 버전을 추가한다(11.4-1).

#### `site` — 현장과 World 좌표계 정의

- 단계 **MVP** · 일반 · 근거: 11.1 site, 4.1

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `site_id` | text | PK | `coordinate_frame.site_id` |  |
| `name` | text | NN |  |  |
| `world_origin` | jsonb |  |  | 원점·축 선택 근거(첫 CALIBRATED 의 바닥·벽, 4.1) |
| `established_calibration_version` | text | FK>calibration_version.calibration_version |  | 좌표계를 정한 보정 |
| `current_floor_plan_id` | bigint | FK>floor_plan.floor_plan_id |  | 현재 BEV 평면도 |
| `migrated_from` | jsonb |  |  | 좌표계 이전 이력 |
| `created_at` | ts | NN |  |  |

#### `camera` — 카메라 장비 정보

- 단계 **MVP** · 일반 · 근거: 11.1 camera, 3.3

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `camera_id` | text | PK | `camera_id` | 예: cam1 |
| `site_id` | text | FK>site.site_id, NN |  |  |
| `model` | text | NN |  | 예: Tapo C110 |
| `hardware_version` | text |  |  |  |
| `firmware_version` | text |  |  |  |
| `width` | int |  | `codec_info.width` |  |
| `height` | int |  | `codec_info.height` |  |
| `nominal_fps` | real |  | `codec_info.nominal_fps` |  |
| `installed_at` | ts |  |  |  |
| `removed_at` | ts |  |  | 철거(R7·운영자 종료 근거) |

#### `zone` — 위험 구역. 수정은 새 버전

- 단계 **MVP** · 버전 · 근거: 11.1 zone, 8.2, 7.2

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `zone_id` | text | PK(1) | `risk_event.zone_id` |  |
| `zone_version` | int | PK(2) |  |  |
| `site_id` | text | FK>site.site_id, NN |  |  |
| `name` | text | NN | `risk_alert.zone_name` |  |
| `zone_kind` | text | NN |  | 유형(개구부·출입 통제·장비 주차 등) |
| `eval_type` | text | NN, CK(GROUND_FOOTPRINT·VOLUME_3D) |  | 8.2 |
| `polygon` | geometry(Polygon,0) | NN |  | World XY(m), SRID 0 로컬 좌표. 저장·조회만(판정은 Risk Engine) |
| `z_min_m` | real |  |  | VOLUME_3D 높이 범위 |
| `z_max_m` | real |  |  |  |
| `severity` | text | NN, CK(CRITICAL·HIGH·CAUTION·INFO) |  |  |
| `requirements` | jsonb |  |  | 요구 조건(안전모 요구 등, R9 확장) |
| `has_guardrail` | boolean |  |  | R3 난간 유무 |
| `valid_from` | ts | NN |  |  |
| `valid_to` | ts |  |  | null = 현재 유효 |
| `created_by` | bigint | FK>app_user.user_id |  |  |

#### `static_object` — 등록 작업면·정적 객체(Operational Geometry)

- 단계 **MVP** · 버전 · 근거: 7.5, 12.6 /api/static-objects

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `object_id` | text | PK(1) | `world_state.support_surface_id` |  |
| `object_version` | int | PK(2) |  |  |
| `site_id` | text | FK>site.site_id, NN |  |  |
| `kind` | text | NN, CK(WORK_SURFACE·OBSTACLE) |  |  |
| `obb` | jsonb | NN |  | 보수적 OBB(중심·크기·yaw) |
| `top_height_m` | real |  |  | 상판 높이(작업면) |
| `name` | text |  |  |  |
| `valid_from` | ts | NN |  |  |
| `valid_to` | ts |  |  |  |
| `created_by` | bigint | FK>app_user.user_id |  |  |

#### `rule_version` — Risk Engine 규칙 임계값 세트. 규칙 등록부에서 자동 생성한 Flyway 시드로 채운다

- 단계 **MVP** · 버전 · 근거: 11.1 rule_version, 7.2·7.4, 16장(시드) · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `rule_id` | text | PK(1) | `risk_event_transition.rule_id` | R1~R10 |
| `rule_version` | int | PK(2) | `risk_event_transition.rule_version` | 교실 시험 프로파일은 기본 + 1000(7.2) |
| `profile` | text | NN, CK(OPERATION·CLASSROOM_TEST) |  |  |
| `thresholds` | jsonb | NN |  | 규칙 객체의 설정값(규칙 등록부에서 추출, ai `simai_ai.risk.rule_catalog`) |
| `default_severity` | text | CK(CRITICAL·HIGH·CAUTION·INFO) |  | 규칙 모듈의 고정 심각도. 비면 규칙이 판정마다 정한다(R1 은 구역 심각도) |
| `legal_basis` | text |  |  | 근거 조항(RuleSpec.legal_basis) |
| `reads` | text[] | NN |  | 규칙이 읽는 상태(RuleSpec.reads) |
| `stage` | text |  |  | MVP·M2·확장 |
| `valid_from` | ts | NN |  |  |
| `valid_to` | ts |  |  |  |

#### `config_bundle` — 구성 묶음 발행과 현장 적용

- 단계 **MVP** · 버전 · 근거: 11.1 config_bundle, 3.2, config_bundle·config_ack · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `config_version` | text | PK | `config_bundle.config_version` |  |
| `previous_config_version` | text | FK>config_bundle.config_version | `config_bundle.previous_config_version` |  |
| `site_id` | text | FK>site.site_id, NN |  |  |
| `issued_at` | ts | NN | `config_bundle.issued_at` |  |
| `issued_by` | bigint | FK>app_user.user_id |  |  |
| `bundle_uri` | text | NN | `config_bundle.bundle_uri` |  |
| `bundle_hash` | text | NN | `config_bundle.bundle_hash` |  |
| `components` | jsonb | NN | `config_bundle.components` | 구성 요소별 version·sha256·uri |
| `calibration_version` | text | FK>calibration_version.calibration_version, NN | `config_bundle.components.calibration` |  |
| `applied_at` | ts |  | `config_ack.applied_at` | 현장 적용 시각(APPLIED 받은 뒤) |

#### `config_bundle_item` — 묶음에 들어간 구역·규칙·형상 버전 목록

- 단계 **MVP** · 일반 · 근거: 3.2 구성 묶음 = 구역·규칙·형상 버전

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `config_version` | text | PK(1), FK>config_bundle.config_version |  |  |
| `item_type` | text | PK(2), CK(ZONE·RULE·STATIC_OBJECT) |  |  |
| `item_id` | text | PK(3) |  | zone_id·rule_id·object_id |
| `item_version` | int | NN |  |  |

#### `config_ack` — Tier 0 의 구성 묶음 적용·거부 응답

- 단계 **MVP** · 일반 · 근거: config_ack, 6.2 Config Sync

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `ack_msg_id` | uuid | PK | `envelope.msg_id` |  |
| `config_version` | text | FK>config_bundle.config_version, NN | `config_ack.config_version` |  |
| `gateway_id` | text | NN | `envelope.source_id` |  |
| `result` | text | NN, CK(APPLIED·REJECTED) | `config_ack.result` |  |
| `bundle_hash` | text | NN | `config_ack.bundle_hash` |  |
| `applied_at` | ts |  | `config_ack.applied_at` |  |
| `reject_reason` | text |  | `config_ack.reject_reason` |  |
| `reject_detail` | text |  | `config_ack.reject_detail` |  |
| `active_config_version` | text |  | `config_ack.active_config_version` |  |
| `received_at` | ts | NN |  |  |

#### `tier0_endpoint` — Tier 0 Pod 엔드포인트 등록 이력(최신 행을 노트북에 줌)

- 단계 **MVP** · 일반 · 근거: 3.12, tier0_endpoint, GET /api/endpoints/tier0 · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `endpoint_id` | bigint identity | PK |  |  |
| `gateway_id` | text | NN | `tier0_endpoint.gateway_id` |  |
| `public_ip` | inet | NN | `tier0_endpoint.public_ip` |  |
| `ports` | jsonb | NN | `tier0_endpoint.ports` | role·camera_id·requested_port·public_port |
| `tls_public_key_sha256` | text | NN | `tier0_endpoint.tls_public_key_sha256` | 공개키 고정 지문 |
| `registered_at` | ts | NN | `tier0_endpoint.registered_at` |  |
| `pod_id` | text |  | `tier0_endpoint.pod_id` |  |
| `datacenter` | text |  | `tier0_endpoint.datacenter` |  |
| `received_at` | ts | NN |  |  |
| `msg_id` | uuid | UQ | 봉투 `msg_id` | 재전송 제거(V8, S15P31A101-290) |
| `schema_version` | text |  | 봉투 `schema_version` | 조회 응답 봉투(V8) |
| `sent_at` | ts |  | 봉투 `sent_at` | 조회 응답 봉투(V8) |

#### `layout_plan` — 가상 레이아웃(개구부·비계·장비 주차 구역)

- 단계 **M3** · 일반 · 근거: 11.1 layout_plan, 8.3, 12.6 /api/layouts

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `layout_id` | bigint identity | PK |  |  |
| `site_id` | text | FK>site.site_id, NN |  |  |
| `name` | text | NN |  |  |
| `items` | jsonb | NN |  | 배치 요소(유형·형상·높이) |
| `revision` | int | NN |  | PUT 마다 증가(보고서는 계산한 revision 을 참조) |
| `source` | text | NN, CK(PLANNED) |  |  |
| `created_by` | bigint | FK>app_user.user_id |  |  |
| `created_at` | ts | NN |  |  |
| `updated_at` | ts | NN |  |  |

#### `equipment_asset` — 장비 GLB 와 반경·형상 파라미터

- 단계 **확장 2** · 일반 · 근거: 11.1 equipment_asset, 부록 C.2, R8 · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `asset_id` | bigint identity | PK |  |  |
| `name` | text | NN |  |  |
| `glb_key` | text | NN |  | 객체 저장소 키 |
| `radius_m` | real | NN |  |  |
| `shape_params` | jsonb |  |  |  |
| `heading_supported` | boolean | NN |  | R8 은 방향 추정이 전제 |
| `created_at` | ts | NN |  |  |

### C. 보정·설치

4장, 12.6 보정·실측 정물·설치 안내·BEV. 11장에 없어 추가.

#### `calibration_version` — 보정 버전(상태·스케일 근거·검증 요약)

- 단계 **MVP** · 버전 · 근거: 11.1, 4장, calibration_update·calibration provider · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `calibration_version` | text | PK | `calibration_update.calibration_version` | 예: cal-7 |
| `site_id` | text | FK>site.site_id, NN |  |  |
| `previous_calibration_version` | text | FK>calibration_version.calibration_version | `calibration_update.previous_calibration_version` |  |
| `state` | text | NN, CK(UNCALIBRATED·PROVISIONAL·CALIBRATED·DRIFTED) | `calibration_update.calibration_state` |  |
| `provider_source` | text | NN, CK(G1_AUTO·DATASET_GT·SIM·MODEL_ESTIMATE) | `calibration_update.provider_source` |  |
| `provider_version` | text |  | `calibration.provider_version` |  |
| `reason` | text | NN | `calibration_update.reason` | INITIAL·RECALIBRATED·STATE_CHANGED·DRIFT_DETECTED·STREAM_CHANGED |
| `effective_at` | ts | NN | `calibration_update.effective_at` |  |
| `calibration_ref` | text | NN | `calibration_update.calibration_ref` | 보정 문서 주소 |
| `calibration_hash` | text | NN | `calibration_update.calibration_hash` |  |
| `scale_basis` | text |  | `calibration.scale_basis` |  |
| `scale_cross_check_basis` | text[] |  | `calibration.scale_cross_check_basis` |  |
| `validation` | jsonb |  | `calibration.validation` | 독립 검증 요약 |
| `operating_reprojection` | jsonb |  | `calibration_update.cameras` | 운영 중 카메라별 재투영 오차(최신값) |
| `source_job_id` | text | FK>analysis_job.job_id |  | calibration_ba 작업 |
| `confirmed_by` | bigint | FK>app_user.user_id |  | POST …/confirmation |
| `confirmed_at` | ts |  |  |  |
| `created_at` | ts | NN | `calibration.created_at` |  |

#### `calibration_camera` — 보정 버전의 카메라별 K·왜곡·R·t

- 단계 **MVP** · 일반 · 근거: calibration provider cameras[] · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `calibration_version` | text | PK(1), FK>calibration_version.calibration_version |  |  |
| `camera_id` | text | PK(2), FK>camera.camera_id | `calibration.cameras.camera_id` |  |
| `image_size` | int[2] | NN | `calibration.cameras.image_size` |  |
| `k_matrix` | jsonb | NN | `calibration.cameras.K` | 3×3 |
| `distortion_model` | text | NN | `calibration.cameras.distortion.model` |  |
| `distortion_coeffs` | double precision[] | NN | `calibration.cameras.distortion.coeffs` |  |
| `r_matrix` | jsonb | NN | `calibration.cameras.R` |  |
| `t_vector` | double precision[3] | NN | `calibration.cameras.t` |  |
| `camera_height_m` | real |  | `calibration.cameras.camera_height_m` |  |
| `reprojection_median_px` | real |  | `calibration.cameras.reprojection_error_px.median` |  |
| `reprojection_p95_px` | real |  | `calibration.cameras.reprojection_error_px.p95` |  |

#### `calibration_run` — 자동 보정 실행 요청(걷기 보정)

- 단계 **M1** · 일반 · 근거: POST /api/calibrations, 4.3 · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `run_id` | bigint identity | PK |  |  |
| `site_id` | text | FK>site.site_id, NN |  |  |
| `status` | text | NN, CK(WALKING·SOLVING·CANDIDATE·FAILED·CONFIRMED) |  |  |
| `requested_by` | bigint | FK>app_user.user_id |  |  |
| `requested_at` | ts | NN |  |  |
| `walk_started_at` | ts |  |  |  |
| `walk_ended_at` | ts |  |  |  |
| `obs2d_archive_uri` | text |  | `observation_summary.archive_uri` | calibration_ba 입력 |
| `job_id` | text | FK>analysis_job.job_id |  |  |
| `result_calibration_version` | text | FK>calibration_version.calibration_version |  | 보정 후보 |
| `failure_reason` | text |  |  |  |

#### `calibration_verification` — 키를 아는 사람 3~5곳 검산 결과

- 단계 **M1** · 일반 · 근거: 4.4 독립 검증점, POST …/verifications · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `verification_id` | bigint identity | PK |  |  |
| `calibration_version` | text | FK>calibration_version.calibration_version, NN |  |  |
| `checkpoint_count` | int | NN | `calibration.validation.checkpoint_count` |  |
| `max_pairwise_position_disagreement_m` | real |  | `calibration.validation.max_pairwise_position_disagreement_m` |  |
| `max_head_height_error_m` | real |  | `calibration.validation.max_head_height_error_m` |  |
| `max_position_error_m` | real |  | `calibration.validation.max_position_error_m` | 실측 기준점이 있을 때만 |
| `floor_plane_disagreement_m` | real |  | `calibration.validation.floor_plane_disagreement_m` |  |
| `floor_coverage_ratio` | real |  | `calibration.validation.floor_coverage_ratio` |  |
| `passed` | boolean | NN |  | CALIBRATED 기준 통과 여부 |
| `detail` | jsonb |  |  | 검산점별 값 |
| `submitted_by` | bigint | FK>app_user.user_id |  |  |
| `submitted_at` | ts | NN |  |  |

#### `scale_reference` — 미리 치수를 잰 시연용 안전 장비 목록

- 단계 **M1** · 일반 · 근거: 4.3 실측 정물, FR-135, /api/scale-references

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `scale_reference_id` | bigint identity | PK |  |  |
| `site_id` | text | FK>site.site_id |  |  |
| `name` | text | NN |  | 예: 라바콘 A |
| `kind` | text | NN |  | 안전봉·라바콘·바리케이드 등 |
| `height_m` | real | NN |  |  |
| `width_m` | real |  |  |  |
| `depth_m` | real |  |  |  |
| `measured_at` | date | NN |  | 측정일 |
| `created_by` | bigint | FK>app_user.user_id |  |  |
| `retired_at` | ts |  |  |  |

#### `calibration_scale_check` — 보정에서 실측 정물별로 잰 크기와 오차

- 단계 **M1** · 일반 · 근거: 4.3 정물끼리 불일치 경고, D-G1-04 · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `calibration_version` | text | PK(1), FK>calibration_version.calibration_version |  |  |
| `scale_reference_id` | bigint | PK(2), FK>scale_reference.scale_reference_id |  |  |
| `measured_height_m` | real | NN |  | 2대 이상 삼각측량 |
| `error_ratio` | real | NN |  |  |
| `used_for_scale` | boolean | NN |  |  |
| `mismatch_warning` | boolean | NN |  |  |

#### `coverage_guide` — 커버리지 계산 결과와 카메라별 안내 문구

- 단계 **MVP** · 일반 · 근거: 4.5, FR-26, /api/cameras/coverage-guides · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `guide_id` | bigint identity | PK |  |  |
| `calibration_version` | text | FK>calibration_version.calibration_version |  |  |
| `camera_id` | text | FK>camera.camera_id |  | null 이면 전체(걷기 경로 안내) |
| `message` | text | NN |  | 예: 카메라 3번이 너무 아래를 봄 |
| `metrics` | jsonb |  |  | 바닥 커버리지 비율 등 |
| `created_at` | ts | NN |  |  |

#### `floor_plan` — BEV 평면도(보조 화면)

- 단계 **MVP** · 일반 · 근거: 4.1 BEV 평면도, FR-22, bev_floorplan · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `floor_plan_id` | bigint identity | PK |  |  |
| `site_id` | text | FK>site.site_id, NN |  |  |
| `calibration_version` | text | FK>calibration_version.calibration_version, NN | `job_request.input.calibration_version` |  |
| `scene_version` | text | FK>scene_version.scene_version |  | 정적 3D 가 있으면 |
| `image_key` | text |  | `job_callback.result` | 객체 저장소 키 |
| `status` | text | NN, CK(PENDING·READY·FAILED) |  |  |
| `job_id` | text | FK>analysis_job.job_id |  |  |
| `created_at` | ts | NN |  |  |

#### `scene_version` — 정적 3D Scene. 테이블은 MVP(버전 ID 참조), 생성은 M3

- 단계 **MVP** · 버전 · 근거: 11.1 scene_version, 5.3, 6.2, FR-27·108 · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `scene_version` | text | PK | `world_state.scene_version` | 예: scene-3 |
| `site_id` | text | FK>site.site_id, NN |  |  |
| `calibration_version` | text | FK>calibration_version.calibration_version, NN |  | CALIBRATED 에서만 생성(4.5) |
| `state` | text | NN, CK(CANDIDATE·VALIDATING·COMMITTED·REJECTED) |  |  |
| `gltf_key` | text |  | `job_callback.result` |  |
| `quality` | jsonb |  | `job_callback.result` | 품질 지표 |
| `input_provenance` | jsonb |  | `input_provenance` |  |
| `is_upper_bound` | boolean | NN | `input_provenance.is_upper_bound` | true 면 COMMITTED 불가 |
| `job_id` | text | FK>analysis_job.job_id |  | static_reconstruct |
| `requested_by` | bigint | FK>app_user.user_id |  | POST /api/scenes/rebuild |
| `committed_at` | ts |  |  |  |
| `rejected_reason` | text |  |  |  |
| `valid_from` | ts |  |  | COMMITTED 시작 |
| `valid_to` | ts |  |  |  |

### D. 시계열 관측

11.2. 볼륨이 큰 것은 TimescaleDB hypertable.

#### `world_track_state` — World Track 상태 10 Hz(최소 리플레이 근거)

- 단계 **MVP** · hypertable · 근거: 11.2, world_state(I1) tracks[] · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `observation_at` | ts | PK(1), NN | `world_state.t_latest_global` | 시간 축 |
| `world_id` | text | PK(2) | `world_state.tracks.world_id` |  |
| `msg_id` | uuid | NN | `envelope.msg_id` | 이벤트 evidence_ref 가 가리키는 값(인덱스) |
| `gateway_id` | text | NN | `envelope.source_id` |  |
| `t_last_valid` | ts | NN | `world_state.tracks.t_last_valid` |  |
| `observation_state` | text | NN | `world_state.tracks.observation_state` |  |
| `track_lifecycle` | text | NN | `world_state.tracks.track_lifecycle` |  |
| `identity_confidence` | text | NN | `world_state.tracks.identity_confidence` |  |
| `identity_revision` | int | NN | `world_state.tracks.identity_revision` |  |
| `split_from` | text |  | `world_state.tracks.split_from` |  |
| `aliases` | text[] |  | `world_state.tracks.aliases` |  |
| `frame_age_ms` | int |  | `world_state.tracks.frame_age_ms` |  |
| `position` | geometry(Point,0) | NN | `world_state.tracks.position_xy` |  |
| `position_xy_sigma` | real[2] | NN | `world_state.tracks.position_xy_sigma` |  |
| `pelvis_xyz` | real[3] |  | `world_state.tracks.pelvis_xyz` |  |
| `ground_anchor_xyz` | real[3] |  | `world_state.tracks.ground_anchor_xyz` | Z=0 대체 금지 |
| `ground_anchor_sigma` | real[3] |  | `world_state.tracks.ground_anchor_sigma` |  |
| `surface_source` | text |  | `world_state.tracks.surface_source` |  |
| `support_surface_id` | text |  | `world_state.tracks.support_surface_id` |  |
| `support_surface_validity` | text | NN | `world_state.tracks.support_surface_validity` |  |
| `contact_validity` | text | NN | `world_state.tracks.contact_validity` |  |
| `z_validity` | text | NN | `world_state.tracks.z_validity` |  |
| `head_z` | real |  | `world_state.tracks.head_z` |  |
| `head_z_sigma` | real |  | `world_state.tracks.head_z_sigma` |  |
| `head_z_validity` | text |  | `world_state.tracks.head_z_validity` |  |
| `velocity` | real[3] |  | `world_state.tracks.velocity` |  |
| `cameras` | text[] | NN | `world_state.tracks.cameras` |  |
| `calibration_version` | text | NN | `world_state.calibration_version` | 논리 참조(hypertable 은 FK 를 걸지 않음) |
| `scene_version` | text | NN | `world_state.scene_version` |  |
| `config_version` | text |  | `world_state.config_version` |  |
| `input_provenance` | jsonb |  | `world_state.input_provenance` |  |
| `received_at` | ts | NN |  |  |

#### `skeleton_3d` — 3D 골격 5~10 Hz(리플레이 확장·뷰어)

- 단계 **MVP** · hypertable · 근거: 11.2, world_state tracks[].skeleton · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `observation_at` | ts | PK(1), NN | `world_state.t_latest_global` |  |
| `world_id` | text | PK(2) | `world_state.tracks.world_id` |  |
| `joint_layout` | text | NN | `world_state.tracks.skeleton.joint_layout` |  |
| `joints` | real[][] | NN | `world_state.tracks.skeleton.joints` | null 관절은 NaN 이 아니라 null |
| `visible` | boolean[] | NN | `world_state.tracks.skeleton.visible` |  |
| `validity` | text[] | NN | `world_state.tracks.skeleton.validity` |  |
| `joint_source` | text[] | NN | `world_state.tracks.skeleton.joint_source` | observed·inferred |

#### `system_health` — Tier 0 health 기록(카메라별 값은 JSONB)

- 단계 **MVP** · hypertable · 근거: 11.2, health(I1) 1 Hz · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `reported_at` | ts | PK(1), NN | `envelope.sent_at` | 시간 축(Tier 0 시계) |
| `gateway_id` | text | PK(2) | `envelope.source_id` |  |
| `msg_id` | uuid | NN | `envelope.msg_id` |  |
| `system_health` | text | NN | `health.system_health` |  |
| `t_latest_global` | ts |  | `health.t_latest_global` |  |
| `pipeline_lag_ms` | int |  | `health.pipeline_lag_ms` | null = 모름 |
| `pipeline_lag_lower_bound_ms` | int |  | `health.pipeline_lag_lower_bound_ms` |  |
| `capture_to_risk_ms` | jsonb |  | `health.capture_to_risk_ms` | p50·p95·p99 |
| `calibration_state` | text | NN | `health.calibration_state` |  |
| `calibration_version` | text |  | `health.calibration_version` |  |
| `config_version` | text |  | `health.config_version` |  |
| `config_sync` | text |  | `health.config_sync` |  |
| `gateway_link` | text |  | `health.gateway_link` |  |
| `cameras` | jsonb | NN | `health.cameras` | 카메라별 fps·상태·동기화·프레임 집계 |
| `tier0_warnings` | jsonb |  | `health.tier0_warnings` |  |
| `ec2_world_state_drops` | jsonb |  | `health.ec2_world_state_drops` |  |
| `gpu_util` | real |  | `health.gpu_util` |  |
| `gpu_memory_used_ratio` | real |  | `health.gpu_memory_used_ratio` |  |
| `active_site_alerts` | jsonb |  | `health.active_site_alerts` |  |
| `active_site_alerts_as_of` | ts |  | `health.active_site_alerts_as_of` |  |
| `laptop_clock_offset` | jsonb |  | `health.laptop_clock_offset` |  |
| `received_at` | ts | NN |  |  |

#### `system_metric` — 구간별 지연·FPS·큐·손실 지표(긴 형식)

- 단계 **MVP** · hypertable · 근거: 11.2 system_metric, 6.6 지연 목표

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `measured_at` | ts | PK(1), NN |  |  |
| `source_id` | text | PK(2) |  | tier0-01·laptop-01·ec2·worker-01 |
| `metric` | text | PK(3) |  | 예: capture_to_site_alert_ms, gateway_to_runpod_loss |
| `camera_id` | text | PK(4) |  | 카메라 무관이면 빈 문자열 |
| `value` | double precision | NN |  |  |
| `unit` | text |  |  |  |
| `labels` | jsonb |  |  | 백분위 등 |

#### `observation_summary` — 2D 관측 카메라별 요약(원본은 Tier 0 저장 위치의 Parquet)

- 단계 **M1** · 일반 · 근거: 11.2, observation_summary(I1), FR-129·141 · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `summary_id` | bigint identity | PK |  |  |
| `gateway_id` | text | NN | `envelope.source_id` |  |
| `window_start` | ts | NN | `observation_summary.window_start_ms` |  |
| `window_end` | ts | NN | `observation_summary.window_end_ms` |  |
| `calibration_version` | text |  | `observation_summary.calibration_version` |  |
| `archive_uri` | text |  | `observation_summary.archive_uri` | volume:// 원본 위치 |
| `cameras` | jsonb | NN | `observation_summary.cameras` | 카메라별 처리 프레임·검출·트랙 수 |
| `received_at` | ts | NN |  |  |

### E. 이벤트·조치

11.3. LIVE 이벤트 정본은 Tier 0 저널이고 EC2 는 받아 저장만 한다(FR-95).

#### `risk_event` — LIVE·REPLAY 이벤트 현재 상태(전이 반영 결과). PLANNED·WHAT_IF 는 sim

- 단계 **MVP** · 일반 · 근거: 11.3 risk_event, risk_event(I2) projection · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `event_id` | uuid | PK, UQ(event_source) | `risk_event.event_id` | UUIDv7 |
| `source` | text | NN, UQ(event_source), CK(LIVE·REPLAY) | `risk_event.source` | notification 복합 FK 대상(11.4-3) |
| `gateway_id` | text |  | `risk_event.gateway_id` |  |
| `rule_id` | text | NN | `risk_event.rule_id` |  |
| `rule_version` | int | NN | `risk_event.rule_version` | (rule_id, rule_version) 논리 참조 |
| `lifecycle` | text | NN, CK(RAISED·UPDATED·RESOLVED) | `risk_event.lifecycle` |  |
| `verification` | text | NN | `risk_event.verification` |  |
| `severity` | text | NN | `risk_event.severity` | 운영자 상향 반영 후 등급 |
| `confidence` | text | NN | `risk_event.confidence` |  |
| `needs_check` | boolean | NN | `risk_event.needs_check` |  |
| `confidence_reasons` | text[] |  | `risk_event.confidence_reasons` |  |
| `world_ids` | text[] | NN | `risk_event.world_ids` | 발생 당시 원본 ID |
| `zone_id` | text |  | `risk_event.zone_id` |  |
| `metrics` | jsonb | NN | `risk_event.metrics` |  |
| `rationale` | text |  | `risk_event.rationale` | 최신 VLM 근거(정본은 vlm_assessment) |
| `proposed_severity` | text |  | `risk_event.proposed_severity` |  |
| `resume_count` | int | NN | `risk_event.resume_count` |  |
| `event_sequence` | int |  | `risk_event.event_sequence` | 반영한 마지막 전이 |
| `config_version` | text |  | `risk_event.config_version` |  |
| `calibration_version` | text |  | `risk_event.calibration_version` |  |
| `scene_version` | text |  | `risk_event.scene_version` |  |
| `raised_at` | ts |  | `risk_event.raised_at` |  |
| `updated_at` | ts |  | `risk_event.updated_at` |  |
| `resolved_at` | ts |  | `risk_event.resolved_at` |  |
| `delivery` | text |  | `risk_event.delivery` | REALTIME·DELAYED_ACTIVE·AUDIT_ONLY |
| `related_event_id` | uuid | FK>risk_event.event_id | `risk_event.related_event_id` |  |
| `resolution` | jsonb |  | `risk_event.resolution` | 운영자 종료(정본은 event_close) |
| `evidence_snapshot` | jsonb |  |  | 근거 스냅샷(11.3) |
| `retention_hold` | boolean | NN |  | 보존 표시(11.3) |

#### `risk_event_update` — Tier 0 전이 기록(추가만). (event_id, event_sequence) 로 중복 제거

- 단계 **MVP** · 일반 · 근거: 11.3, risk_event_transition(I1), 3.2 저널 · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `event_id` | uuid | PK(1), FK>risk_event.event_id | `risk_event_transition.event_id` |  |
| `event_sequence` | int | PK(2) | `risk_event_transition.event_sequence` |  |
| `msg_id` | uuid | UQ, NN | `envelope.msg_id` |  |
| `gateway_id` | text | NN, UQ(journal) | `risk_event_transition.gateway_id` |  |
| `journal_offset` | bigint | NN, UQ(journal) | `risk_event_transition.journal_offset` |  |
| `transition` | text | NN, CK(RAISED·UPDATED·RESOLVED·ACKNOWLEDGED) | `risk_event_transition.transition` |  |
| `occurred_at` | ts | NN | `risk_event_transition.occurred_at` |  |
| `observation_at` | ts | NN | `risk_event_transition.observation_time` |  |
| `evaluated_at` | ts | NN | `risk_event_transition.evaluated_at` |  |
| `rule_id` | text | NN | `risk_event_transition.rule_id` |  |
| `rule_version` | int | NN | `risk_event_transition.rule_version` |  |
| `config_version` | text | NN | `risk_event_transition.config_version` |  |
| `calibration_version` | text | NN | `risk_event_transition.calibration_version` |  |
| `scene_version` | text | NN | `risk_event_transition.scene_version` |  |
| `source` | text | NN, CK(LIVE) | `risk_event_transition.source` |  |
| `severity` | text | NN | `risk_event_transition.severity` |  |
| `confidence` | text | NN | `risk_event_transition.confidence` |  |
| `verification` | text | NN | `risk_event_transition.verification` |  |
| `needs_check` | boolean | NN | `risk_event_transition.needs_check` |  |
| `confidence_reasons` | text[] |  | `risk_event_transition.confidence_reasons` |  |
| `world_ids` | text[] | NN | `risk_event_transition.world_ids` |  |
| `zone_id` | text |  | `risk_event_transition.zone_id` |  |
| `metrics` | jsonb | NN | `risk_event_transition.metrics` |  |
| `evidence_ref` | uuid |  | `risk_event_transition.evidence_ref` | world_track_state.msg_id 논리 참조 |
| `resent` | boolean | NN | `risk_event_transition.resent` |  |
| `resumed` | boolean | NN | `risk_event_transition.resumed` |  |
| `related_event_id` | uuid |  | `risk_event_transition.related_event_id` |  |
| `ack_id` | uuid | FK>event_ack.ack_id | `risk_event_transition.ack` | ACKNOWLEDGED 일 때 |
| `close_id` | uuid | FK>event_close.close_id | `risk_event_transition.resolution` | 운영자 종료일 때 |
| `escalation_id` | uuid | FK>event_escalation.escalation_id | `risk_event_transition.escalation` | 운영자 상향일 때 |
| `delivery_mode` | text | NN, CK(REALTIME·DELAYED_ACTIVE·AUDIT_ONLY) |  | 재동기화 중 받았는지(11.3 지연 전달·감사 전용) |
| `received_at` | ts | NN |  |  |

#### `event_ack` — 운영자 확인 기록(누가·어디서·어느 상태)

- 단계 **MVP** · 일반 · 근거: 3.2·3.10, operator_ack, event_ack·site_ack · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `ack_id` | uuid | PK | `operator_ack.ack_id, risk_event.acks` |  |
| `event_id` | uuid | FK>risk_event.event_id, NN | `event_ack.event_id` |  |
| `target_event_sequence` | int |  | `event_ack.event_sequence` | 확인한 사람이 본 상태 |
| `channel` | text | NN, CK(CONTROL_ROOM·FOREMAN_PWA·SITE_BUTTON) | `operator_ack.channel` |  |
| `actor_role` | text | NN, CK(ADMIN·FOREMAN·SITE) | `operator_ack.actor_role` |  |
| `actor_user_id` | bigint | FK>app_user.user_id | `operator_ack.actor_id` | 관제 화면·PWA |
| `actor_device_id` | text | FK>device.device_id | `operator_ack.actor_id` | 현장 확인 버튼 |
| `acked_at` | ts | NN | `operator_ack.acked_at` |  |
| `journal_event_sequence` | int |  |  | ACKNOWLEDGED 전이 sequence(저널 기록 뒤 채움) |

#### `event_close` — 관리자의 구역 대상 이벤트 종료 명령과 결과

- 단계 **MVP** · 일반 · 근거: 6.4 운영자 종료, event_close·event_close_result · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `close_id` | uuid | PK | `event_close.close.close_id, event_close_result.close_id` | EC2 가 발급하는 고유 ID. I1 `event_close` 의 멱등 키(Tier 0 는 같은 값을 DUPLICATE 로 답함) |
| `idempotency_key` | text | UQ, NN |  | REST 요청이 보낸 멱등 키(`POST /api/events/{id}/close` 본문). 같은 키로 다시 오면 새 행을 만들지 않고 처음 결과를 돌려준다. `close_id` 와 따로 둔다 |
| `event_id` | uuid | FK>risk_event.event_id, NN | `event_close.event_id, event_close_result.event_id` |  |
| `actor_user_id` | bigint | FK>app_user.user_id, NN | `event_close.close.actor_id` |  |
| `requested_at` | ts | NN | `event_close.close.requested_at` |  |
| `reason` | text | NN | `event_close.close.reason` |  |
| `result` | text | CK(ACCEPTED·DUPLICATE·REJECTED) | `event_close_result.result` | null = 응답 대기 |
| `reject_reason` | text |  | `event_close_result.reject_reason` |  |
| `event_sequence` | int |  | `event_close_result.event_sequence` |  |
| `decided_at` | ts |  | `event_close_result.decided_at` |  |

#### `event_escalation` — 승인한 VLM 상향 의견의 적용 명령과 결과

- 단계 **M3** · 일반 · 근거: 5.4, FR-116, event_escalate·event_escalate_result · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `escalation_id` | uuid | PK | `operator_escalation.escalation_id, event_escalate_result.escalation_id, risk_event.escalations` | 멱등 키 |
| `event_id` | uuid | FK>risk_event.event_id, NN | `event_escalate.event_id, event_escalate_result.event_id` |  |
| `assessment_id` | bigint | FK>vlm_assessment.assessment_id, NN | `operator_escalation.assessment_job_id` | 작업 ID 로 찾은 평가 |
| `basis` | text | NN, CK(VLM_PROPOSAL) | `operator_escalation.basis` |  |
| `target_severity` | text | NN, CK(CRITICAL·HIGH·CAUTION) | `operator_escalation.target_severity` |  |
| `actor_user_id` | bigint | FK>app_user.user_id, NN | `operator_escalation.actor_id` |  |
| `requested_at` | ts | NN | `operator_escalation.requested_at` |  |
| `note` | text |  | `operator_escalation.note` |  |
| `result` | text | CK(ACCEPTED·DUPLICATE·REJECTED) | `event_escalate_result.result` |  |
| `reject_reason` | text |  | `event_escalate_result.reject_reason` |  |
| `event_sequence` | int |  | `event_escalate_result.event_sequence` |  |
| `decided_at` | ts |  | `event_escalate_result.decided_at` |  |

#### `journal_cursor` — Tier 0 인스턴스별 저널 수신 상태

- 단계 **MVP** · 일반 · 근거: 3.2 저널 계약, journal_ack·event_state_snapshot · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `gateway_id` | text | PK | `journal_ack.gateway_id` |  |
| `acked_offset` | bigint | NN | `journal_ack.acked_offset` | 빠짐없이 저장한 마지막 offset |
| `resend_from_offset` | bigint |  | `journal_ack.resend_from_offset` | 빈 구간 재요청 시작 offset |
| `sync_mode` | boolean | NN | `journal_ack.sync_mode` | 재동기화 중(실시간 알림 보류) |
| `last_snapshot_at` | ts |  | `event_state_snapshot.snapshot_at` |  |
| `last_snapshot_offset` | bigint |  | `event_state_snapshot.journal_offset` |  |
| `updated_at` | ts | NN |  |  |

#### `event_frame` — 이벤트 프레임 주소(VLM 입력)

- 단계 **M3** · 일반 · 근거: event_frame_ready(I1), 5.4 VLM 입력 · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `frame_id` | bigint identity | PK |  |  |
| `event_id` | uuid | FK>risk_event.event_id, NN | `event_frame_ready.event_id` |  |
| `camera_id` | text | NN | `event_frame_ready.frames.camera_id` |  |
| `frame_uri` | text | NN | `event_frame_ready.frames.frame_uri` | s3:// 주소 |
| `estimated_capture_at` | ts | NN | `capture_time.estimated_capture_time_ms` |  |
| `timestamp_source` | text | NN | `capture_time.timestamp_source` |  |
| `timestamp_uncertainty_ms` | int | NN | `capture_time.timestamp_uncertainty_ms` |  |
| `received_at` | ts | NN |  |  |

#### `action_ticket` — 조치 티켓(현재 상태)

- 단계 **M2** · 일반 · 근거: 11.3, 8.4, FR-40·41·42, /api/tickets

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `ticket_id` | bigint identity | PK | `action_mismatch.ticket_id` |  |
| `event_id` | uuid | FK>risk_event.event_id, NN | `action_mismatch.event_id` | CRITICAL·HIGH 에서 자동 생성 |
| `status` | text | NN, CK(REQUESTED·CONFIRMED·IN_PROGRESS·DONE) |  | 요청→확인→조치 중→완료 |
| `assignee_user_id` | bigint | FK>app_user.user_id |  | 반장 |
| `photo_key` | text |  |  | 완료 사진 객체 키(11.3 사진 주소) |
| `created_at` | ts | NN |  |  |
| `updated_at` | ts | NN |  |  |
| `completed_at` | ts |  |  |  |
| `mismatch` | boolean | NN | `action_mismatch.cleared` | 완료인데 이벤트 활성 |
| `mismatch_detected_at` | ts |  | `action_mismatch.detected_at` |  |
| `mismatch_cleared_at` | ts |  |  |  |

#### `action_ticket_history` — 티켓 상태 변경 이력

- 단계 **M2** · 일반 · 근거: 11.4-1 추가만

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `history_id` | bigint identity | PK |  |  |
| `ticket_id` | bigint | FK>action_ticket.ticket_id, NN |  |  |
| `from_status` | text |  |  |  |
| `to_status` | text | NN |  |  |
| `actor_user_id` | bigint | FK>app_user.user_id |  | null = 시스템 자동 생성 |
| `photo_key` | text |  |  |  |
| `changed_at` | ts | NN |  |  |

#### `media_clip` — 이벤트 클립 기록(카메라 하나 × 위험 구간 하나)

- 단계 **MVP** · 일반 · 근거: 11.3, 11.5, media_clip(I1) · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `clip_id` | uuid | PK | `media_clip.clip_id` |  |
| `event_id` | uuid | FK>risk_event.event_id, NN, UQ(clip_span) | `media_clip.event_id` |  |
| `gateway_id` | text | NN | `media_clip.gateway_id` |  |
| `event_sequence` | int | NN | `media_clip.event_sequence` |  |
| `resume_count` | int | NN, UQ(clip_span) | `media_clip.resume_count` | (event_id, resume_count, camera_id) |
| `rule_id` | text | NN | `media_clip.rule_id` |  |
| `severity` | text | NN, CK(CRITICAL·HIGH) | `media_clip.severity` |  |
| `source` | text | NN, CK(LIVE) | `media_clip.source` |  |
| `camera_id` | text | NN, UQ(clip_span) | `media_clip.camera_id` |  |
| `stream_epochs` | int[] | NN | `media_clip.stream_epochs` |  |
| `anchor_capture_at` | ts | NN | `media_clip.anchor_capture_ms` |  |
| `requested_start_at` | ts | NN | `media_clip.requested_start_ms` |  |
| `requested_end_at` | ts | NN | `media_clip.requested_end_ms` |  |
| `clip_start_capture_at` | ts | NN | `media_clip.clip_start_capture_ms` |  |
| `clip_end_capture_at` | ts | NN | `media_clip.clip_end_capture_ms` |  |
| `frame_count` | int | NN | `media_clip.frame_count` |  |
| `starts_with_keyframe` | boolean | NN | `media_clip.starts_with_keyframe` |  |
| `truncated_start` | boolean | NN | `media_clip.truncated_start` |  |
| `truncated_end` | boolean | NN | `media_clip.truncated_end` |  |
| `has_gaps` | boolean | NN | `media_clip.has_gaps` |  |
| `content_type` | text | NN | `media_clip.content_type` |  |
| `container` | text | NN | `media_clip.container` |  |
| `size_bytes` | bigint | NN | `media_clip.size_bytes` |  |
| `sha256` | text | NN | `media_clip.sha256` |  |
| `face_redaction` | text | NN, CK(NONE·BLURRED_BEFORE_EXPORT) | `media_clip.face_redaction` |  |
| `redactor_version` | text |  | `media_clip.redactor_version` |  |
| `clip_created_at` | ts | NN | `media_clip.created_at_ms` |  |
| `delete_after` | ts | NN | `media_clip.delete_after_ms` | 14일 |
| `retention_hold` | boolean | NN | `media_clip.retention_hold` | 현재 보존 표시(이력은 media_clip_retention) |
| `object_key` | text | NN | `media_clip.object_key` |  |
| `uploaded_at` | ts | NN | `media_clip.uploaded_at_ms` |  |
| `spooled` | boolean |  | `media_clip.spooled` |  |
| `upload_attempts` | int |  | `media_clip.upload_attempts` |  |
| `received_at` | ts | NN |  |  |
| `deleted_at` | ts |  |  | 수명 주기 삭제 확인 |

#### `media_clip_retention` — 보존 표시 이력(CRITICAL 자동, 관리자 붙이기·떼기)

- 단계 **MVP** · 일반 · 근거: 11.5, media_clip_retention(I1), PUT /api/clips/{id}/retention

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `retention_id` | bigint identity | PK |  |  |
| `clip_id` | uuid | FK>media_clip.clip_id, NN | `media_clip_retention.clip_id` |  |
| `event_id` | uuid | NN | `media_clip_retention.event_id` |  |
| `gateway_id` | text |  | `media_clip_retention.gateway_id` | Tier 0 기록일 때 |
| `resume_count` | int |  | `media_clip_retention.resume_count` |  |
| `severity` | text |  | `media_clip_retention.severity` |  |
| `retention_hold` | boolean | NN | `media_clip_retention.retention_hold` | 관리자는 false(떼기)도 가능 |
| `reason` | text | NN, CK(CRITICAL_AUTO·ADMIN_HOLD·ADMIN_RELEASE) | `media_clip_retention.reason` |  |
| `source` | text |  | `media_clip_retention.source` |  |
| `actor_user_id` | bigint | FK>app_user.user_id |  | 관리자일 때 |
| `issued_at` | ts | NN | `media_clip_retention.issued_at_ms` |  |

#### `clip_access_log` — 클립 열람 기록

- 단계 **MVP** · 일반 · 근거: 14장·11.5 조회 기록

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `access_id` | bigint identity | PK |  |  |
| `clip_id` | uuid | FK>media_clip.clip_id, NN |  |  |
| `user_id` | bigint | FK>app_user.user_id, NN |  |  |
| `accessed_at` | ts | NN |  |  |
| `signed_url_expires_at` | ts | NN |  | 발급한 임시 주소 만료 |

#### `upload_grant` — 임시 업로드 주소 발급 기록(올라온 키 검증)

- 단계 **MVP** · 일반 · 근거: 12.6 /api/uploads/sign

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `grant_id` | bigint identity | PK |  |  |
| `object_key` | text | UQ, NN | `media_clip.object_key` |  |
| `purpose` | text | NN, CK(EVENT_FRAME·EVENT_CLIP·TICKET_PHOTO) |  |  |
| `requested_by_device` | text | FK>device.device_id |  | Tier 0 는 기기 토큰 |
| `requested_by_user` | bigint | FK>app_user.user_id |  | 반장(사진) |
| `issued_at` | ts | NN |  |  |
| `expires_at` | ts | NN |  |  |
| `used_at` | ts |  |  |  |
| `requester` | text | NN, CK(TIER0·DEVICE·USER) |  | 요청 주체 종류. Tier 0 토큰이면 기기·사용자 칸이 비어 이것으로 가른다(V9, S15P31A101-69) |
| `content_type` | text | NN |  | 서명에 묶은 형식(V9) |
| `size_bytes` | bigint | NN |  | 서명에 묶은 크기(V9) |
| `sha256` | text | NN |  | 서명에 묶은 내용 해시, 같은 클립 재발급·올라온 키 검증(V9) |
| `event_id` | uuid |  |  | 이벤트 프레임·클립(V9) |

### F. 알림

10장, 11.3 notification, 12.6 푸시.

#### `notification` — 실제 알림 발송 기록. LIVE 이벤트만

- 단계 **M1** · 일반 · 근거: 11.3, 11.4-3, 10.1, risk_alert(I3)

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `notification_id` | bigint identity | PK |  |  |
| `event_id` | uuid | NN, FK(event_id, source)>risk_event | `risk_alert.event_id` | 복합 FK |
| `source` | text | NN, CK(LIVE) | `risk_alert.source` | 11.4-3 |
| `event_sequence` | int | NN |  | 알린 상태 |
| `channel` | text | NN, CK(WEB_PUSH·WATCH) |  | WATCH 는 확장(C.6) |
| `subscription_id` | bigint | FK>push_subscription.subscription_id |  |  |
| `target_user_id` | bigint | FK>app_user.user_id |  |  |
| `severity` | text | NN, CK(CRITICAL·HIGH) | `risk_alert.severity` |  |
| `summary` | text | NN | `risk_alert.summary` | 규칙 근거 한 줄 |
| `delivery` | text | NN, CK(REALTIME·DELAYED_ACTIVE) | `risk_alert.delivery` |  |
| `observation_at` | ts |  | `risk_alert.observation_time` | 지연 측정 기준 |
| `sent_at` | ts |  |  |  |
| `receipt_at` | ts |  |  | Service Worker 수신 확인 |
| `acked_at` | ts |  |  |  |
| `status` | text | NN, CK(QUEUED·SENT·FAILED·EXPIRED) |  |  |
| `error` | text |  |  |  |

#### `push_subscription` — Web Push 구독

- 단계 **M1** · 일반 · 근거: 10.1, FR-36, /api/push/subscribe

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `subscription_id` | bigint identity | PK |  |  |
| `user_id` | bigint | FK>app_user.user_id, NN |  |  |
| `endpoint` | text | UQ, NN |  |  |
| `p256dh` | text | NN |  |  |
| `auth` | text | NN |  |  |
| `user_agent` | text |  |  |  |
| `created_at` | ts | NN |  |  |
| `revoked_at` | ts |  |  | 해지·만료 |
| `last_success_at` | ts |  |  |  |

#### `test_push` — 시험 푸시(RiskEvent 아님, notification 에 넣지 않음)

- 단계 **M1** · 일반 · 근거: 3.10, FR-140, /api/push/test

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `test_push_id` | bigint identity | PK |  |  |
| `requested_by` | bigint | FK>app_user.user_id, NN |  |  |
| `subscription_id` | bigint | FK>push_subscription.subscription_id |  |  |
| `sent_at` | ts |  |  |  |
| `receipt_at` | ts |  |  |  |

#### `push_receipt` — Service Worker 수신 확인 원본

- 단계 **M1** · 일반 · 근거: 10.1 수신 확인, FR-136, /api/push/receipts

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `receipt_id` | bigint identity | PK |  |  |
| `notification_id` | bigint | FK>notification.notification_id |  | 실제 알림 |
| `test_push_id` | bigint | FK>test_push.test_push_id |  | 시험 푸시. 둘 중 하나만(CK) |
| `subscription_id` | bigint | FK>push_subscription.subscription_id |  |  |
| `received_at` | ts | NN |  | 기기 시각 |
| `reported_at` | ts | NN |  | EC2 수신 시각 |

### G. 분석 워커 작업

I5 작업 대여(12.5). 11장에 없어 추가.

#### `analysis_job` — I5 작업(재할당돼도 같은 job_id)

- 단계 **MVP** · 일반 · 근거: 12.5, job_request·job_callback · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `job_id` | text | PK | `job_request.job_id, job_callback.job_id` |  |
| `job_type` | text | NN | `job_request.job_type, job_callback.job_type` | vlm_rationale·tbm_report·static_reconstruct·calibration_ba·bev_floorplan·incident_summary·hmr_* |
| `priority` | text | NN, CK(VLM_FAST·REPORT·BACKGROUND) | `job_request.priority` |  |
| `status` | text | NN, CK(QUEUED·LEASED·SUCCEEDED·FAILED·TIMED_OUT·CANCELLED) | `job_callback.status` |  |
| `attempt` | int | NN | `job_request.attempt` | 반납은 세지 않음 |
| `max_attempts` | int | NN | `job_request.max_attempts` |  |
| `timeout_ms` | int | NN | `job_request.timeout_ms` |  |
| `input` | jsonb | NN | `job_request.input` |  |
| `result` | jsonb |  | `job_callback.result` |  |
| `error` | jsonb |  | `job_callback.error` |  |
| `event_id` | uuid | FK>risk_event.event_id |  | 이벤트 관련 작업 |
| `requested_by` | bigint | FK>app_user.user_id |  |  |
| `created_at` | ts | NN |  |  |
| `started_at` | ts |  | `job_callback.started_at` |  |
| `finished_at` | ts |  | `job_callback.finished_at` |  |

#### `job_lease` — 작업 대여(한 시점에 유효한 대여 하나)

- 단계 **MVP** · 일반 · 근거: 12.5 대여·연장·반납, job_lease_status · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `lease_id` | uuid | PK | `job_request.lease_id, job_callback.lease_id` |  |
| `job_id` | text | FK>analysis_job.job_id, NN | `job_request.job_id` |  |
| `worker_id` | text | FK>worker.worker_id, NN |  |  |
| `attempt` | int | NN | `job_request.attempt` |  |
| `leased_at` | ts | NN | `job_request.leased_at` |  |
| `lease_expires_at` | ts | NN | `job_request.lease_expires_at` |  |
| `heartbeat_interval_ms` | int | NN | `job_request.heartbeat_interval_ms` |  |
| `last_heartbeat_at` | ts |  |  |  |
| `progress` | real |  | `job_heartbeat.progress` |  |
| `status` | text | NN, CK(ACTIVE·SUBMITTED·EXPIRED·RELEASED·REJECTED) | `job_lease_status.status` |  |
| `reject_reason` | text |  | `job_lease_status.reason` |  |
| `consume_attempt` | boolean |  | `job_callback.error.consume_attempt` | false = 반납 |
| `submitted_at` | ts |  |  |  |

### H. AI 결과·보고서

11.3 vlm_assessment·report, 5.4 RAG.

#### `vlm_assessment` — VLM 근거·사고 구간 요약과 관리자 상향 결정

- 단계 **M3** · 일반 · 근거: 11.3, vlm_rationale 결과, 5.4, FR-86·88·116 · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `assessment_id` | bigint identity | PK |  |  |
| `event_id` | uuid | FK>risk_event.event_id, NN | `vlm_rationale.result.event_id` |  |
| `event_sequence` | int | NN | `vlm_rationale.result.event_sequence` |  |
| `job_id` | text | FK>analysis_job.job_id, UQ, NN |  |  |
| `job_type` | text | NN, CK(vlm_rationale·incident_summary) |  |  |
| `model_version` | text | NN | `vlm_rationale.result.model_version` |  |
| `input_ref` | jsonb |  | `job_request.input` | 프레임·클립 키, face_policy |
| `rule_severity` | text | NN | `vlm_rationale.result.severity` |  |
| `text` | text | NN | `vlm_rationale.result.rationale` | 근거 또는 요약 문장 |
| `clause_ids` | text[] |  | `vlm_rationale.result.clause_ids` |  |
| `proposed_severity` | text |  | `vlm_rationale.result.proposed_severity` | 상향 의견만 |
| `latency_ms` | int |  | `vlm_rationale.result.latency_ms` |  |
| `decision` | text | NN, CK(NONE·PENDING·APPROVED·REJECTED) |  | 상향 의견이 없으면 NONE |
| `decided_by` | bigint | FK>app_user.user_id |  |  |
| `decided_at` | ts |  |  |  |
| `created_at` | ts | NN |  |  |

#### `report` — TBM 보고서·일일 일지

- 단계 **M3** · 일반 · 근거: 11.3 report, 8.3, FR-20·21·122 · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `report_id` | bigint identity | PK |  |  |
| `report_kind` | text | NN, CK(TBM·DAILY) | `job_request.input.report_kind` |  |
| `layout_id` | bigint | FK>layout_plan.layout_id |  | TBM |
| `layout_revision` | int |  |  | 계산한 레이아웃 revision |
| `sim_run_id` | bigint | FK>sim.evaluation_run.run_id |  | PLANNED 계산 |
| `report_date` | date |  |  | DAILY |
| `job_id` | text | FK>analysis_job.job_id |  | tbm_report |
| `status` | text | NN, CK(REQUESTED·GENERATING·DONE·FAILED) |  |  |
| `body` | text |  | `job_callback.result` | LLM 초안 |
| `citations` | text[] |  | `job_callback.result` | 입력에 담긴 조항 ID 만 |
| `llm_model` | text |  |  |  |
| `requested_by` | bigint | FK>app_user.user_id, NN |  |  |
| `requested_at` | ts | NN |  |  |
| `completed_at` | ts |  |  |  |

#### `rag_clause` — 안전 수칙 조항과 임베딩

- 단계 **M3** · 일반 · 근거: 5.4, 3.8 pgvector, FR-20 · 검토: 이연주
- 마이그레이션 `V3__rag_clause.sql`(pgvector 확장 포함), 행은 ai 가 생성한 반복 마이그레이션 `R__rag_clauses.sql`. 수집·분할·임베딩 규칙은 `docs/design/rag-clause-index.md`(S15P31A101-27)

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `clause_id` | text | PK | `vlm_rationale.result.clause_ids` | 예: 산업안전보건기준규칙 제43조. 청크가 여럿이면 clause_id#chunk_index |
| `law_name` | text | NN |  |  |
| `article` | text | NN |  |  |
| `title` | text |  |  |  |
| `body` | text | NN |  |  |
| `embedding` | vector(1024) | NN |  | BAAI/bge-m3 dense, float32, L2 정규화. HNSW 인덱스 vector_cosine_ops |
| `embedding_model` | text | NN |  | BAAI/bge-m3 |
| `embedding_revision` | text | NN |  | 가중치 revision. 문서·질문이 같은 값이어야 한다(운영 색인 생성 전에 고정) |
| `chunk_index` | int | NN, CHECK ≥ 0 |  | 조항을 나눈 순서(0부터). 조 본문이 1,000자를 넘을 때만 항·호 경계로 나눈다 |
| `source_version` | text | NN |  | 조문 시행일(YYYY-MM-DD). 판(MST)은 `R__rag_clauses.sql` 머리 주석 |
| `loaded_at` | ts | NN |  | 행이 바뀔 때만 갱신 |

#### `rag_query_template` — TBM 질문 템플릿과 질문 임베딩

- 단계 **M3** · 일반 · 근거: 5.4(문서·질문 같은 모델 revision·설정), FR-20 · 검토: 이연주
- EC2 Java 는 bge-m3 를 돌리지 않는다. TBM 질의는 PLANNED 결과(규칙 ID·대상 종류)에서 만들므로 템플릿 문장을 문서와 같은 스크립트·revision 으로 미리 임베딩해 두고, EC2(S15P31A101-223)는 키로 벡터를 골라 `rag_clause` 를 검색만 한다(S15P31A101-27 결정 E). 자유 문장 질의(확장 3)는 별도 임베딩 컨테이너로 한다
- 마이그레이션 `V3__rag_clause.sql`, 행은 `R__rag_clauses.sql`

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `template_key` | text | PK |  | `규칙 ID.대상 종류`, 예: `R3.ELEVATED`, `R1.OPENING`, `TBM.GENERAL` |
| `text` | text | NN |  | 질문 문장 |
| `embedding` | vector(1024) | NN |  | `rag_clause.embedding` 과 같은 모델·revision·설정 |
| `embedding_model` | text | NN |  | BAAI/bge-m3 |
| `embedding_revision` | text | NN |  | `rag_clause` 와 같은 값이어야 한다(`R__rag_clauses.sql` 이 확인) |

### I. 계획·시뮬레이션 (sim 스키마)

11.3 "PLANNED·WHAT_IF 결과는 sim.*", 8.3, 부록 C.2·C.4.

#### `sim.evaluation_run` — EC2 Risk Engine 컨테이너 계산 실행(PLANNED·WHAT_IF)

- 단계 **M3** · 일반 · 근거: 11.3, 8.3, FR-19·128, 부록 C.2·C.4 · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `run_id` | bigint identity | PK |  |  |
| `kind` | text | NN, CK(PLANNED·WHAT_IF) | `risk_origin` |  |
| `layout_id` | bigint | FK>layout_plan.layout_id |  | PLANNED(TBM·단독 평가) |
| `layout_revision` | int |  |  |  |
| `trajectory_plan_id` | bigint | FK>sim.trajectory_plan.plan_id |  | 위험 동선(확장 2) |
| `scenario_id` | bigint | FK>sim.what_if_scenario.scenario_id |  | What-if(확장 4) |
| `config_version` | text | FK>config_bundle.config_version |  | 기준 구성 |
| `engine_version` | text | NN |  | Risk Engine 라이브러리 버전(Tier 0 와 동일성, FR-128) |
| `status` | text | NN, CK(RUNNING·DONE·FAILED) |  |  |
| `requested_by` | bigint | FK>app_user.user_id |  |  |
| `requested_at` | ts | NN |  |  |
| `completed_at` | ts |  |  |  |

#### `sim.risk_result` — 계산된 위험 항목(실제 알림 채널로 가지 않음)

- 단계 **M3** · 일반 · 근거: 11.3 sim.*, 7.4 출처 분리 · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `result_id` | bigint identity | PK |  |  |
| `run_id` | bigint | FK>sim.evaluation_run.run_id, NN |  |  |
| `source` | text | NN, CK(PLANNED·WHAT_IF) | `risk_event.source` |  |
| `rule_id` | text | NN |  |  |
| `rule_version` | int | NN |  |  |
| `severity` | text | NN |  | 확률 대신 등급 + 근거 |
| `confidence` | text |  |  |  |
| `target_ref` | text |  |  | 레이아웃 요소·가상 인물·장비 |
| `zone_id` | text |  |  |  |
| `metrics` | jsonb | NN |  | 수평 거리·높이 차 |
| `predicted_at` | ts |  |  | 위험 예측 스팟 시각(확장 2) |
| `position` | geometry(Point,0) |  |  |  |

#### `sim.trajectory_plan` — 시간 구간이 있는 계획 궤적

- 단계 **확장 2** · 일반 · 근거: 부록 C.2 F-01 위험 동선 · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `plan_id` | bigint identity | PK |  |  |
| `site_id` | text | FK>site.site_id, NN |  |  |
| `name` | text | NN |  | 예: 10~12시 카트 A→B |
| `asset_id` | bigint | FK>equipment_asset.asset_id |  |  |
| `waypoints` | jsonb | NN |  | (t, x, y) 목록 |
| `window_start` | ts | NN |  |  |
| `window_end` | ts | NN |  |  |
| `created_by` | bigint | FK>app_user.user_id |  |  |
| `created_at` | ts | NN |  |  |

#### `sim.what_if_scenario` — 과거 로그·계획에서 사람·장비·신호수 위치를 옮긴 시나리오

- 단계 **확장 4** · 일반 · 근거: 부록 C.4 F-03 · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `scenario_id` | bigint identity | PK |  |  |
| `site_id` | text | FK>site.site_id, NN |  |  |
| `base_kind` | text | NN, CK(LOG_WINDOW·LAYOUT) |  |  |
| `base_from` | ts |  |  | 과거 로그 구간 |
| `base_to` | ts |  |  |  |
| `base_layout_id` | bigint | FK>layout_plan.layout_id |  |  |
| `modifications` | jsonb | NN |  | 옮긴 대상과 새 위치 |
| `created_by` | bigint | FK>app_user.user_id |  |  |
| `created_at` | ts | NN |  |  |

#### `sim.visibility_result` — 지점별 가시 카메라 수·안전모 픽셀 크기

- 단계 **확장 4** · 일반 · 근거: 부록 C.4 공통 가시성 엔진 · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `run_id` | bigint | PK(1), FK>sim.evaluation_run.run_id |  |  |
| `cell_id` | int | PK(2) |  | 격자 칸 |
| `cell` | geometry(Polygon,0) | NN |  |  |
| `visible_camera_count` | int | NN |  | 기준: 최소 2대 |
| `min_helmet_px` | real |  |  | 기준: 최소 1대에서 32px 이상 |
| `occluded_by` | jsonb |  |  | 가림 원인(책상·기둥) |

#### `sim.placement_recommendation` — 카메라·신호수·펜스 배치 제안

- 단계 **확장 4** · 일반 · 근거: 부록 C.4 F-04 배치 추천 · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `recommendation_id` | bigint identity | PK |  |  |
| `run_id` | bigint | FK>sim.evaluation_run.run_id, NN |  |  |
| `kind` | text | NN, CK(CAMERA·SIGNALER·FENCE) |  |  |
| `proposal` | jsonb | NN |  | 위치·각도·선 |
| `rationale` | text | NN |  | 근거(최적해 아님) |
| `coverage_before` | jsonb |  |  |  |
| `coverage_after` | jsonb |  |  |  |
| `status` | text | NN, CK(PROPOSED·ACCEPTED·REJECTED) |  | 확정 배치는 F-02 보고서에 들어감 |
| `decided_by` | bigint | FK>app_user.user_id |  |  |
| `decided_at` | ts |  |  |  |

### J. 확장: 에이전틱·공정

부록 C.3·C.5.

#### `agentic_report` — 작업중지 명령서 초안·우회 동선·반장 전달문

- 단계 **확장 3** · 일반 · 근거: 부록 C.3 F-08, FR-31 · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `agentic_report_id` | bigint identity | PK |  |  |
| `event_id` | uuid | FK>risk_event.event_id, NN |  | 고위험 LIVE 이벤트 |
| `job_id` | text | FK>analysis_job.job_id |  | 명령서 초안 작업(job_type 은 확장 3 착수 때) |
| `order_draft` | text | NN |  | 작업중지 명령서 초안 |
| `detour_run_id` | bigint | FK>sim.evaluation_run.run_id |  | F-01 경로 계산 |
| `foreman_message` | text |  |  | 반장 전달문 초안 |
| `status` | text | NN, CK(DRAFT·APPROVED·PUBLISHED·REJECTED) |  | 관리자 승인 후에만 발행 |
| `approved_by` | bigint | FK>app_user.user_id |  |  |
| `approved_at` | ts |  |  |  |
| `published_at` | ts |  |  |  |
| `created_at` | ts | NN |  |  |

#### `operator_qa` — LLM 이 로그 조회 도구로 답한 질의응답

- 단계 **확장 3** · 일반 · 근거: 부록 C.3 운영자 질의응답, 5.4 용도 5 · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `qa_id` | bigint identity | PK |  |  |
| `user_id` | bigint | FK>app_user.user_id, NN |  |  |
| `question` | text | NN |  |  |
| `answer` | text |  |  |  |
| `tool_calls` | jsonb |  |  | 조회한 로그 범위 |
| `llm_model` | text |  |  |  |
| `created_at` | ts | NN |  |  |

#### `work_schedule` — 오늘의 작업·공정을 구역에 연결

- 단계 **확장 5** · 일반 · 근거: 부록 C.5 F-07 공정 연계, FR-34

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `schedule_id` | bigint identity | PK |  |  |
| `site_id` | text | FK>site.site_id, NN |  |  |
| `work_date` | date | NN |  |  |
| `zone_id` | text | NN |  | 구역 ID(버전 무관) |
| `task_name` | text | NN |  | 예: 양중 |
| `days_since_pour` | int |  |  | 타설 후 경과일 |
| `note` | text |  |  | 데이터 확보 전 수동 메모 |
| `source` | text | NN, CK(MANUAL·IMPORT) |  |  |
| `created_by` | bigint | FK>app_user.user_id |  |  |

### K. 확장: 워치

부록 C.6. 11.2·11.3 의 channel_state·watch_feature·watch_binding.

#### `watch_registrant` — 워치 등록자(관리자만 조회)

- 단계 **확장** · 일반 · 근거: 부록 C.6 워치 등록자 이름은 접근 제한 별도 테이블

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `registrant_id` | bigint identity | PK |  |  |
| `device_id` | text | FK>device.device_id, UQ, NN |  | kind = WATCH |
| `registrant_name` | text | NN |  | 접근 제한(관리자만). 보고서에는 "워치 등록자 기준" |
| `helmet_required` | boolean |  |  | 착용 의무 |
| `created_at` | ts | NN |  |  |
| `deleted_at` | ts |  |  |  |

#### `watch_binding` — 익명 World ID ↔ 워치 배정 이력

- 단계 **확장** · 일반 · 근거: 11.3 watch_binding, 부록 C.6 1:1 배정 · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `binding_id` | bigint identity | PK |  |  |
| `device_id` | text | FK>device.device_id, NN |  |  |
| `world_id` | text | NN |  |  |
| `score` | real | NN |  | 움직임 대조 점수 |
| `bound_at` | ts | NN |  |  |
| `unbound_at` | ts |  |  |  |
| `unbind_reason` | text |  |  | ID 병합·분열 의심, 끊김 등 |

#### `channel_state` — World ID 별 개인 채널 상태

- 단계 **확장** · hypertable · 근거: 11.2 channel_state, 부록 C.6 · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `observed_at` | ts | PK(1), NN |  |  |
| `world_id` | text | PK(2) |  |  |
| `state` | text | NN, CK(BOUND·UNBOUND·LOST) |  |  |
| `device_id` | text |  |  |  |
| `confidence` | real |  |  |  |

#### `watch_feature` — 워치 가속도 특징

- 단계 **확장** · hypertable · 근거: 11.2 watch_feature, 부록 C.6 I4 gait_features · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `measured_at` | ts | PK(1), NN |  |  |
| `device_id` | text | PK(2) |  |  |
| `features` | jsonb | NN |  | 걸음·시작/멈춤 시점, 0.5~3 Hz 크기 신호 |
| `received_at` | ts | NN |  |  |

#### `watch_alert` — 워치 진동 알림 상세(notification 의 WATCH 채널)

- 단계 **확장** · 일반 · 근거: 부록 C.6 I4 watch_alert·ack, 격상

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `notification_id` | bigint | PK, FK>notification.notification_id |  |  |
| `device_id` | text | FK>device.device_id, NN |  |  |
| `vibration_pattern` | text | NN, CK(CAUTION_DOUBLE·DANGER_LONG) |  | 주의: 짧게 두 번, 위험: 길게 연속 |
| `direction` | text |  |  | 방향 |
| `word` | text |  |  | 한 단어 |
| `acked_at` | ts |  |  | 워치 ACK |
| `escalated_at` | ts |  |  | ACK 대기 초과 → 현장 경보·관리자 알림 |

#### `watch_impact` — 워치 충격 이벤트(낙상 추가 근거)

- 단계 **확장** · 일반 · 근거: 부록 C.6 워치 충격 감지, FR-47

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `impact_id` | bigint identity | PK |  |  |
| `device_id` | text | FK>device.device_id, NN |  |  |
| `detected_at` | ts | NN |  |  |
| `peak_g` | real | NN |  | 임계값은 확장 착수 때 |
| `linked_event_id` | uuid | FK>risk_event.event_id |  | R4 연계 |
| `received_at` | ts | NN |  |  |

### L. 확장: PPE·정적 공간 변화

부록 C.7·C.8. ppe_state·region_state.

#### `ppe_state` — World ID 별 PPE 판정

- 단계 **확장** · hypertable · 근거: 11.2 ppe_state, 부록 C.7, R9 · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `observed_at` | ts | PK(1), NN |  |  |
| `world_id` | text | PK(2) |  |  |
| `helmet` | text | NN, CK(WORN·NOT_WORN·UNKNOWN) |  |  |
| `vest` | text | CK(WORN·NOT_WORN·UNKNOWN) |  |  |
| `signaler_marker` | boolean |  |  | 신호수 표식 |
| `confidence` | real |  |  |  |
| `model_version` | text | NN |  | PPE 모델(YOLO26) 버전. crop 은 저장하지 않음(11.5) |

#### `region_state` — 정적 공간 변화 영역(임시 형상·불확실 점유)

- 단계 **확장** · 일반 · 근거: 11.2 region_state, 부록 C.8, R10 · 검토: 이연주

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `region_id` | bigint identity | PK |  |  |
| `site_id` | text | FK>site.site_id, NN |  |  |
| `state` | text | NN, CK(TEMPORARY_COLLIDER·UNKNOWN_OCCUPANCY·REMOVAL_PENDING·CLEARED) |  |  |
| `volume` | jsonb | NN |  | 실루엣 원뿔 교집합·가능 점유 볼륨 |
| `consistent_cameras` | int |  |  | 2대 이상 일관성 검사 |
| `detected_at` | ts | NN |  |  |
| `ttl_expires_at` | ts |  |  | 해결 절차 격상 타이머 |
| `escalation_level` | int | NN |  |  |
| `candidate_scene_version` | text | FK>scene_version.scene_version |  |  |
| `operator_check_requested_at` | ts |  |  |  |
| `resolved_at` | ts |  |  |  |

## 4. 컬럼 매핑 전에 정한 것 (2026-10-20)

| 항목 | 결정 |
|---|---|
| 운영자 확인·종료·상향 기록 | 이 정의대로. 전이는 `risk_event_update` 한 곳에 쌓고, 명령·결과는 `event_ack`·`event_close`·`event_escalation` 으로 나누어 전이 행이 FK 로 가리킨다 |
| hypertable | `system_health`·`device_heartbeat` 도 hypertable(확장: `channel_state`·`watch_feature`·`ppe_state`, PPE 는 확장 범위). 11장 첫머리에 반영 |
| 11장에 없던 테이블 | 11장(11.1~11.3, 11.6·11.7 신설)과 부록 C 각 절의 "확장 DB 테이블"에 반영했다(S15P31A101-207, 기획서 v1.3 수정 16) |
| 임베딩 | BAAI/bge-m3 dense **1024차원**, `float32`, L2 정규화, cosine. 문서·질문에 같은 revision·설정. `rag_clause.embedding vector(1024)` + HNSW `vector_cosine_ops`. revision 은 운영 색인 생성 전에 고정 |
| API 표와 부록 C | API 표 v8 에서 "워치 수신 확인"을 I4 WebSocket `ack` 로 고쳤다 |

남은 가정(4단계 검증에서 확인): REPLAY 이벤트는 `risk_event.source` 에 LIVE·REPLAY 만 두고 PLANNED·WHAT_IF 는 `sim.*`, 보관 정책은 TimescaleDB 보관 정책과 `delete_after`, 확장 테이블의 값 이름은 부록 C 문구에서 옮긴 초안이다.

## 5. 검증한 것

- FK 가 가리키는 테이블·컬럼이 모두 이 정의에 있다(자동 검사).
- I1·I5 주요 메시지 17종(`risk_event_transition`, `media_clip`, `health`, `config_ack`, `tier0_endpoint`, `job_request` 등)의 payload 최상위 필드가 모두 어느 컬럼의 출처로 쓰였다(자동 검사, 미반영 0건).
- API 명세 표 v11 의 REST·WS 경로 전부가 이 정의의 테이블로 연결된다(자동 검사, 연결 안 된 경로 0개). `/api/push/vapid-public-key` 는 설정값이라 테이블이 없다.
