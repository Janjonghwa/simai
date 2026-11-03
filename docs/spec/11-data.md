# 11. 데이터

Spatial DB는 EC2의 PostgreSQL + TimescaleDB(시계열) + PostGIS(공간)로 구성한다. 영상·사진·3D 자산은 객체 저장소에 두고 DB에는 주소만 저장한다. 2D 관측 원본은 EC2 가 아니라 Tier 0 저장 위치(3.12)에 둔다(11.2).

**DB 운용.**

- 이미지는 `timescale/timescaledb-ha` 의 -all 태그를 쓴다(TimescaleDB·PostGIS 포함).
- `risk_event`는 일반 테이블로 둔다. `notification`이 `risk_event(event_id, source)`를 복합 FK 로 참조해야 하기 때문이다(11.4).
- `world_track_state`, `skeleton_3d`, `system_metric`, `system_health`, `device_heartbeat` 는 hypertable 로 만들고 압축과 보관 정책을 건다(확장: `channel_state`, `watch_feature`, `ppe_state`). 다른 테이블은 hypertable 을 외래 키로 참조하지 않고, hypertable 의 버전 ID 는 논리 참조로만 둔다.
- 법령 지식 그래프와 RAG 색인은 같은 PostgreSQL 의 `rag_node`·`rag_edge` 와 `pgvector`·`pg_trgm` 이다(v1.3 수정 49). 임베딩은 `Qwen/Qwen3-Embedding-0.6B`(Apache-2.0) dense 1024차원(`float32`, L2 정규화, cosine, 이전 `BAAI/bge-m3` 대체)이고, 문서와 질의에 같은 고정 모델 revision·설정을 쓴다. 문서 임베딩 입력은 `"[법령명 조문(제목)] 본문"` 형식을 쓰며, 법령 조문·지침·사례 노드(`rag_node`)와 TBM 표준 질의 템플릿(`rag_query_template`)은 시드 적재 때 미리 임베딩해 넣고 비정형 실시간 질의는 EC2 Python 컨테이너 CPU 에서 임베딩한다(`docs/design/tbm-legal-graph.md`).
- 컬럼·키·제약은 ERD(구현 설계 문서)와 Flyway 마이그레이션이 정본이고, 이 장은 테이블과 원칙을 적는다.
- PostGIS 는 저장·조회에만 쓴다. 구역 판정 계산은 Risk Engine 한 곳에서만 한다(3.2).

## 11.1 구성·기준 데이터

자주 바뀌지 않으며, 수정 대신 버전을 추가한다.

