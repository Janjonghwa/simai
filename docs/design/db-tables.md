# simAI DB 테이블 정의

S15P31A101-210. 관계 그림은 [`db-erd.md`](db-erd.md), 검증은 [`db-verification.md`](db-verification.md) 에 있다. ERD 1단계(테이블 목록)·2단계(계약 → 컬럼)의 결과다.

- **기준:** 기획서 v1.3 수정 19 11장(데이터)·부록 C, 계약 v5.3.0, API 명세 표 v11(노션, 166행)
- **범위:** 테이블 77개, 컬럼 807개. 확장 테이블도 컬럼까지 썼다. 이 중 V1~V13 으로 만든 테이블은 51개(MVP 40 · M1 9 · M3 `rag_clause`·`rag_query_template`), 컬럼 575개, FK 76개
- **단계별:** MVP 40 · M1 9 · M2 2 · M3 9 · 확장 17 (확장 `watch_connection` 은 V28(S15P31A101-336), `watch_binding`·`watch_alert` 는 V35(S15P31A101-338). M3 `rag_query_template` 은 S15P31A101-27, MVP `config_bundle_file`·`calibration_document`·`system_outage`·`calibration_update_message`·`i1_deferred_message`·`bev_frame_set` 은 V6·V7·V10·V11·V12 에서 추가)
- **작성·검토:** DB 는 EC2 에 있으므로 곽나혁이 작성한다. Tier 0·분석 워커가 채우는 테이블은 이연주가 검토한다("검토" 표시)
- **마이그레이션:** `server/src/main/resources/db/migration/` 의 V1(MVP·M1 테이블 43개), V2(현장·카메라 시드), V3(`rag_clause`·`rag_query_template`, pgvector), V4(저널 세대 `journal_epoch`), V5(`token_hash` 유일, `worker.rotated_at`), V6(`config_bundle_file`·`calibration_document`, `config_bundle.rule_profile`), V7(`system_outage`), V8(`tier0_endpoint` 봉투 칸), V9(`upload_grant` 요청 주체·내용 칸), V10(`calibration_update_message`), V11(`media_clip_retention.msg_id`, `i1_deferred_message`), V12(`bev_frame_set`, `floor_plan` 작업 칸), V13(`notification` 유일 키·`claimed_until`), V14(`avatar_asset`, `upload_grant` 목적 `AVATAR_ASSET`·워커 요청, -325), V15(`video_segment`·`video_access_log`, `upload_grant` 목적 `VIDEO_SEGMENT`), V16(`upload_grant` 목적 `SCENE_GLTF`), `R__rule_versions.sql`(ai 규칙 등록부에서 자동 생성), `R__rag_clauses.sql`(ai RAG 조항·임베딩에서 자동 생성)

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
| 이름 | 사람 이름은 위치·이벤트·작업명·담당 조 기록에 넣지 않는다. 계정은 `user_id`, 기기는 `device_id`(11.4-5, NFR-14). 예외는 관리자만 조회하고 외부 LLM 에 절대 보내지 않는 `report_signature`(TBM 보고서 참석자 전자서명 실명, 8.3·14장, S15P31A101-426)와 확장 `watch_registrant`(C.6)뿐 |
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
| B. 구성·기준 데이터 | `zone` | MVP | 버전 | 위험 구역(CRITICAL·HIGH 2종). 수정은 새 버전 | 있음 | `/api/zones`, `/api/zones/commit`, `/api/zones/{}/versions` |  |
| B. 구성·기준 데이터 | `static_object` | MVP | 버전 | 등록 작업면·정적 객체(Operational Geometry, 고소 작업면은 둘레 HIGH 구역과 `zone_id` 연결) | 수정 16 추가 | `/api/static-objects`, `/api/zones/commit` |  |
| B. 구성·기준 데이터 | `rule_version` | MVP | 버전 | Risk Engine 규칙 임계값 세트. 규칙 등록부에서 자동 생성한 Flyway 시드로 채운다 | 있음 | `/api/rules` | 이연주 |
| B. 구성·기준 데이터 | `config_bundle` | MVP | 버전 | 구성 묶음 발행과 현장 적용 | 있음 | `/api/config-bundles`, `/api/config-bundles/{}` | 이연주 |
| B. 구성·기준 데이터 | `config_bundle_item` | MVP | 일반 | 묶음에 들어간 구역·규칙·형상 버전 목록 | 수정 16 추가 | `/api/config-bundles` |  |
| B. 구성·기준 데이터 | `config_bundle_file` | MVP | 일반 | 구성 묶음이 가리키는 파일 바이트(매니페스트·구성 요소). Tier 0 가 서명 주소로 내려받는다(V6, S15P31A101-120) | V6 추가 | `/api/config-bundles/{}/files/{}` | 이연주 |
| B. 구성·기준 데이터 | `config_ack` | MVP | 일반 | Tier 0 의 구성 묶음 적용·거부 응답 | 수정 16 추가 | `/api/config-bundles/{}`, `/topic/health` |  |
| B. 구성·기준 데이터 | `tier0_endpoint` | MVP | 일반 | Tier 0 Pod 엔드포인트 등록 이력(최신 행을 노트북에 줌) | 수정 16 추가 | `/api/endpoints/tier0` | 이연주 |
| B. 구성·기준 데이터 | `layout_plan` | M3 | 일반 | 가상 레이아웃(개구부·비계·장비 주차 구역) | 있음 | `/api/layouts`, `/api/layouts/{}` |  |
| B. 구성·기준 데이터 | `equipment_asset` | 확장 2 | 일반 | 장비 GLB 와 반경·형상 파라미터 | 있음 | 없음(다른 테이블이 참조하거나 I1·I5 수신으로 채움) | 이연주 |
| C. 보정·설치 | `calibration_version` | MVP | 버전 | 보정 버전(상태·스케일 근거·검증 요약) | 있음 | `/api/calibrations/current`, `/api/calibrations/{}/confirmation` | 이연주 |
| C. 보정·설치 | `calibration_camera` | MVP | 일반 | 보정 버전의 카메라별 K·왜곡·R·t | 수정 16 추가 | 없음(다른 테이블이 참조하거나 I1·I5 수신으로 채움) | 이연주 |
| C. 보정·설치 | `calibration_document` | MVP | 일반 | 보정 문서 원문. 구성 묶음 calibration 구성 요소로 그대로 내려준다(V6, S15P31A101-120) | V6 추가 | `/api/calibrations` | 이연주 |
| C. 보정·설치 | `calibration_run` | M1 | 일반 | 자동 보정 실행 요청(걷기 보정) | 수정 16 추가 | `/api/calibrations` | 이연주 |
| C. 보정·설치 | `calibration_verification` | M1 | 일반 | 키를 아는 사람 3~5곳 검산 결과 | 수정 16 추가 | `/api/calibrations/{}/verifications` | 이연주 |
| C. 보정·설치 | `scale_reference` | M1 | 일반 | 미리 치수를 잰 시연용 안전 장비 목록 | 수정 16 추가 | `/api/scale-references` |  |
| C. 보정·설치 | `calibration_scale_check` | M1 | 일반 | 보정에서 실측 정물별로 잰 크기와 오차 | 수정 16 추가 | 없음(다른 테이블이 참조하거나 I1·I5 수신으로 채움) | 이연주 |
| C. 보정·설치 | `coverage_guide` | MVP | 일반 | 커버리지 계산 결과와 카메라별 안내 문구 | 수정 16 추가 | `/api/cameras/coverage-guides` | 이연주 |
| C. 보정·설치 | `calibration_update_message` | MVP | 일반 | I1 calibration_update 수신 기록(추가만, msg_id 로 재전송 제거). 현재 보정 상태는 LEAST(sent_at, received_at) 가 가장 늦은 것, 같으면 늦게 받은 것(V10, S15P31A101-263) | V10 추가 | `/api/calibrations/current` |  |
| C. 보정·설치 | `floor_plan` | MVP | 일반 | BEV 평면도(보조 화면) | 수정 16 추가 | `/api/floor-plans/current` | 이연주 |
| C. 보정·설치 | `bev_frame_set` | MVP | 일반 | BEV 평면도 입력 프레임 묶음(관리 명령으로 등록, 추가만)(V12, S15P31A101-240) | V12 추가 | 없음(관리 명령 `admin floorplan frames`) | 이연주 |
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
| E. 이벤트·조치 | `i1_deferred_message` | MVP | 일반 | 앞선 메시지를 기다리는 I1 메시지(받은 봉투 그대로, 처리되면 지움)(V11, S15P31A101-261) | V11 추가 | 없음(다른 테이블이 참조하거나 I1·I5 수신으로 채움) |  |
| E. 이벤트·조치 | `event_frame` | M3 | 일반 | 이벤트 프레임 주소(VLM 입력) | 수정 16 추가 | 없음(다른 테이블이 참조하거나 I1·I5 수신으로 채움) | 이연주 |
| E. 이벤트·조치 | `action_ticket` | M2 | 일반 | 조치 티켓(현재 상태) | 있음 | `/api/events/{}`, `/api/tickets`, `/api/tickets/{}` 외 |  |
| E. 이벤트·조치 | `action_ticket_history` | M2 | 일반 | 티켓 상태 변경 이력 | 수정 16 추가 | `/api/tickets/{}` |  |
| E. 이벤트·조치 | `media_clip` | MVP | 일반 | 이벤트 클립 기록(카메라 하나 × 위험 구간 하나) | 있음 | `/api/clips/{}`, `/api/clips/{}/retention`, `/api/events/{}/clips` | 이연주 |
| E. 이벤트·조치 | `media_clip_retention` | MVP | 일반 | 보존 표시 이력(CRITICAL 자동, 관리자 붙이기·떼기) | 있음 | `/api/clips/{}/retention` |  |
| E. 이벤트·조치 | `clip_access_log` | MVP | 일반 | 클립 열람 기록 | 수정 16 추가 | `/api/clips/{}` |  |
| E. 이벤트·조치 | `video_segment` | MVP | 일반 | 상시 영상 조각 기록(14일 뒤 객체 삭제, 행은 남김) | 수정 36 추가 | `/api/videos`, I1 `video_segment` |  |
| E. 이벤트·조치 | `video_access_log` | MVP | 일반 | 상시 영상 열람 기록 | 수정 36 추가 | `/api/videos/{}` |  |
| E. 이벤트·조치 | `upload_grant` | MVP | 일반 | 임시 업로드 주소 발급 기록(올라온 키 검증) | 수정 16 추가 | `/api/uploads/sign` |  |
| F. 알림 | `notification` | M1 | 일반 | 실제 알림 발송 기록. LIVE 이벤트만(넣기·꺼내기는 `NotificationQueue`, 같은 전이·채널·대상 유일 V13, S15P31A101-123) | 있음 | 없음(다른 테이블이 참조하거나 I1·I5 수신으로 채움) |  |
| F. 알림 | `push_subscription` | M1 | 일반 | Web Push 구독 | 수정 16 추가 | `/api/push/subscribe` |  |
| F. 알림 | `test_push` | M1 | 일반 | 시험 푸시(RiskEvent 아님, notification 에 넣지 않음) | 수정 16 추가 | `/api/push/test` |  |
| F. 알림 | `push_receipt` | M1 | 일반 | Service Worker 수신 확인 원본 | 수정 16 추가 | `/api/push/receipts` |  |
| G. 분석 워커 작업 | `analysis_job` | MVP | 일반 | I5 작업(재할당돼도 같은 job_id, `tbm_report` 는 폐기 예정·EC2 컨테이너로 이전) | 수정 16 추가 | `/api/events/{}/summary` 외 | 이연주 |
| G. 분석 워커 작업 | `job_lease` | MVP | 일반 | 작업 대여(한 시점에 유효한 대여 하나) | 수정 16 추가 | `/api/worker/jobs/lease`, `/api/worker/jobs/{}/heartbeat`, `/api/worker/jobs/{}/result` | 이연주 |
| H. AI 결과·보고서 | `vlm_assessment` | M3 | 일반 | VLM 근거·사고 구간 요약과 관리자 상향 결정 | 있음 | `/api/events/{}`, `/api/events/{}/escalation`, `/api/events/{}/summary` | 이연주 |
| H. AI 결과·보고서 | `report` | M3 | 일반 | TBM 보고서·일일 일지(`subgraph`·`steps`·작업 마무리 `wrap_up_summary`·잠금 `locked_at`·내용 해시 `content_sha256` 포함, EC2 Python 컨테이너 생성) | 있음 | `/api/reports/daily`, `/api/reports/tbm`, `/api/reports/tbm/{}` | 이연주 |
| H. AI 결과·보고서 | `report_signature` | M3 | 일반 | TBM 보고서 참석자 전자서명(실명·손글씨 이미지·버전 잠금 해시, 관리자 전용·외부 LLM 미전송, NFR-14 예외) | 수정 52 추가(후속 구현) | `/api/reports/tbm/{}/signatures` | 곽나혁 |
| H. AI 결과·보고서 | `rag_clause` | M3 | 일반 | 안전 수칙 조항과 임베딩(후속 구현에서 `rag_node` 로 이관·흡수, S15P31A101-415) | 수정 16 추가 | `/api/reports/tbm` | 이연주 |
| H. AI 결과·보고서 | `rag_source` | M3 | 일반 | 법령 지식 그래프 원문 출처·버전(MST·시행일·원문 해시) | 수정 49 추가(후속 구현) | `/api/reports/tbm`, `/api/reports/daily` | 이연주 |
| H. AI 결과·보고서 | `rag_node` | M3 | 일반 | 건설안전 법령 지식 그래프 노드(법령·지침·사례·개념)와 `Qwen3-Embedding-0.6B` 임베딩 | 수정 49 추가(후속 구현) | `/api/reports/tbm`, `/api/reports/daily` | 이연주 |
| H. AI 결과·보고서 | `rag_edge` | M3 | 일반 | 건설안전 법령 지식 그래프 엣지(상하위·참조·위임·지침·사례·방호조치) | 수정 49 추가(후속 구현) | `/api/reports/tbm`, `/api/reports/daily` | 이연주 |
| H. AI 결과·보고서 | `rag_query_template` | M3 | 일반 | TBM 질문 템플릿과 질문 임베딩 | S15P31A101-27 추가 | `/api/reports/tbm` | 이연주 |
| H. AI 결과·보고서 | `work_unit` | M3 | 일반 | 작업 관리(`PLANNED`→`IN_PROGRESS`→`DONE`, 실제 시작/종료 시각, 히스토리 다시 보기) | V21 추가, 수정 52 확장 | `/api/work-units`, `/api/work-units/{}`, `/api/work-units/{}/start`, `/api/work-units/{}/end` | 곽나혁 |
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
| K. 확장: 워치 | `watch_binding` | 확장 | 일반 | 익명 World ID ↔ 워치 배정 이력(V35, 지금은 관리자 임시 수동 배정) | 있음 | `/api/watch-bindings` | 곽나혁 |
| K. 확장: 워치 | `channel_state` | 확장 | hypertable | World ID 별 개인 채널 상태 | 있음 | 없음(다른 테이블이 참조하거나 I1·I5 수신으로 채움) | 곽나혁 |
| K. 확장: 워치 | `watch_feature` | 확장 | hypertable | 워치 가속도 특징 | 있음 | `I4 gait_features` | 곽나혁 |
| K. 확장: 워치 | `watch_connection` | 확장 | 일반 | 워치 I4 연결 기록(연결·끊김 경계, 끝 사유). 열린 연결은 워치마다 하나(V28, S15P31A101-336) | V28 추가 | `I4 /ws/watch`, `/topic/health`(watches) | 곽나혁 |
| K. 확장: 워치 | `watch_alert` | 확장 | 일반 | 워치 진동 알림과 ACK·격상(V35, LIVE 만) | 수정 16 추가 | `I4 watch_alert`·`watch_ack`, `/topic/watch`(watch_escalation) | 곽나혁 |
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
| `token_hash` | text | UQ, NN |  | 기기마다 따로 발급. 요청마다 이 값으로 찾고 두 기기가 같은 해시를 갖지 않게 유일(V5, S15P31A101-222) |
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
| `token_hash` | text | UQ, NN |  | 요청마다 이 값으로 찾는다(V5, S15P31A101-222) |
| `job_types` | text[] | NN | `job_lease_request.job_types` | 마지막 대여 요청의 처리 가능 유형 |
| `issued_at` | ts | NN |  |  |
| `revoked_at` | ts |  |  |  |
| `last_seen_at` | ts |  |  |  |
| `rotated_at` | ts |  |  | 토큰 회전(14장). 회전해도 issued_at 은 처음 발급 시각(V5, S15P31A101-222) |

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
| `zone_kind` | text | NN |  | 유형(개구부·고소 작업면 둘레·장비 주차·양중 하부 등. 단순 표시용 "작업구역"은 두지 않음, S15P31A101-426) |
| `eval_type` | text | NN, CK(GROUND_FOOTPRINT·VOLUME_3D) |  | 8.2 |
| `polygon` | geometry(Polygon,0) | NN |  | World XY(m), SRID 0 로컬 좌표. 저장·조회만(판정은 Risk Engine) |
| `z_min_m` | real |  |  | VOLUME_3D 높이 범위 |
| `z_max_m` | real |  |  |  |
| `severity` | text | NN, CK(CRITICAL·HIGH·CAUTION·INFO) |  | 신규 생성·편집 선택지는 `CRITICAL`(빨강)·`HIGH`(주황) 2종(S15P31A101-426, 기존 행·스키마 호환을 위해 DB CK 허용값은 유지하되 API 쓰기에서 `CRITICAL`·`HIGH` 만 허용. 이벤트 등급 `CAUTION`·`INFO` 와 별개) |
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
| `zone_id` | text | UQ(지금 버전 중) |  | 작업면이 속한 구역 ID(버전 무관, V22). 한 구역에 지금 작업면 하나, 장애물은 null |
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
| `rule_profile` | text | CK(OPERATION·CLASSROOM_TEST) |  | 묶음에 넣은 규칙 묶음(7.4 운영·교실 시험 프로필). 다음 발행의 기본값(V6, S15P31A101-120) |