| 테이블 | 주요 내용 |
|---|---|
| `site` | World 좌표계 정의(원점·축·스케일 근거), BEV 평면도 참조, 좌표계 이전 이력 |
| `camera` | 카메라 ID, 모델, 하드웨어·펌웨어 버전, 해상도·fps |
| `calibration_version` | 카메라별 K·왜곡·R·t·높이, 재투영 오차, 스케일 근거, 상태 |
| `scene_version` | 정적 3D 자산 주소(`GLTF_VISUAL`, `SURFACE_SOURCE_MAP`), 사용한 보정 버전, 작업·생성 기록(`job_id`, `run_id`), 상태(`CANDIDATE`·`COMMITTED`·`REJECTED`) |
| `scene_activation` | Scene 버전 활성화·되돌리기 이력(누가·언제·이유, 추가만). 가장 최근 이력이 가리키는 버전이 현재 활성(`COMMITTED`) Scene 정본이다(12.6, `docs/design/scene-version-from-job.md`) |
| `zone` | 다각형 + 높이 범위, 판정 유형(GROUND_FOOTPRINT/VOLUME_3D), 유형, 심각도, 요구 조건, 유효 시작·종료 |
| `rule_version` | 규칙 ID, 임계값 세트, 적용 기간(시험 프로파일 포함, 7.4). 값은 AI 규칙 등록부에서 뽑으며 같은 버전의 값은 바꾸지 않는다(첫 운영 배포 전 예외, 11.4-1). 근거 조항(`legal_basis`)은 비어 있으면 시드가 채우고, 다른 값이 있으면 첫 운영 배포 전에는 갱신하고 뒤에는 마이그레이션이 멈춘다(7.1) |
| `equipment_asset` | GLB 주소, 반경·형상 파라미터(확장, 부록 C) |
| `layout_plan` | 가상 레이아웃(개구부·장비 주차 구역은 구역, 가설 비계는 작업면)·계획 궤적(F-02·F-01), 출처 PLANNED. 계획 작업 위치는 선택이다: 관리자가 주면 그 위치만 쓰고, 없으면(빈 목록 포함) Risk Engine 컨테이너가 작업면·구역에서 만든다(8.3) |
| `layout_plan_revision` | 가상 레이아웃 revision 이력(추가만): 그때의 이름·항목. TBM 보고서가 계산한 `lv-{revision}` 배치를 나중에도 다시 본다. `layout_plan` 은 지금 revision 의 사본이다 |
| `config_bundle` | 구역·규칙·보정·형상(등록 정적 객체·작업면 OBB) 버전 묶음, 해시, 발행 시각, 현장 적용 시각(RunPod Tier 0 ACK), 묶음에 넣은 규칙 프로필(`rule_profile`: 운영·교실 시험, 7.4. 다음 발행의 기본값) |
| `config_bundle_item` | 묶음에 들어간 구역·규칙·형상의 ID 와 버전 |
| `config_bundle_file` | 묶음이 가리키는 파일 바이트(매니페스트·구성 요소). Tier 0 가 서명 주소로 내려받는다 |
| `config_ack` | Tier 0 의 묶음 적용·거부 응답(I1 `config_ack`) |
| `static_object` | 등록 작업면·정적 객체: 보수적 OBB, 상판 높이(7.5). 수정은 새 버전 |
| `tier0_endpoint` | Tier 0 Pod 공개 IP·포트·공개키 지문 등록 이력(3.12) |

## 11.2 시계열 관측

| 테이블 | 주요 내용 | 빈도 | 보관 |
|---|---|---|---|
| `observation_2d` | 촬영 시각, 카메라, 카메라 내 트랙, bbox, keypoint 배열. **EC2 DB 에 두지 않고** Tier 0 저장 위치(3.12)에 Parquet 로 보관한다(위치는 19.1 저장 위치 구조의 `obs2d`. 컨테이너 디스크면 인스턴스를 지울 때 함께 사라진다). EC2 에는 필요할 때 카메라별 요약(I1 `observation_summary`, 2D 원본 없음)만 보낸다 | 저장 5\~15 Hz(처리는 카메라당 12 FPS 이상, 5.1) | 7\~14일(보정 재계산용) |
| `world_track_state` | World ID, XYZ, 불확실성, 속도, 관측 상태, gap, 기여 카메라, 보정·Scene 버전 | 10 Hz | 11.5 |
| `skeleton_3d` | World ID, 3D 관절, 관절 가시성, 계산 방식 | 5\~10 Hz | 11.5 |
| `system_health` | 시스템 상태, 카메라별 상태, lag, 보정 상태, 하트비트 | 1 Hz | 11.5 |
| `system_metric` | 구간별 지연, FPS, 큐 길이, GPU 사용률, 프레임 손실 | 1\~5 Hz | 11.5 |
| `device_heartbeat` | 노트북 하트비트(Tier 0 연결, 현장 표시, 카메라 수신 상태). 원격 단절 구분(3.2) 근거 | 1 Hz | 11.5 |
| `observation_summary` | 2D 관측 카메라별 요약(I1 `observation_summary`, 원본 위치 `archive_uri`) | 필요 시 | 11.5 |
| `system_outage` | 장애 기록(관제 정지·현장 장비 응답 없음·원격 전달 단절, 6.5): 마지막 정상 근거, EC2 판정 시각, 끝 시각과 끝난 이유(복구·종류 바뀜·EC2 재시작). 열린 장애는 하나다(G3 근거) | 장애 시작·끝마다 | 11.5 |

`ppe_state`, `channel_state`, `watch_feature`, `region_state`(와 11.3 의 `watch_binding`) 등 확장 테이블은 MVP DB 에 두지 않는다. 목록은 부록 C 각 절의 “확장 DB 테이블”에 있다.

## 11.3 이벤트·조치

| 테이블 | 주요 내용 |
|---|---|
| `risk_event` | 규칙·버전, World ID·구역, 출처, 생명주기·검증 상태, severity·confidence·confidence_reasons, 발생·해소 시각, 근거 스냅샷, 관련 이벤트(related_event_id), 보존 표시 |
| `risk_event_update` | RunPod Tier 0 전이의 시간순 기록(event_sequence, config_version, 지연 전달·감사 전용 여부). 저널 위치는 저널 세대(`journal_epoch`)와 offset 으로 가른다(Tier 0 저널이 offset 0 부터 새로 시작하면 EC2 가 세대를 올린다) |
| `vlm_assessment` | 이벤트, 작업 유형(지금 기록하는 것은 `vlm_rationale`, 사고 구간 요약은 `incident_summary` 테이블)·job_id, 모델·버전, 입력 참조, 근거 문장, 인용 조항 ID, 상향 의견(proposed_severity), 관리자 결정(승인·거절, 누가·언제, `escalation_id`), G7 종단간 지연(`event_observed_at`, `event_to_rationale_ms`)과 추론 지연 |
| `notification` | 이벤트, 채널, 대상, 발송·수신 확인·ACK 시각 (LIVE 이벤트만 연결 가능. 시험 알림은 이 테이블에 넣지 않음). 발송 대기열을 겸해 발송 워커 임대 기한(`claimed_until`)을 두고, 비었거나 지난 대기 건은 다시 꺼낸다. 원격 발송 채널(Web Push)은 폐기했고(10.1), 테이블은 LIVE 전용 알림 기록과 DB 제약(11.4 의 3)을 위해 둔다 |
| `media_clip` | 이벤트 연결 영상 구간의 객체 키, 카메라·구간(촬영 시각)·해시·크기, 얼굴 흐림 여부, 삭제 예정일, 보존 표시. Tier 0 가 I1 `media_clip` 으로 보내고, 보낸 뒤 붙는 CRITICAL 자동 보존은 고치지 않고 `media_clip_retention` 으로 이력을 더한다 |
| `report` | 보고서 종류(TBM·DAILY), 대상 작업(`work_unit.work_id`)·보고서 버전(`report_version`, 덮어쓰지 않고 새 행 추가)·공종(`work_type`), 레이아웃·계산 결과(`sim.*`) 참조, 생성 상태, 본문(`sections`), 계산 근거 표, 인용 목록(`citations`), 법령 지식 그래프 서브그래프(`subgraph`: `nodes`·`edges`), 4단계 실행 상태(`steps`), 구역 최근 이벤트 요약(`zone_history`) (EC2 Python 컨테이너 생성, v1.3 수정 49) |
| `work_unit` | TBM 작업(날짜·작업명·시간 구간·구역/장소·예정 인원·담당 조, 그리고 작업이 쓰는 가상 레이아웃 `layout_id` 및 비개인정보 공종 `work_type`, 8.3·12.6, `docs/design/server-work-units.md`·`docs/design/tbm-legal-graph.md`) |
| `event_ack` | 운영자 확인: 누가·어디서·확인 대상 `event_sequence`(3.10). 저널의 ACKNOWLEDGED 전이가 가리킨다 |
| `event_close` | 운영자 종료 명령과 Tier 0 결과(6.4). REST 요청의 멱등 키는 따로 저장하고(유일), `close_id` 는 EC2 가 발급한다. 종료 전이가 가리킨다 |
| `event_escalation` | 승인한 VLM 상향 의견의 적용 명령과 Tier 0 응답 결과(`result_status`: `PENDING`·`ACCEPTED`·`DUPLICATE`·`REJECTED`, `reject_reason`, `result_received_at`, 5.4). 상향 전이가 가리킨다 |
| `journal_cursor` | Tier 0 인스턴스별 저널 수신 상태: 지금 받는 저널 세대(`journal_epoch`), `acked_offset`, 재동기화 중 여부, 마지막 스냅샷(3.2) |
| `i1_deferred_message` | 앞선 메시지를 기다리는 I1 메시지(받은 봉투 그대로, 처리되면 지움) |
| `incident_summary` | 사고 구간 VLM 사후 요약 요청·결과(추가만): 이벤트·event_sequence·job_id, 상태(QUEUED·RUNNING·SUCCEEDED·FAILED·TIMED_OUT), 요청자·요청·완료 시각, 구간, 요약 문장, 근거 시각, 사용 프레임 수, 모델 버전, 지연, 오류 코드(5.4) |
| 이벤트 프레임 | 별도 테이블을 두지 않는다. I1 `event_frame_ready` 의 프레임 주소와 촬영 시각은 `vlm_rationale` 작업 입력(`analysis_job.input.frames`)에 넣어 둔다(5.4) |
| `clip_access_log` | 이벤트 클립 열람 기록(14장) |
| `video_segment` | 상시 영상 조각의 객체 키, Tier 0·카메라·구간(촬영 시각)·stream_epoch·해시·크기, 삭제 예정일, 삭제 시각. Tier 0 가 I1 `video_segment` 로 보낸다(5.1, v1.3 수정 36) |
| `video_access_log` | 상시 영상 열람 기록(14장) |
| `cctv_remote_usage` | 관제 화면 원격 CCTV 송출 바이트의 1분 단위 추가 전용(append-only) 사용량 기록(UTC 달력 월 누적 합산으로 월 80 GB 상한 판정, 영상 바이트 자체는 EC2 에 저장하지 않음, 3.3, v1.3 수정 46) |
| `upload_grant` | 임시 업로드 주소 발급 기록(올라온 객체 키 검증): 목적(이벤트 프레임·클립, 아바타 자산, 상시 영상 조각, Scene glTF, 정적 3D 입력 `STATIC_RECONSTRUCT_INPUT`. 조치 사진 `TICKET_PHOTO` 는 폐기, 10.1), 요청 주체(Tier 0·기기·관리자·분석 워커, 12.6), 서명에 묶은 형식·크기·sha256, 이벤트 프레임·클립이면 이벤트 |
| `avatar_asset` | 아바타 자산 버전(키 `(world_id, stream_epoch, appearance_version)`, 추가만, 11.5·12.2) |
| `push_subscription`·`test_push`·`push_receipt` | 폐기(v1.3, 10.1). DB 에는 남아 있으나 쓰지 않으며, 테이블 정리는 별도 이슈에서 한다 |
| `rag_clause` | 초기 안전 수칙 조항 테이블(`V5` 시드 호환 유지). 신규 TBM·일일 보고서 검색 정본은 아래 `rag_node`·`rag_edge` 법령 지식 그래프로 전환한다(v1.3 수정 49, `docs/design/db-tables.md`) |
| `rag_source` | 법령 지식 그래프 원문 출처·버전: 법령·행정규칙별 법령 ID·MST·시행일·공포번호·원문 해시·수집 시각(원문은 저장소 밖, S15P31A101-416) |
| `rag_node` | 건설안전 법령 지식 그래프 노드: 종류(`node_kind`: 법령·조·항·호·목·별표·개념·위험 규칙, 후속 지침·재해사례)와 층(`layer`: 법률·중대재해처벌법·시행령·시행규칙·안전보건기준규칙·고시), 출처 법령·문서명, 조문 번호·제목·본문, 규칙 태그(`rule_tags`, `R1`~`R7`), 출처 판 번호(`source_version`), 임베딩(`Qwen/Qwen3-Embedding-0.6B` 1024차원, 입력 `"[법령명 조문(제목)] 본문"`), 모델 revision. 국가법령정보센터 Open API 법령(산안법·시행령·시행규칙, 안전보건기준규칙, 중대재해처벌법·시행령)을 먼저 적재하고, KOSHA GUIDE 와 중대재해 사이버안전박물관 재해사례는 재배포·API 이용 조건을 확인한 뒤 적재한다(`docs/design/tbm-legal-graph.md`) |
| `rag_edge` | 건설안전 법령 지식 그래프 방향성 엣지(`src_node_id` → `dst_node_id`, 종류 `edge_kind`: 상하위·참조·준용·"불구하고"·위임·별표·외부 법령 인용·개념 연결·규칙 태그, 후속 지침·사례 연결), 추출 방법(`method`)·검수 상태(`review_status`), 근거 문구(`relation_label`). 하이브리드 검색 후 1단계 확장(검수 전 후보 제외)과 관제 웹 `subgraph` 시각화에 쓴다. 칸·값 정본은 `docs/design/db-tables.md` |
| `rag_query_template` | TBM 표준 질의 템플릿(`R1`~`R7`·구역 유형 조합)과 미리 계산한 임베딩(`rag_node` 와 같은 `Qwen/Qwen3-Embedding-0.6B` 고정 revision). 비정형 실시간 질의는 EC2 Python 컨테이너 CPU 에서 임베딩한다 |