#### `config_bundle_item` — 묶음에 들어간 구역·규칙·형상 버전 목록

- 단계 **MVP** · 일반 · 근거: 3.2 구성 묶음 = 구역·규칙·형상 버전

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `config_version` | text | PK(1), FK>config_bundle.config_version |  |  |
| `item_type` | text | PK(2), CK(ZONE·RULE·STATIC_OBJECT) |  |  |
| `item_id` | text | PK(3) |  | zone_id·rule_id·object_id |
| `item_version` | int | NN |  |  |

#### `config_bundle_file` — 구성 묶음이 가리키는 파일 바이트(매니페스트·구성 요소)

- 단계 **MVP** · 일반 · 근거: 3.2 구성 묶음, 11.1, GET /api/config-bundles/{}/files/{} (V6, S15P31A101-120)

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `config_version` | text | PK(1), FK>config_bundle.config_version |  |  |
| `name` | text | PK(2), CK(manifest·zones·rules·calibration·operational_geometry) |  | 매니페스트 또는 구성 요소 이름 |
| `content` | bytea | NN |  | EC2 가 발행한 바이트 그대로(같은 주소는 늘 같은 바이트). Tier 0 가 서명 주소로 내려받아 해시를 검사한다 |
| `sha256` | text | NN |  |  |

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