PLANNED·WHAT_IF 결과는 별도 스키마 `sim.*`에 저장한다: `sim.evaluation_run`(계산 실행, 기준 구성, Risk Engine 라이브러리 버전, 판정에 쓴 계획 작업 위치 목록과 위치마다 출처 REQUESTED·GENERATED), `sim.risk_result`(위험 항목, 실제 알림 채널로 가지 않음). 위치 목록은 EC2 가 컨테이너 결과(`risk_plan_result.positions`)를 그대로 저장한다. 컨테이너가 만든 위치는 다시 계산하면 달라질 수 있으므로 결과와 함께 둔다. 확장 테이블(`sim.trajectory_plan` 등)은 부록 C 에 있다.

운영자 확인·종료·상향은 전이 기록(`risk_event_update`)에 한 줄로 쌓고, 누가·왜·결과는 `event_ack`·`event_close`·`event_escalation` 에 두어 전이 행이 가리킨다.

## 11.4 스키마 원칙

1. 시계열·이벤트는 추가만 한다. 구역·규칙·보정·Scene은 유효 기간이 있는 새 버전을 추가한다.
   - **첫 운영 배포 전 규칙 보완 예외(Lead 승인 2026-10-22).** 첫 실제 운영 배포 전에는 규칙의 판정 동작·값을 같은 `rule_version` 안에서 고칠 수 있다. 근거: 그 버전으로 남은 운영 판정 기록(운영 DB·배포 데이터)이 없어 지킬 과거 판정이 없다. 고친 값은 등록부에서 시드를 다시 만들어 같은 버전 행에 반영한다. 첫 운영 배포 시점부터는 동작이나 값이 바뀌면 반드시 새 버전을 추가하고 이 예외는 끝난다. 첫 운영 배포 시점은 그 배포 MR 이 개정 이력(README)에 날짜와 함께 기록하고, 그 뒤 시드의 같은 버전 값 변경은 마이그레이션 검사가 막는다.