- 단계 **M3** · 일반 · 근거: 11.1 layout_plan, 8.3, 12.6 /api/layouts · V23(S15P31A101-30, `docs/design/server-layouts.md`)

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `layout_id` | bigint identity | PK | `risk_plan_request.layout.layout_id` |  |
| `site_id` | text | FK>site.site_id, NN |  |  |
| `name` | text | NN |  |  |
| `items` | jsonb | NN |  | 배치 요소 `[{item_id, kind(OPENING·SCAFFOLD·EQUIPMENT), center[3], size[3], yaw_rad, label}]`(관제 앱 PLANNED OBB). revision 마다의 사본은 `layout_plan_revision`(layout_id, revision PK, name, items, created_by, created_at, 추가만) |
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

#### `calibration_document` — 보정 문서 원문

- 단계 **MVP** · 일반 · 근거: contracts/providers/calibration, 구성 묶음 calibration 구성 요소, POST /api/calibrations (V6, S15P31A101-120)

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `calibration_version` | text | PK, FK>calibration_version.calibration_version |  |  |
| `content` | bytea | NN |  | 보정 문서 원문. 구성 묶음 calibration 구성 요소로 그대로 내려준다. Tier 0 → EC2 전달 길이 없어 관리자가 올린다 |
| `sha256` | text | NN |  |  |
| `uploaded_by` | bigint | FK>app_user.user_id |  |  |
| `uploaded_at` | ts | NN |  |  |

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

#### `calibration_update_message` — I1 calibration_update 수신 기록(추가만)

- 단계 **MVP** · 일반 · 근거: calibration_update(I1), 12.1 재전송 제거, GET /api/calibrations/current (V10, S15P31A101-263)

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `msg_id` | uuid | PK | `envelope.msg_id` | 재전송 제거 |
| `gateway_id` | text | NN | `envelope.source_id` |  |
| `calibration_version` | text | FK>calibration_version.calibration_version, NN | `calibration_update.calibration_version` | DRIFTED 는 새 버전을 만들지 않고 같은 버전의 상태만 바꾼다 |
| `calibration_state` | text | NN, CK(UNCALIBRATED·PROVISIONAL·CALIBRATED·DRIFTED) | `calibration_update.calibration_state` |  |
| `reason` | text | NN | `calibration_update.reason` |  |
| `sent_at` | ts | NN | `envelope.sent_at` | Tier 0 시계. 현재 상태는 LEAST(sent_at, received_at) 가 가장 늦은 것, 같으면 늦게 받은 것 |
| `received_at` | ts | NN |  |  |
| `payload` | jsonb | NN | `calibration_update` | 받은 payload 원문(카메라별 운영 중 재투영 오차 포함) |

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
| `frame_set_id` | bigint | FK>bev_frame_set.frame_set_id, UQ(calibration_version, frame_set_id) |  | 입력 프레임 묶음. 같은 보정·묶음은 한 번만 등록(V12, S15P31A101-240) |
| `result` | jsonb |  | `job_callback.result` | 결과 원문(픽셀 ↔ World `raster`·방 외곽 등, V12) |
| `error` | text |  |  | FAILED 이유(V12) |
| `finished_at` | ts |  |  | READY·FAILED 시각(V12) |

#### `bev_frame_set` — BEV 평면도 입력 프레임 묶음

- 단계 **MVP** · 일반 · 근거: 12.5 bev_floorplan input.frames, S15P31A101-240(관리 명령 `admin floorplan frames`, 사용자 결정 2026-10-27)

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `frame_set_id` | bigint identity | PK |  |  |
| `site_id` | text | FK>site.site_id, NN |  |  |
| `frame_source` | text | NN, CK(LIVE·FILE·SIM·DATASET) | `bev_floorplan.input.frame_source` |  |
| `frames` | jsonb | NN | `bev_floorplan.input.frames` | 카메라별 사람 없는 프레임(s3://)·촬영 시각. 추가만, 가장 늦은 묶음을 쓴다 |
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

#### `system_outage` — 장애 기록(관제 정지·현장 장비 응답 없음·원격 전달 단절)

- 단계 **MVP** · 일반 · 근거: 6.5, FR-11, G3 단절 감지 시간 (V7, S15P31A101-122)

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `outage_id` | bigint identity | PK |  |  |
| `kind` | text | NN, CK(CONTROL_HALTED·DEVICE_DOWN·REMOTE_LINK_LOST) |  |  |
| `last_seen_at` | ts |  |  | 실패한 쪽의 마지막 정상 근거(하트비트·health 수신, 처리 진행). 모르면 null |
| `detected_at` | ts | NN |  | EC2 가 장애로 판정한 시각(받는 쪽 단조 시계로 3회 누락) |
| `ended_at` | ts | CHECK(end) |  | 열린 장애는 하나(`ended_at IS NULL` 부분 유일 인덱스) |
| `end_reason` | text | CK(RECOVERED·CHANGED·EC2_RESTART), CHECK(end) |  | end: ended_at 과 end_reason 은 함께 채우거나 함께 비운다 |
| `details` | jsonb |  |  |  |

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

- 지표 이름·단위와 출처 칸은 `server-system-metrics.md`(S15P31A101-70). Tier 0 지표는 health 를 받을 때, EC2 지표(`source_id = ec2`)는 10초마다 쓴다. 백분위 지표의 `value` 는 p95.

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
| `journal_epoch` | int | NN, UQ(journal) |  | 저널 세대. 기본 0. Tier 0 저널이 새로 시작하면(offset 0 부터) EC2 가 올린다. 유일 키가 (gateway_id, journal_offset) 에서 (gateway_id, journal_epoch, journal_offset) 으로 바뀌었다(V4, S15P31A101-106) |
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
| `journal_epoch` | int | NN |  | 지금 받는 저널 세대. 기본 0(V4, S15P31A101-106, docs/design/server-event-journal.md) |

#### `i1_deferred_message` — 앞선 메시지를 기다리는 I1 메시지

- 단계 **MVP** · 일반 · 근거: media_clip·media_clip_retention(I1), 12.1 (V11, S15P31A101-261)

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `msg_id` | uuid | PK | `envelope.msg_id` |  |
| `msg_type` | text | NN | `envelope.msg_type` |  |
| `envelope` | jsonb | NN | `envelope` | 받은 봉투 그대로. 이벤트 전이보다 먼저 온 클립, 클립보다 먼저 온 보존 기록을 두었다가 다시 처리하고, 처리되면 지운다 |
| `reason` | text | NN |  | 미룬 이유 |
| `received_at` | ts | NN |  | EC2 가 처음 받은 시각. 저장할 때도 이 시각을 받은 시각으로 쓴다 |
| `attempts` | int | NN |  | 기본 0 |
| `next_attempt_at` | ts | NN |  |  |

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
| `ticket_id` | bigint identity | PK |  |  |
| `event_id` | uuid | FK>risk_event.event_id, NN |  | CRITICAL·HIGH 에서 자동 생성 |
| `status` | text | NN, CK(REQUESTED·CONFIRMED·IN_PROGRESS·DONE) |  | 요청→확인→조치 중→완료 |
| `assignee_user_id` | bigint | FK>app_user.user_id |  | 반장 |
| `photo_key` | text |  |  | 완료 사진 객체 키(11.3 사진 주소) |
| `created_at` | ts | NN |  |  |
| `updated_at` | ts | NN |  |  |
| `completed_at` | ts |  |  |  |
| `mismatch` | boolean | NN |  | 미사용: 조치 불일치 알림 취소·계약 `action_mismatch` 삭제(S15P31A101-411), 티켓 정리 때 함께 제거 |
| `mismatch_detected_at` | ts |  |  | 미사용(위와 같음) |
| `mismatch_cleared_at` | ts |  |  | 미사용(위와 같음) |

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
| `deleted_at` | ts |  |  | 클립 삭제 작업이 객체를 지운 시각(S15P31A101-72, 수명 주기 규칙은 쓰지 않음) |

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
| `msg_id` | uuid | UQ | 봉투 `msg_id` | Tier 0 기록의 재전송 제거(V11, S15P31A101-261). 관리자 기록은 비움 |

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
| `requester` | text | NN, CK(TIER0·DEVICE·USER), CHECK(requester_ref) |  | 요청 주체 종류. Tier 0 토큰이면 기기·사용자 칸이 비어 이것으로 가른다(V9, S15P31A101-69). requester_ref: DEVICE 이면 requested_by_device 만, USER 이면 requested_by_user 만 채운다(V9) |
| `content_type` | text | NN |  | 서명에 묶은 형식(V9) |
| `size_bytes` | bigint | NN, CHECK > 0 |  | 서명에 묶은 크기(V9) |
| `sha256` | text | NN, CHECK 소문자 16진수 64자 |  | 서명에 묶은 내용 해시, 같은 클립 재발급·올라온 키 검증(V9) |
| `event_id` | uuid |  |  | 이벤트 프레임·클립(V9) |

### F. 알림

10장, 11.3 notification, 12.6 푸시.

#### `notification` — 실제 알림 발송 기록. LIVE 이벤트만

- 단계 **M1** · 일반 · 근거: 11.3, 11.4-3, 10.1, risk_alert(I3)

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `notification_id` | bigint identity | PK |  |  |
| `event_id` | uuid | NN, FK(event_id, source)>risk_event, UQ(transition_target) | `risk_alert.event_id` | 복합 FK. transition_target: 같은 전이·채널·대상에 한 번(NULLS NOT DISTINCT, V13, S15P31A101-123) |
| `source` | text | NN, CK(LIVE) | `risk_alert.source` | 11.4-3 |
| `event_sequence` | int | NN, UQ(transition_target) |  | 알린 상태 |
| `channel` | text | NN, CK(WEB_PUSH·WATCH), UQ(transition_target) |  | WATCH 는 확장(C.6) |
| `subscription_id` | bigint | FK>push_subscription.subscription_id, UQ(transition_target) |  |  |
| `target_user_id` | bigint | FK>app_user.user_id, UQ(transition_target) |  |  |
| `severity` | text | NN, CK(CRITICAL·HIGH) | `risk_alert.severity` |  |
| `summary` | text | NN | `risk_alert.summary` | 규칙 근거 한 줄 |
| `delivery` | text | NN, CK(REALTIME·DELAYED_ACTIVE) | `risk_alert.delivery` |  |
| `observation_at` | ts |  | `risk_alert.observation_time` | 지연 측정 기준 |
| `sent_at` | ts |  |  |  |
| `receipt_at` | ts |  |  | Service Worker 수신 확인 |
| `acked_at` | ts |  |  |  |
| `status` | text | NN, CK(QUEUED·SENT·FAILED·EXPIRED) |  |  |
| `error` | text |  |  |  |
| `claimed_until` | ts |  |  | 발송 워커 임대 기한(QUEUED 일 때만 뜻). 비었거나 지났으면 다시 꺼낼 수 있다(V13, S15P31A101-123) |

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
| `job_type` | text | NN | `job_request.job_type, job_callback.job_type` | vlm_rationale·vlm_zone_draft(초기 세팅 VLM 위험구역 초안, S15P31A101-426)·static_reconstruct·scene_regenerate(수동 맵 재생성, S15P31A101-427·-428)·calibration_ba·bev_floorplan·incident_summary·hmr_* (`tbm_report` 는 폐기 예정, S15P31A101-415) |
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
| `event_observed_at` | ts | NN | `job_request.input.observation_time` | G7 기준 관측 시각(V18) |
| `created_at` | ts | NN |  | EC2 저장 시각 |
| `event_to_rationale_ms` | bigint | NN |  | G7: 관측 시각 → EC2 저장(V18) |
| `within_deadline` | boolean | 생성 칸 |  | vlm_rationale 이 관측 + 5000 ms 안에 저장됐는지(`event_to_rationale_ms ≤ 5000`), incident_summary 는 null. 늦은 성공도 SUCCEEDED 로 저장하므로 마감 판정은 이 칸으로 센다(V25, S15P31A101-408) |

#### `report` — TBM 보고서·일일 일지

- 단계 **M3** · 일반 · 근거: 11.3 report, 8.3, FR-20·21·122 · 검토: 이연주
- S15P31A101-415(`docs/design/tbm-legal-graph.md`): TBM·일일 보고서 생성을 RunPod 분석 워커(I5 `tbm_report`, 폐기 예정)에서 EC2 Python 컨테이너로 옮기고, 보고서 단위를 TBM 작업(`work_unit`) 하나당 보고서(재생성 시 덮어쓰지 않고 `report_version` 새 행 추가)로 정하며, 사용한 법령 지식 그래프 서브그래프(`subgraph`)와 4단계 생성·검증 실행 기록(`steps`)을 함께 저장한다(컬럼 추가는 후속 구현에서 마이그레이션)
- S15P31A101-33(`docs/design/daily-report.md`, V36): 일일 일지(DAILY)는 작업 없이 현장·날짜(`site_id`·`report_date`)에 묶는다. CK `report_kind_target_ck`(TBM 은 `work_id` 필수·`report_date` NULL, DAILY 는 `work_id` NULL·`site_id`·`report_date` 필수), 부분 UQ `(site_id, report_date, report_version)`·생성 중 하나 `(site_id, report_date) WHERE status = 'GENERATING'`
- S15P31A101-426(`docs/design/work-session-flow.md`): 작업 종료(`POST /api/work-units/{id}/end`) 시 해당 작업 진행 구간·구역의 실제 LIVE `risk_event` 집계 사실과 외부 LLM 이 작성한 "작업 마무리" 문단(`wrap_up_summary`)을 채워 새 `report_version` 으로 저장한다. 참석자 첫 전자서명 등록 시 해당 버전 본문의 정규화 해시(`content_sha256`)와 잠금 시각(`locked_at`)을 고정하며, 잠긴 뒤 수정·마무리 추가는 새 버전(`report_version + 1`)으로 저장해 재서명받는다(컬럼 추가는 후속 구현에서 마이그레이션)

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `report_id` | bigint identity | PK |  |  |
| `report_kind` | text | NN, CK(TBM·DAILY) | `job_request.input.report_kind` |  |
| `work_id` | bigint | FK>work_unit.work_id |  | TBM 대상 작업(`work_unit` 하나에 보고서, 후속 구현에서 추가) |
| `report_version` | int | NN, CK(>= 1) |  | 같은 `work_id`(또는 `report_date`)의 보고서 버전(덮어쓰지 않고 새 행 추가, 후속 구현에서 추가) |
| `work_type` | text |  |  | 생성 시점의 공종 코드(비개인정보, 법령 그래프 검색·LLM 작성 입력, 후속 구현에서 추가) |
| `layout_id` | bigint | FK>layout_plan.layout_id |  | TBM |
| `layout_revision` | int |  |  | 계산한 레이아웃 revision |
| `sim_run_id` | bigint | FK>sim.evaluation_run.run_id |  | PLANNED 계산 |
| `report_date` | date | CK(DAILY 면 NN, TBM 이면 NULL) |  | 일일 일지(DAILY)의 날짜, Asia/Seoul 하루(V36, S15P31A101-33) |
| `site_id` | text | FK>site.site_id, CK(DAILY 면 NN) |  | 일일 일지의 현장. TBM 은 `work_unit` 을 거쳐 현장을 찾아 비워 둔다(V36) |
| `events` | jsonb | NN, 기본 `[]` |  | 일일 일지 생성에 넘긴 그날 LIVE `risk_event` 사실(`rule_id`·`severity`·`zone_id`·`raised_at_ms`·`resolved`·`needs_check`, 최대 100건(넘으면 높은 등급부터, 발생 순으로 저장), 사람 ID·근거 문장 없음, V36) |
| `event_summary` | jsonb | NN, 기본 `{}` |  | 일일 일지의 그날 LIVE 이벤트 집계(`event_count`·`resolved_count`·`needs_check_count`·`events_truncated`·`severity_counts`·`rule_counts`, 100건 상한 없이 전부, V36) |
| `job_id` | text | FK>analysis_job.job_id |  | 레거시(`tbm_report` I5 폐기 예정, EC2 Python 컨테이너 직접 생성 시 NULL) |
| `status` | text | NN, CK(REQUESTED·GENERATING·DONE·FAILED) |  |  |
| `body` | text |  | `job_callback.result` | LLM 초안 |
| `citations` | text[] |  | `job_callback.result` | 허용 목록(`allowed_ids`) 안의 조항·노드 ID 만 |
| `subgraph` | jsonb |  |  | 보고서에 사용한 법령 지식 그래프 서브그래프(`nodes[]`·`edges[]`, 시연 Scene 6 그래프 패널용, 후속 구현에서 추가) |
| `steps` | jsonb |  |  | 코드 4단계 생성·검증 파이프라인 실행 기록 배열(`step`·`label`·`status`·`input_count`·`output_count`·`duration_ms`·`detail`, 후속 구현에서 추가) |
| `wrap_up_summary` | jsonb |  |  | 작업 종료(`POST /api/work-units/{id}/end`) 시 실제 LIVE `risk_event` 집계 사실(`wrap_up_facts`: 등급별·규칙별 건수, 확인/종료 건수, 작업 외 진입 건수)과 외부 LLM 이 작성한 "작업 마무리" 문단(`wrap_up_text`), 후속 구현에서 추가 |
| `content_sha256` | text |  |  | 첫 전자서명 등록 시 고정하는 보고서 본문 정규화 해시(`sha256:<64hex>`), 후속 구현에서 추가 |
| `locked_at` | ts |  |  | 첫 전자서명 등록으로 이 `report_version` 이 잠긴 시각. 잠긴 버전은 수정 불가하며 변경 시 새 `report_version` 생성, V29 적용, 역할과 무관한 첫 서명에서 잠금 |
| `llm_model` | text |  |  |  |
| `requested_by` | bigint | FK>app_user.user_id, NN |  |  |
| `requested_at` | ts | NN |  |  |
| `completed_at` | ts |  |  |  |
| `approved_at` | ts |  |  | 관리자 최종 결재 시각, 미결재는 null. 새 버전은 null로 시작 |
| `approved_by` | bigint | FK>app_user.user_id |  | 요청 결재 관리자 계정, approved_at과 함께 기록. 응답 approved_by_name은 display_label(없으면 login_id), 서명 실명과 별도 |

#### `report_signature` — TBM 보고서 참석자 전자서명·관리자 결재

- 단계 **M3** · 일반 · 근거: 8.3, 11.3, 11.4-5(NFR-14 서명 실명 예외), 14장 (`docs/design/work-session-flow.md`, S15P31A101-426) · 검토: 곽나혁
- V29에서 생성하고 V31에서 서명 역할·관리자 최종 결재를 추가한다. TBM 회의록 참석자·승인자의 **실명(`signer_name`)** 과 **손글씨 서명 이미지(`signature_image_key`)** 를 서버 전용으로 저장한다. **관리자(`ADMIN`)만 조회 가능하며 외부 LLM API 나 분석 워커에는 절대 보내지 않는다.** 화면·인쇄본에 `"공인 전자서명 아님, 내부 기록용 전자 서명"` 을 표시한다. 보관 주기는 11.5·18.4 에 따라 프로젝트 발표 후 30일 이내 전체 파기 대상에 포함한다(실제 현장 운영 시 보존 기간은 Lead 확인 필요).

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `signature_id` | bigint identity | PK |  |  |
| `report_id` | bigint | FK>report.report_id, NN |  | 대상 TBM 보고서 |
| `report_version` | int | NN, CK(>= 1) |  | 서명한 보고서 버전. 보고서가 수정되거나 작업 마무리가 붙어 새 버전이 생기면 새 버전에 다시 서명 |
| `signer_role` | text | NN, CK(PARTICIPANT·APPROVER), 기본 PARTICIPANT |  | 기존 행은 PARTICIPANT, APPROVER는 (report_id, report_version) 부분 유일 인덱스로 버전당 1회. 결재 후 두 역할 모두 409 REPORT_APPROVED, 기존 해시 유지 |
| `signer_name` | text | NN |  | 참석자·승인자 실명(1~40자, NFR-14 예외: TBM 전자서명에만 허용, 외부 LLM 미전송) |
| `signature_image_key` | text | NN |  | 비공개 객체 저장소 키(또는 서버 전용 저장 경로, PNG 최대 600×240 px · 256 KiB) |
| `image_sha256` | text | NN |  | 서명 이미지 바이트 해시(`sha256:<64hex>`) |
| `report_content_sha256` | text | NN |  | 서명 시점의 `report.content_sha256` 사본(감사 추적용) |
| `legal_notice_ack` | boolean | NN, CK(= true) |  | `"공인 전자서명 아님, 내부 기록용 전자 서명"` 고지 확인 |
| `recorded_by` | bigint | FK>app_user.user_id, NN |  | 서명을 수집·등록한 요청 관리자 계정. APPROVER는 이 계정 본인의 결재 |
| `signed_at` | ts | NN |  | 서명 등록 시각 |

#### `rag_clause` — 안전 수칙 조항과 임베딩

- 단계 **M3** · 일반 · 근거: 5.4, 3.8 pgvector, FR-20 · 검토: 이연주
- 마이그레이션 `V3__rag_clause.sql`(pgvector 확장 포함), 행은 ai 가 생성한 반복 마이그레이션 `R__rag_clauses.sql`. 수집·분할·임베딩 규칙은 `docs/design/rag-clause-index.md`(S15P31A101-27)
- **개편 관계(S15P31A101-415, `docs/design/tbm-legal-graph.md`):** 후속 구현에서 아래 `rag_node`·`rag_edge` 법령 지식 그래프 테이블(`Qwen/Qwen3-Embedding-0.6B` 1024차원, `pg_trgm` + `pgvector`)로 확장·이관한다. 기존 `rag_clause.clause_id`(`산업안전보건기준규칙 제N조[#k]`)는 `rag_node.node_id`(`layer = 'OSH_STANDARD'`)로 그대로 유지해 `rule_version.legal_basis` 와 호환되며, 전환 뒤 `rag_clause` 는 호환 뷰 또는 정리한다.

#### `rag_source` — 법령 지식 그래프 원문 출처·버전

- 단계 **M3** · 일반 · 근거: 3.8, 5.4, 11.3 (`docs/design/tbm-legal-graph.md`, `docs/design/legal-graph-pipeline.md`, S15P31A101-415·416) · 검토: 이연주
- 후속 구현 마이그레이션에서 생성. 국가법령정보 Open API 로 받은 법령·행정규칙 하나가 한 행이다. 원문은 저장소에 넣지 않고 해시만 둔다.

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `law_key` | text | PK |  | 파이프라인 안정 키(예: `OSHA_STD`) |
| `law_id` | text | NN |  | 법령 ID |
| `law_name` | text | NN |  | 정식 명칭 |
| `layer` | text | NN |  | `rag_node.layer` 와 같은 값 |
| `mst` | text | NN |  | 법령 일련번호(MST) |
| `effective_date` | date | NN |  | 시행일 |
| `promulgation_no` | text |  |  | 공포번호 |
| `source_url` | text | NN |  | 원문 조회 주소(인증키 없음) |
| `raw_sha256` | text | NN |  | 받은 원문 해시 |
| `fetched_at` | ts | NN |  | 수집 시각 |


#### `rag_node` — 건설안전 법령 지식 그래프 노드와 임베딩

- 단계 **M3** · 일반 · 근거: 3.8, 5.4, 8.3, 11.3, FR-20·89 (`docs/design/tbm-legal-graph.md`, S15P31A101-415) · 검토: 이연주
- 후속 구현 마이그레이션에서 생성(`pg_trgm` 확장, `HNSW vector_cosine_ops` 인덱스, `GIN rule_tags`, `GIN (title || ' ' || body) gin_trgm_ops`). 국가법령정보 Open API 법령(법률·시행령·시행규칙·산업안전보건기준에 관한 규칙·중대재해처벌법)과 개념 엔티티를 먼저 적재하고, KOSHA GUIDE·재해사례 요약은 이용 조건 확인 뒤 단계 도입한다. 문서 임베딩 입력 형식은 `"[법령명 조문(제목)] 본문"`.

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `node_id` | text | PK |  | 예: `산업안전보건기준규칙 제43조`, `산업안전보건법 제38조`, `CONCEPT:개구부_방호조치` (`rag_clause.clause_id` 와 같은 형식) |
| `node_kind` | text | NN |  | `LAW`(법령 묶음)·`ARTICLE`(조)·`PARAGRAPH`(항)·`ITEM`(호)·`SUBITEM`(목)·`ANNEX`(별표)·`EXTERNAL_LAW`(카탈로그 밖 인용 법령)·`CONCEPT`(개념 엔티티: 위험요인·설비·방호조치)·`RISK_RULE`(R1~R7). 후속: `GUIDE_SECTION`(KOSHA GUIDE)·`ACCIDENT_CASE`(재해사례) |
| `layer` | text | NN |  | `STATUTE`(법률)·`SAPA`(중대재해처벌법·시행령)·`DECREE`(시행령)·`RULE`(시행규칙)·`OSH_STANDARD`(산업안전보건기준에 관한 규칙)·`ADMIN_RULE`(고시·행정규칙)·`EXTERNAL`·`CONCEPT`·`RISK_RULE`. 후속: `KOSHA_GUIDE`·`ACCIDENT_CASE` |
| `law_key` | text | FK>rag_source.law_key |  | 개념·규칙 노드는 NULL |
| `parent_id` | text | FK>rag_node.node_id |  | 상위 노드(조→항→호). 최상위는 NULL |
| `source_name` | text | NN |  | 법령·문서 정식 명칭(예: `산업안전보건기준에 관한 규칙`). 개념·규칙 노드는 종류 이름 |
| `article_ref` | text |  |  | 조·항·호·별표 표기(예: `제43조`, `제43조 제1항 제2호`, `별표 3`). 법령·개념 노드는 NULL |
| `title` | text | NN |  | 조문·항목 제목 또는 개념 이름(없으면 `article_ref`) |
| `chunk_index` | int | NN, 기본 0, CHECK ≥ 0 |  | 같은 조문 안 분할 순서 |
| `body` | text | NN |  | 정규화된 본문(외부 주소·개인정보 없음) |
| `rule_tags` | text[] | NN, 기본 '{}' |  | 위험 규칙·용도 태그(`R1`~`R7`, `TBM`). GIN 인덱스 |
| `attrs` | jsonb | NN, 기본 '{}' |  | 원문 순번·표시 이름·검수 상태 등 보조 값 |
| `embedding_input` | text |  |  | 임베딩에 넣은 문자열 `"[법령명 조문(제목)] 본문"` |
| `embedding` | vector(1024) |  |  | `Qwen/Qwen3-Embedding-0.6B` float32, L2 정규화. HNSW `vector_cosine_ops`. 임베딩 대상이 아닌 묶음 노드는 NULL |
| `embedding_model` | text |  |  | `Qwen/Qwen3-Embedding-0.6B` (임베딩 있는 행은 NN) |
| `embedding_revision` | text |  |  | 가중치 고정 revision 해시(`rag_query_template` 과 동일 값, 임베딩 있는 행은 NN) |
| `source_version` | text | NN |  | `시행일 YYYY-MM-DD · 공포번호 · MST` 또는 지침 판. 개념·규칙 노드는 사전 버전 |
| `loaded_at` | ts | NN |  | 시드 적재 시각 |

#### `rag_edge` — 건설안전 법령 지식 그래프 엣지

- 단계 **M3** · 일반 · 근거: 3.8, 5.4, 8.3, 11.3 (`docs/design/tbm-legal-graph.md`, S15P31A101-415) · 검토: 이연주
- 후속 구현 마이그레이션에서 생성. 조·항·호 상하위 관계와 `"제N조에 따른"` 참조·위임을 정규식·결정적 파서로 추출해 적재하며, 하이브리드 시드 노드에서 1단계(1-hop) 확장에 쓴다.

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `edge_id` | text | PK |  | `{src_node_id}--{edge_kind}-->{dst_node_id}` |
| `src_node_id` | text | FK>rag_node.node_id, NN, UQ(rag_edge_triple) |  | 출발 노드 ID |
| `dst_node_id` | text | FK>rag_node.node_id, NN, UQ(rag_edge_triple) |  | 도착 노드 ID. 인덱스 `(dst_node_id, edge_kind)` |
| `edge_kind` | text | NN, UQ(rag_edge_triple) |  | `PARENT_CHILD`(상하위)·`REFERENCES`(참조 "제N조에 따른")·`MUTATIS_MUTANDIS`(준용)·`NOTWITHSTANDING`("제N조에도 불구하고")·`CITES_EXTERNAL`(카탈로그 밖 법령 인용)·`DELEGATES`(위임)·`ANNEX_RELATES`(별표 연결)·`HAS_HAZARD`·`INVOLVES_EQUIPMENT`·`REQUIRES_CONTROL`·`MITIGATES`(개념 연결)·`TAGGED_RULE`(위험 규칙 태그). 후속: `GUIDES`(지침 연결)·`ILLUSTRATES`(사례 연결) |
| `method` | text | NN |  | 추출 방법(구조 분해·정규식·사전·명세) |
| `review_status` | text | NN |  | `auto`·`candidate`·`spec`, 사람 검수 반영 뒤 `reviewed`·`rejected`. 검색 확장은 `candidate`·`rejected` 를 쓰지 않는다 |
| `relation_label` | text |  |  | 추출 근거 문구(예: `제13조에 따른 안전난간`) |
| `attrs` | jsonb | NN, 기본 '{}' |  | 보조 값 |
| `loaded_at` | ts | NN |  | 시드 적재 시각 |

#### `rag_clause` (기존 단일 조항 테이블 — 후속 구현에서 `rag_node` 로 이관)

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `clause_id` | text | PK | `vlm_rationale.result.clause_ids` | 예: 산업안전보건기준규칙 제43조. 청크가 여럿이면 clause_id#chunk_index |
| `law_name` | text | NN |  |  |
| `article` | text | NN |  |  |
| `title` | text |  |  |  |
| `body` | text | NN |  |  |
| `embedding` | vector(1024) | NN |  | BAAI/bge-m3 dense, float32, L2 정규화. HNSW 인덱스 vector_cosine_ops |
| `embedding_model` | text | NN |  | `Qwen/Qwen3-Embedding-0.6B` (기존 V3 시드는 `BAAI/bge-m3`, 후속 구현에서 교체) |
| `embedding_revision` | text | NN |  | 가중치 revision. 문서·질문이 같은 값이어야 한다(운영 색인 생성 전에 고정) |
| `chunk_index` | int | NN, CHECK ≥ 0 |  | 조항을 나눈 순서(0부터). 조 본문이 1,000자를 넘을 때만 항·호 경계로 나눈다 |
| `source_version` | text | NN |  | 조문 시행일(YYYY-MM-DD). 판(MST)은 `R__rag_clauses.sql` 머리 주석 |
| `loaded_at` | ts | NN |  | 행이 바뀔 때만 갱신 |

#### `rag_query_template` — TBM 질문 템플릿과 질문 임베딩

- 단계 **M3** · 일반 · 근거: 5.4(문서·질문 같은 모델 revision·설정), FR-20 · 검토: 이연주
- EC2 Java 는 임베딩 모델을 돌리지 않는다. TBM 질의는 규칙·구역 조합별 표준 질의를 문서와 같은 모델·revision 으로 미리 임베딩해 두고(`rag_query_template`), 실시간 질의 임베딩이 필요할 때는 EC2 Python 컨테이너 CPU 에서 수행한다(S15P31A101-415, `docs/design/tbm-legal-graph.md`). 후속 구현에서 임베딩 모델을 `BAAI/bge-m3` 에서 `Qwen/Qwen3-Embedding-0.6B`(1024차원 유지)로 교체한다
- 마이그레이션 `V3__rag_clause.sql`, 행은 `R__rag_clauses.sql`

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `template_key` | text | PK |  | `규칙 ID.대상 종류`, 예: `R3.ELEVATED`, `R1.OPENING`, `TBM.GENERAL` |
| `text` | text | NN |  | 질문 문장 |
| `embedding` | vector(1024) | NN |  | `rag_clause.embedding` 과 같은 모델·revision·설정 |
| `embedding_model` | text | NN |  | `Qwen/Qwen3-Embedding-0.6B` (기존 V3 시드는 `BAAI/bge-m3`, 후속 구현에서 교체) |
| `embedding_revision` | text | NN |  | `rag_clause` 와 같은 값이어야 한다(`R__rag_clauses.sql` 이 확인) |

#### `work_unit` — 작업 관리(TBM 계획·진행·종료 및 히스토리 다시 보기)

- 단계 **M3** · 일반 · 근거: 히스토리 작업 다시 보기, `docs/design/server-work-units.md` · V21
- 작업명·담당 조에는 작업자 개인 이름을 두지 않는다(NFR-14, 참석자 실명은 `report_signature` 에만 허용). `zone_id` 는 구역 ID(버전 무관)라 FK 없이 쓰기 때 존재를 검사한다. 상태는 `PLANNED` → `IN_PROGRESS`(`POST /api/work-units/{id}/start`) → `DONE`(`POST /api/work-units/{id}/end`)으로 전이한다(`docs/design/work-session-flow.md`, S15P31A101-426)

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `work_id` | bigint identity | PK |  | 응답은 글자로 준다 |
| `site_id` | text | FK>site.site_id, NN |  |  |
| `work_date` | date | NN |  | 현장 시각의 작업 날짜 |
| `name` | text | NN |  | 1~100자 |
| `starts_at` | ts | NN |  |  |
| `ends_at` | ts | NN, CK(> starts_at) |  | 24시간 이내(API 검사) |
| `zone_id` | text |  |  | null 이면 구역 밖 작업 |
| `place` | text |  |  | 구역 밖 장소(없으면 응답에 구역 이름) |
| `planned_people` | int | NN, CK(>= 0) |  | 0~100(API 검사) |
| `lead_team` | text |  |  | 담당 조 이름(LLM 에는 보내지 않고 서버가 보고서 머리말에만 붙임) |
| `layout_id` | bigint | FK>layout_plan.layout_id |  | 이 작업이 쓰는 가상 레이아웃(`V23 layout_plan` 연결, 후속 구현에서 추가) |
| `work_type` | text |  |  | 공종 코드(예: `SCAFFOLD_INSTALL`·`OPENING_WORK`·`HIGH_PLACE`·`MATERIAL_HANDLING`·`EQUIPMENT_OP`·`FLOOR_FINISHING`·`GENERAL`, 비개인정보라 LLM·검색 입력 허용, 후속 구현에서 추가) |
| `status` | text | NN, CK(PLANNED·IN_PROGRESS·DONE), 기본 'PLANNED' |  | 작업 상태: `PLANNED`(계획) → `IN_PROGRESS`(진행, 작업 시작) → `DONE`(종료, 작업 종료). 한 시점에 현장당 `IN_PROGRESS` 는 최대 1개(후속 구현에서 추가, S15P31A101-426) |
| `actual_started_at` | ts |  |  | `POST /api/work-units/{id}/start` 실제 작업 시작 시각(예정 `starts_at` 과 별도 기록, 후속 구현에서 추가) |
| `actual_ended_at` | ts | CK(>= actual_started_at) |  | `POST /api/work-units/{id}/end` 실제 작업 종료 시각(예정 `ends_at` 과 별도 기록, 후속 구현에서 추가) |
| `created_by` | bigint | FK>app_user.user_id |  |  |
| `created_at` | ts | NN |  |  |
| `updated_at` | ts | NN |  |  |

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

- 단계 **확장** · 일반 · 근거: 11.3 watch_binding, 부록 C.6 1:1 배정 (V35, S15P31A101-338) · 검토: 곽나혁(워치 트랙 전담)
- 움직임 대조 배정(`MOTION_MATCH`, -340 `WatchMotionMatcher`, `docs/design/server-watch-match.md`)이 기본이고, 관리자 수동 배정(`MANUAL`, `/api/watch-bindings`)은 덮어쓰기다: 수동 배정이 걸린 워치·World ID 는 대조가 건드리지 않는다.

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `binding_id` | bigint identity | PK |  |  |
| `device_id` | text | FK>device.device_id, NN |  | 지금 배정은 워치마다 하나(`unbound_at IS NULL` 부분 유일 인덱스) |
| `world_id` | text | NN |  | 익명 World ID. 지금 배정은 World ID 마다 하나(부분 유일 인덱스) |
| `method` | text | NN, CK(MANUAL·MOTION_MATCH) |  | 배정 방식 |
| `score` | real | NN |  | 움직임 대조 점수. MANUAL 은 1 |
| `bound_by_user_id` | bigint | FK>app_user.user_id, CHECK(manual) |  | 수동 배정한 관리자. MANUAL 이면 필수 |
| `bound_at` | ts | NN |  |  |
| `unbound_at` | ts | CHECK(end) |  |  |
| `unbind_reason` | text | CK(RELEASED·REPLACED·MATCH_LOST·IDENTITY_CHANGED), CHECK(end) |  | 관리자가 풂, 같은 워치·World ID 의 새 배정, 대조 점수가 떨어지거나 트랙이 사라짐(V39), World ID 병합·분할(V39). end: unbound_at 과 함께 채우거나 함께 비운다 |

#### `channel_state` — World ID 별 개인 채널 상태

- 단계 **확장** · hypertable · 근거: 11.2 channel_state, 부록 C.6 · 검토: 곽나혁
- V39(S15P31A101-340). 상태가 바뀐 때만 한 행을 쓴다(10 Hz 마다 쓰지 않는다). 최신 상태는 World ID 의 마지막 행이다.

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `observed_at` | ts | PK(1), NN |  | 바뀐 때의 World 시각(`world_state.t_latest_global`) |
| `world_id` | text | PK(2) |  |  |
| `state` | text | NN, CK(BOUND·UNBOUND·LOST) |  | BOUND 배정·워치 연결, LOST 배정·워치 끊김, UNBOUND 그 밖(워치 없음·미확정·신뢰도 부족·ID 교정 직후) |
| `device_id` | text |  |  | BOUND·LOST 일 때 배정된 워치 |
| `confidence` | real |  |  | 배정 `score`(대조 0~1, 수동 1) |

#### `watch_feature` — 워치 가속도 특징

- 단계 **확장** · hypertable · 근거: 11.2 watch_feature, 부록 C.6 I4 gait_features · 검토: 곽나혁

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `measured_at` | ts | PK(1), NN |  | 창 시작을 EC2 시각으로 바꾼 값(`window_start_ms − clock_offset_ms`, 모르면 받은 시각 − 창 길이). V39(S15P31A101-340) |
| `device_id` | text | PK(2) |  |  |
| `features` | jsonb | NN | `gait_features` payload 그대로 | 걸음 시점·시작/멈춤 전환·0.5~3 Hz 대역 크기 시계열. 시각은 워치 시계(`clock_basis` WATCH)이고 `clock_offset_ms` 로 환산한다(-334) |
| `received_at` | ts | NN |  |  |

#### `watch_alert` — 워치 진동 알림과 ACK·격상

- 단계 **확장** · 일반 · 근거: 부록 C.6 I4 watch_alert·watch_ack, FR-45·46, 격상 (V35, S15P31A101-338; V40 수동 알림 `origin`·`requested_by_user_id`, S15P31A101-475) · 검토: 곽나혁
- 처음 설계는 `notification`(WATCH 채널)의 상세 표였으나, `notification` 은 구독·사용자 대상이고 CRITICAL·HIGH 만 받아(CAUTION 불가) 워치 알림은 따로 둔다.
- V40(-475): 관리자·반장이 `POST /api/watch-alerts` 로 직접 보낸 알림(`origin` MANUAL)도 같은 표에 남긴다. MANUAL 은 위험 이벤트가 없어 `source` NULL(복합 FK 는 MATCH SIMPLE 이라 검사하지 않음), `event_id` 는 그 알림만의 UUIDv7, `world_id` 는 배정이 없으면 NULL. CHECK `watch_alert_origin_columns_check` 가 EVENT(`source`·`world_id` 있음, `requested_by_user_id` 없음)와 MANUAL(`source` 없음, `requested_by_user_id` 있음)을 가른다.
  LIVE 만 받는 두 겹 차단은 같은 방식(source CK + `risk_event(event_id, source)` 복합 FK)으로 건다.

| 컬럼 | 타입 | 키·제약 | 출처 계약 필드 | 설명 |
|---|---|---|---|---|
| `alert_id` | bigint identity | PK |  |  |
| `alert_msg_id` | uuid | UQ, NN | `watch_alert` 봉투 `msg_id` | `watch_ack.alert_msg_id` 가 이 값으로 행을 찾는다(-334) |
| `origin` | text | NN, CK(EVENT·MANUAL), CHECK(origin) |  | EVENT 위험 이벤트로 보냄(-338), MANUAL 관리자·반장이 직접 보냄(V40, -475) |
| `event_id` | uuid | NN, FK(event_id, source)>risk_event | `watch_alert.event_id` | 같은 이벤트(재개마다)·워치·등급에 한 번(부분 UQ `event_id, resume_count, device_id, severity`, SEND_FAILED 제외). MANUAL 은 그 알림만의 UUIDv7(FK 는 source 가 NULL 이라 검사하지 않음) |
| `source` | text | CK(LIVE), CHECK(origin) |  | 11.4-3. EVENT 는 LIVE, MANUAL 은 NULL |
| `requested_by_user_id` | bigint | FK>app_user, CHECK(origin) |  | MANUAL 을 보낸 사용자. EVENT 는 NULL |
| `event_sequence` | int | NN, CK(≥ 1) | `watch_alert.event_sequence` | 알린 이벤트 상태 |
| `resume_count` | int | NN | `risk_event.resume_count` | 알린 때의 재개 횟수. 해소 뒤 다시 활성(resumed)이면 새로 알린다 |
| `device_id` | text | FK>device.device_id, NN |  |  |
| `world_id` | text | CHECK(origin) |  | 보낼 때 그 워치에 배정된 익명 World ID. MANUAL 은 배정이 없으면 NULL |
| `severity` | text | NN, CK(CRITICAL·HIGH·CAUTION) | `watch_alert.severity` | 7.3 워치 개인 채널 |
| `vibration_pattern` | text | NN, CK(DANGER_LONG·CAUTION_DOUBLE·CAUTION_WEAK) | `watch_alert.vibration_pattern` | 위험: 길게 연속, 경고: 짧게 두 번, 주의: 약하게 한 번(FR-45) |
| `direction` | text | CK(FRONT·BACK·LEFT·RIGHT) | `watch_alert.direction` | 작업자 기준 방향. EVENT 는 아직 싣지 않아 null(남은 일), MANUAL 은 보낸 사람이 준 값 |
| `word` | text | NN | `watch_alert.word` | 한 단어(8자 이하, 규칙별) |
| `needs_check` | boolean | NN | `watch_alert.needs_check` |  |
| `ack_within_ms` | integer | NN, CK(1~600000) | `watch_alert.ack_within_ms` | 확인 기한(길이). 격상 판단 = sent_at + 이 값 |
| `sent_at` | ts | NN | `watch_alert` 봉투 `sent_at` | EC2 가 보낸(보내려 한) 시각 |
| `acked_at` | ts |  | `watch_ack.acked_at` (kind VIBRATED) | 워치가 진동을 시작한 시각(워치 시계, 기록용) |
| `ack_received_at` | ts |  | EC2 가 VIBRATED `watch_ack` 를 받은 EC2 시각 | 격상 판단 기준: 이 값이 없거나 `sent_at + ack_within_ms` 뒤면 격상. 워치 시계를 믿지 않는다 |
| `confirmed_at` | ts |  | `watch_ack.acked_at` (kind CONFIRMED) | 작업자가 화면에서 확인한 시각(워치 시계). 없을 수 있다 |
| `confirm_received_at` | ts |  | EC2 가 CONFIRMED 를 받은 EC2 시각 |  |
| `escalated_at` | ts | CHECK(escalation) | `watch_escalation.escalated_at` | 관리자 화면 알림(/topic/watch) |
| `escalation_reason` | text | CK(NO_ACK·SEND_FAILED), CHECK(escalation) | `watch_escalation.reason` | 기한 안에 ACK 없음, 워치로 보내지 못함 |

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
- V1~V13 을 적용한 임시 DB(`timescaledb-ha:pg17-all`)의 테이블·컬럼·타입·NN·PK·FK·UQ·CHECK 와 이 정의를 기계로 대조해 DB 에만 있는 것을 0건으로 맞췄다(2026-10-29, S15P31A101-318). 문서에만 있는 것은 M2·M3·확장 테이블과 그 테이블을 가리키는 `risk_event_update.escalation_id` FK 다.
- I1·I5 주요 메시지 17종(`risk_event_transition`, `media_clip`, `health`, `config_ack`, `tier0_endpoint`, `job_request` 등)의 payload 최상위 필드가 모두 어느 컬럼의 출처로 쓰였다(자동 검사, 미반영 0건).
- API 명세 표 v11 의 REST·WS 경로 전부가 이 정의의 테이블로 연결된다(자동 검사, 연결 안 된 경로 0개). `/api/push/vapid-public-key` 는 설정값이라 테이블이 없다.