2. 모든 계산 결과는 출처를 안다: 위치(보정·Scene 버전), 이벤트(규칙 버전), 근거(모델 버전).
3. `notification`은 출처가 LIVE인 `risk_event`만 참조할 수 있게 한다. 구현: notification에 source 컬럼을 두고 risk_event(event_id, source)를 복합 FK로 참조하며 source = ‘LIVE’ 체크를 건다(일반 CHECK는 다른 테이블을 볼 수 없으므로). 발송 워커를 둘 때도 LIVE만 소비하고, DB 기록 차단과 실제 발송 차단을 각각 테스트한다.
4. 촬영·수신·판정 시각을 모두 UTC로 저장한다.
5. 위치 기록에는 이름이 없다.

**용량.** 10명 기준 `world_track_state`는 하루 8시간에 약 300만 행(압축 전 수백 MB). 가장 무거운 `observation_2d`는 하루 수 GB까지 가능하므로 EC2 가 아니라 Tier 0 저장 위치에 두고 보관을 짧게 하며, 필요하면 5 Hz로 낮춘다. 지급 EC2 디스크는 320 GB 다. hypertable 전체가 압축 전 운영일 하루 약 2 GB(10명·8시간, 골격 포함 추정)라 60 운영일이어도 약 120 GB 이고 압축하면 그보다 훨씬 작으므로, 좌표·골격·이벤트는 상시 삭제 없이 발표 후 일괄 파기까지 둔다(11.5). 추정은 운영 초기에 청크 크기로 다시 잰다.

**리플레이 질의.** 최소 리플레이(M3)는 시각 T의 `world_track_state`와 T에 활성인 `risk_event`를 쓴다. 리플레이 확장은 여기에 `skeleton_3d`, T에 유효한 `zone`·`rule_version`, T에 COMMITTED인 `scene_version`을 더한다(부록 C).

## 11.5 보관과 개인정보

보관 기간은 18.4 의 확인 절차가 같은 값을 쓴다. 촬영·시연·데이터 활용은 기관(SSAFY) 허가를 기준으로 하며, 참여자 별도 동의나 시연·공개 자료의 '사람 없음 또는 동의자만' 제한은 두지 않는다(2026-10-23 사용자 결정, 16장).

| 데이터 | 보관 |
|---|---|
| 원본 영상 | 관제 중 RunPod로 상시 전송(TLS). Tier 0 가 카메라별 상시 영상 조각으로 객체 저장소에 저장하고 14일 후 EC2 삭제 작업이 자동 삭제(보존 표시 없음, 얼굴 흐림 없음, 열람은 관리자·조회 기록, v1.3 수정 36) |
| 이벤트 영상 클립 | 14일 후 자동 삭제. CRITICAL 이벤트 연결분은 자동 보존 표시, 아차사고는 관리자가 보존 표시를 붙이거나 뗌. 보존분은 접근 기록을 남기고 프로젝트 종료 시 파기. 얼굴은 흐림 처리하지 않는다(사용자 결정, 16장). 열람은 권한자로 제한하고 조회 기록을 남긴다(14장). 흐림 단계는 선택 사항으로 남기며, 켜면 업로드 전에 반드시 거치고 실패하면 흐리지 않은 클립으로 대신하지 않는다. 생성형 얼굴 복원·표정 추정은 하지 않고, 재식별(Re-ID)은 전신 임베딩을 기하 게이트 뒤 보조 비용으로만 쓰며 Tier 0 위치·ID 정본을 덮지 않는다(도입은 G2 측정 뒤, 19.1). EC2 단절 중 클립은 Tier 0 저장 위치에 먼저 저장 후 복구 시 업로드(5.1). Tier 0 에 남은 미업로드 클립은 보존 표시가 없고 삭제 예정일이 지난 것만 올리지 않고 지우며, 보존 표시가 있는 클립은 전체 파기 기한 전까지 복원·업로드한다 |
| PPE crop(확장) | 판정 직후 삭제, 저장 없음 |
| 2D 관측 | Tier 0 저장 위치(3.12), 7\~14일 |
| 좌표·상태·이벤트 로그 | 얼굴 없음. 프로젝트 발표 후 30일 안 파기 |
| 시계열(hypertable) | `world_track_state`·`skeleton_3d`·`system_health` 는 상시 삭제 없음(리플레이용, 발표 후 일괄 파기), 7일 지난 청크는 압축. 운영 지표 `system_metric`·`device_heartbeat` 는 30일 보관 정책. EC2 디스크 사용률이 70% 를 넘으면 `skeleton_3d` 에도 30일 보관 정책을 건다. 확장 hypertable 은 확장 착수 때 정한다 |
| 시나리오 데이터셋 | 기관(SSAFY) 허가 기준, 보관 기간 명시(프로젝트 발표 후 30일 안 파기) |
| 내부 테스트·녹화 영상 | 기관(SSAFY) 허가 기준으로 촬영·활용한다(참여자 별도 동의나 시연·공개 자료의 '사람 없음 또는 동의자만' 제한 없음). 보관 기한 필수, 커밋 안 함, 노트북과 프로젝트 녹화 보관 위치(15.3, 19.1)에만 두고 프로젝트 발표 후 30일 안 삭제 |
| HMR 아바타 자산·외형/텍스처·포즈(시연 범위·EXP-02) | 기관(SSAFY) 허가 기준(별도 참여자 동의 조건 없음). EC2 는 아바타 자산 버전(`avatar_asset`, 키 `(world_id, stream_epoch, appearance_version)`)마다 DB 에 한 줄씩 추가만 하고 덮어쓰지 않으며, 객체 저장소의 자산 파일(스키닝 glTF·UV 텍스처·표면 출처 지도 PNG)과 함께 프로젝트 발표 후 30일 안에 파기한다. 실시간 포즈(`avatar_pose_batch`)는 저장하지 않는 것이 기본이고, 리플레이용 사람별 1 Hz 다운샘플 저장은 선택이다(저장 시 발표 후 30일 안 파기). 외형 텍스처에는 관측된 얼굴 픽셀을 그대로 쓸 수 있고(`face_policy` 기본 OBSERVED, BLURRED·EXCLUDED 선택), 얼굴 영역도 관측·미관측을 구분한다(`face_ratios`, 보간 없음). 안 보이는 얼굴을 지어내는 생성형 얼굴 복원·표정 추정은 하지 않는다 |
| 전체 | 프로젝트 발표 후 30일 안에 모든 데이터를 파기한다(보존 표시분 포함) |

- 자동 삭제는 EC2 클립 삭제 작업, TimescaleDB 보관 정책, Tier 0 저장 위치 정리 작업으로 구현한다. 클립 삭제 작업은 `delete_after` 가 지났고 보존 표시가 없는 클립의 객체를 지우고 `deleted_at` 을 남긴다. 객체 저장소 수명 주기 규칙은 DB 의 보존 표시를 모르므로 클립에 쓰지 않는다.
- 촬영·외부 클라우드(GPU 실행 사이트·EC2) 전송·이벤트 클립 보관(얼굴 흐림 없이 14일 보관, CRITICAL 이벤트 연결 클립은 보존 표시로 프로젝트 발표 후 30일 안 파기까지 유지, 열람은 권한자로 제한하고 조회 기록 유지)·시연 및 발표 자료 활용은 모두 기관(SSAFY) 허가를 기준으로 하며, 참여자 개별 동의서나 비참여자 동의 제한은 두지 않는다(16장).
- 외부 LLM 에는 SSAFY GMS 를 거쳐 OpenAI(국외)로 텍스트(규칙 계산 결과·법령 조항·이벤트 요약·비개인정보 공종 `work_type`)만 보내고 영상·이미지·얼굴·실명이나 작업명·인원·반장/조 이름은 보내지 않는다(5.4, 8.3). AWS 는 서울 리전이다.
- 전송 구간은 모두 암호화한다(14장).

## 11.6 계정·기기·분석 작업

| 테이블 | 주요 내용 |
|---|---|
| `app_user` | 계정, 역할 3종(관리자·반장·열람, 14장). 위치·이벤트 기록에는 계정 ID 만 쓴다 |
| `refresh_token` | 갱신 토큰(해시만 저장) |
| `device` | 현장 노트북 기기와 토큰(확장: 워치) |
| `worker` | 분석 워커와 토큰, 처리 가능 작업 유형 |
| `analysis_job` | I5 작업: 유형·우선순위·시도 횟수·입력·결과(12.5) |
| `job_lease` | 작업 대여: 만료·하트비트·반납(12.5) |

계정, 기기·워커 토큰은 API 없이 EC2 관리 명령으로 만들고 회전·폐기한다(14장). `site`·`camera` 는 Flyway 시드(현장 `classroom-1`, 카메라 `cam1`~`cam4`)로, `rule_version` 은 AI 규칙 등록부에서 자동 생성한 Flyway 반복 마이그레이션으로 넣는다(16장).

## 11.7 보정·설치

| 테이블 | 주요 내용 |
|---|---|
| `calibration_camera` | 보정 버전의 카메라별 K·왜곡·R·t·재투영 오차 |
| `calibration_document` | 보정 문서 원문. 구성 묶음의 calibration 구성 요소로 그대로 내려준다 |
| `calibration_update_message` | I1 `calibration_update` 수신 기록(추가만, `msg_id` 로 재전송 제거). 현재 보정 상태는 보낸·받은 시각 중 이른 쪽이 가장 늦은 기록이고, 같으면 늦게 받은 것이다 |
| `calibration_run` | 자동 보정 실행(걷기 시간, 2D 관측 위치, `calibration_ba` 작업, 결과 후보) |
| `calibration_verification` | 독립 검증점 결과(4.4) |
| `scale_reference` | 실측 정물 목록(이름·치수·측정일, 4.3) |
| `calibration_scale_check` | 보정에서 정물별로 잰 크기·오차·불일치 경고 |
| `coverage_guide` | 커버리지 계산과 카메라별 설치 안내(4.5) |
| `floor_plan` | BEV 평면도(보정·Scene 버전, 이미지 키, `bev_floorplan` 작업, 4.1) |
| `bev_frame_set` | BEV 평면도 입력 프레임 묶음(관리 명령으로 등록, 추가만). 같은 보정·묶음의 평면도는 한 번만 만든다 |
