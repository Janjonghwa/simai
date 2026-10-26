# simai System Specification v1.3 (기획서)

이 폴더가 기획서의 **정본**이다. Notion 에는 이 폴더로 가는 링크와 발표·제출용으로 내보낸 사본만 둔다.
요구사항 명세서는 [`../requirements.md`](../requirements.md) 에 있다. 저장소 규칙과 계약은 `AGENTS.md`, `contracts/` 를 본다.

**기준 문서:** simai System Specification v1.2 r4 수정 7 (2026-10-17) · **개정일:** 2026-10-23

2026-10-17 · A101 · **상태: v1.3** · 정본: GitLab `docs/spec/`

**문서 관계.** 이 문서는 simai Pipeline v1.0\~v1.3(Architecture & Reliability Specification 포함)을 대체한다. 이후 “v1.x”는 이 문서 계열만을 가리키고, 이전 문서들은 “구 Pipeline 문서”로 부른다.

**기준선과 개정 관리.** v1.2 r3 Errata 3 을 첫 개발 기준선으로 보존하고, v1.2 r4 에서 GPU 확보·대형 Experiment 방식·인체 표현 목표를 바꿨다(그때 ‘사실적 외형 아바타 비목표’를 바꿔 관측 기반 메시 아바타를 EXP-02 연구 대상으로 했다, 1.4). v1.3 은 v1.2 r4 수정 1\~7 과 2026-10-17 전체 검토 결정을 한 판으로 합친 것이다. G0\~G8 수치는 검증 전 초기 목표이며, 바꾸려면 실측·결정 기록과 함께 명시적으로 개정한다.

**개정 이력**

| 버전 | 날짜 | 내용 |
|---|---|---|
| v1.3 | 2026-10-17 | v1.2 r4 수정 1\~7 과 2026-10-17 전체 검토 결정 통합(S15P31A101-154). 무마커 내부 파라미터(교실 직선) + HumanCalib 번들 조정, 보정 대체 경로를 걷기 재수행·정물 추가로(마커·자동 보정 비교 후보 제거), VLM 은 Cosmos3-Nano Reasoner·텍스트 생성은 외부 LLM API, YOLO26 E2E 기본·발 6점 미세조정, R2·R3 가장자리를 등록 작업면 OBB 로, 발생 지속 조건·이벤트 재개, 하트비트·시계 차이·촬영 시각 추정 방식, Pod 엔드포인트 등록·TLS 공개키 고정, 시험 알림·푸시 수신 확인, I5 작업 가져가기, 2D 관측 RunPod 보관, PWA M1·M2·최소 리플레이 M3, 캡슐 아바타, 확장 기능을 부록 C 로 이동, 15.1 시연 합격 게이트와 부록 D 회귀 시험 분리, 부록 E 시연 시나리오, 16장 영역별 정리, 본문 개정 꼬리표 삭제 |
| v1.3 수정 1 | 2026-10-17 | 저장소 구조에 `contracts/data/`(녹화 세션 manifest 등 데이터 형식 계약)를 더함(S15P31A101-158). 21.1 |
| v1.3 수정 2 | 2026-10-17 | R7(구역 감시 능력 저하) 심각도를 HIGH(시스템)에서 CAUTION(시스템)으로 낮춤: 시스템 상태 안내라 관제 화면 안내로만 보내고 현장 소리·반장 PWA 로 내지 않는다. 저하 구역 안 사람의 위험은 각 규칙과 R6 이 ‘확인 필요’로 다룬다(S15P31A101-73). 7.2, 6.5 |
| v1.3 수정 3 | 2026-10-17 | 머지된 v1.3 계약(I0·I1·I5·녹화 manifest) 이름으로 본문을 맞춤(S15P31A101-167): 현장 경보 목록 `health.active_site_alerts`(3.10), 시계 차이 `clock_sync_request`·`clock_sync_response`·`health.laptop_clock_offset` 과 T_lag·pipeline_lag 보정(3.9, 6.1, 부록 A), 엔드포인트 등록 `tier0_endpoint`(3.12), 2D 관측 요약 `observation_summary`(11.2), I5 작업 대여 `job_lease_request`·`job_request`·`job_heartbeat`·`job_callback`·`job_lease_status`(12.2, 12.5), 녹화 manifest `recording_manifest`(15.3). Network Volume 구조에 `obs2d`·`recordings` 추가(19.1, 3.12). 보정 출처 값을 `G1_AUTO`·`DATASET_GT`·`SIM`·`MODEL_ESTIMATE` 로 정리(20.4. 이 값을 쓰는 calibration·calibration_update·run_record·scene_output·human_mesh_output 계약 2.0.0). 내부 파라미터 추정 후보 라이선스 확인과 고전 기준 잠정 기본(4.3, 3.8, 부록 B). 아직 계약에 없는 항목(`confidence_reasons`, 이벤트 재개 전이, 노트북 송신 큐 버린 프레임 수)을 19.1 에 모음(6.4, 7.1, 12장 머리말, 12.7) |
| v1.3 수정 4 | 2026-10-18 | 6·7차 구현 결정과 사용자(Lead) 승인 계약을 반영(S15P31A101-169). 확인 대상 `event_sequence`·경보 단계·상향 재경보·재동기화 `as_of`(3.10, 3.2), 시계 차이 정책: 교환 지연 60초 유지 + 표류 불확실성, 그 뒤·표본 없음·단절은 offset null + UNSAFE_UNKNOWN, 관측 상태 유지·해소 확정 금지, 시계 점프 뒤 해소 유지 시간 재시작(3.9, 6.1, 6.5), Config Sync STALE 정의(3.2, 6.2)와 `world_state.config_version`, 판정이 아닌 전이의 `evidence_ref` null(3.2, 12.3), 이벤트 클립 얼굴 흐림 안 함·동의서 항목·`media_clip`·`media_clip_retention`(5.1, 11.3, 11.5, 12.2, 12.3), 기하 단계 10명 p95 50ms 단계 목표(6.6), 번들 조정은 HumanCalib 원리의 자체 구현·CCVS 보조 진단·DRIFTED 감시 규칙(4.3, 4.4, 3.8, 18.1, 부록 B), `stream_epoch` 비감소·STREAM_REJECT·SKIP_NOTICE 구간 집합·구분 불가 빈칸(12.7), 재개 전이·`confidence_reasons` 계약 반영 표기 정리(6.4, 7.1, 12.4), 결정 기록(16장), 미결 사항(19.1). 계약: `world_state` 1.2.0·`risk_event_transition` 1.4.0·`health` 1.4.0(마이너), `media_clip`·`media_clip_retention` 1.0.0 신설 |
| v1.3 수정 5 | 2026-10-18 | 8\~10차 구현 결정과 사용자 승인 계약을 반영(S15P31A101-172). 재동기화 스냅샷 만들기와 빈 저널 `journal_offset` null(3.2), 재시작 복구: 해소 기록(tombstone)·World Track 은 저장만(재관측 매칭 후보, 정본 아님)·복구 시작 거부 네 경우·복구 Health 해제 조건·저널 정리 하한(3.2), 이중 감시의 `tier0.control`·`site_display` 함께 읽기(3.2), 노트북 장애 판단: 3회 누락·정상 복귀 2초 유지·영상 상태는 중계 보고·시작 직후 관제 중단·UNSAFE_UNKNOWN health 는 관제 중단(3.10), TensorRT 엔진 첫 기동 GPU 별 생성·볼륨 캐시, Tier 0 이미지 CUDA 13·드라이버 조건·다이제스트(3.12, 18.2), 접지 추정 근거·분해능(약 8 cm)·가림 유지 3초(5.2), 늦은 재연결·ID 교정: 2대 근거·병합 금지 반증·첫 관측부터 도달 거리·잔여 위험(5.2), World 위치 정의에 `surface_source`·`head_z_validity`(5.2), R2·R3 높이 프로파일(운영 2 m, 교실 시험 0.35 m·rule_version+1000)·위험 가장자리와 이어진 같은 층 작업면·접지 모름 정책, R5 판정 초기값(2.0/2.5초, 0.4 m/s, 타원 범위, 판정 제외·holding, 오예측 정의)(7.2), 용어 `surface_source` 두 필드 구분·`head_z_validity`·`aliases`·해소 기록(부록 A), 복구 운영 절차(18.5 신설), I1 예시(12.3), 계약 반영 목록·미결(19.1). 계약 v2.3.0: `world_state` 1.4.0·`event_state_snapshot` 1.3.0(마이너), `laptop_heartbeat` 설명 보강 |
| v1.3 수정 6 | 2026-10-18 | 저장소 구조의 `docs/` 하위 폴더 용도를 적음: `spec/`·`design/`(구현 설계 문서)·`data/`(데이터 manifest)·`experiments/`(S15P31A101-176). 21.1 |
| v1.3 수정 7 | 2026-10-19 | 사용자 승인 I1 계약 변경을 반영(S15P31A101-179). `head_z` 는 `head_z_validity` 가 VALID·UNCERTAIN 이면 값을 싣고 UNAVAILABLE 이면 비움(2026-10-18 ‘VALID 일 때만’ 대체), UNCERTAIN 값은 확정 근거 아님(R1·R5 는 Z 모름, R4 는 확인 필요로 사용하고 UNCERTAIN 머리로는 해소하지 않음)(5.2), 시계 차이·최신 촬영 시각을 모르면 `health.pipeline_lag_ms` null·하한 `pipeline_lag_lower_bound_ms`(6.1), 카메라 동기화 근거 `sync_valid`(근거 없으면 null)(6.1), Tier 0 내부 기록 장애와 `health.tier0_warnings`(6.5), I1 예시·설명(12.3), 용어(부록 A), 계약 반영 목록·미결(19.1). 계약 v3.0.0: `world_state` 2.0.0·`health` 2.0.0(메이저) |
| v1.3 수정 8 | 2026-10-19 | 무마커 내부 파라미터 추정 기본 경로 결정(S15P31A101-192): AnyCalib(`anycalib_dist`, `radial:2`) 기본, 사용 불가 때 GeoCalib(`distorted`·`radial`), 그다음 고전 기준(가중치 없는 예비). 실패한 결과는 다른 방법 값으로 채우지 않고 비우며 방법을 기록, 고전 기준으로 학습 결과의 직선성 확인, 입력은 메모리 시간 중앙값 배경(영상 비저장). C110 세 방법 비교 미결 삭제(4.3, 3.8, 16장, 19.1, 부록 B) |
| v1.3 수정 9 | 2026-10-19 | 계약 정리 5차(S15P31A101-197, `health` 변경은 사용자(Lead) 승인). Tier 0 우선으로 시작하지 않았거나 멈춘 분석 작업은 시도 횟수를 쓰지 않고 반납(3.2, 12.5), GPU 상태 표시 파일 쓰기 실패 운영 경고 `LIVE_MARKER_WRITE_FAILED`(정보 경고, 판정·알림 불변)(6.5, 부록 A), 운영 경로 검증 Run `experiment_id` `VAL-01`(채택 게이트 근거 아님)(20.5), 계약 반영 목록(19.1). I5 `attempt` 는 반납을 세지 않는 시도 번호, `max_attempts` 는 최대 시도 수(Lead 결정, 12.5). 계약 v4.0.0: `job_request` 3.0.0(메이저), `job_callback`·`health`·`run_record` 2.1.0(마이너) |
| v1.3 수정 10 | 2026-10-20 | 운영자 종료(S15P31A101-200): 감시 확보 근거가 오지 않아 자동 해소되지 않는 구역 대상 이벤트(카메라 철거, 구역 제거, 재시작 복원)를 관제 화면 관리자가 누가·왜를 남기고 닫는다. 사람 대상·HIGH·CRITICAL·지금 판정이 있는 이벤트는 거부, 종료는 해소 확인이 아니며(검증 상태 그대로) 재개하지 않음(재발생은 새 이벤트), 같은 명령은 멱등(6.4). I1 `event_close`·`event_close_result` 1.0.0(12.2, 12.3). 종료 전이는 `risk_event_transition`·`risk_event` 2.0.0 의 운영자 종료 해소 `resolution` 으로 싣고, 이것이 있는 해소는 VERIFIED·판정 근거를 요구하지 않는다(메이저, Lead 승인 2026-10-20, 12.3·12.4·19.1). 계약 v5.0.0 |
| v1.3 수정 11 | 2026-10-20 | World ID 자동 분할(S15P31A101-201): 한 ID 의 로컬 트랙이 2대 이상 카메라에서 같은 카메라의 두 검출로 갈라지고 0.35 m 넘게 떨어진 근거가 3번 쌓이면 분할, 원래 ID 는 예측 위치에 가까운 쪽, 두 ID 의 `identity_revision` 함께 증가·별칭 불변·병합 금지 쌍, 한 카메라·불확실 클러스터·0.35 m 안·근거 2초 끊김은 분할 안 함, 진행 중 이벤트는 원래 ID 에 두고 갈라져 나간 ID 를 분할 상대로 두어 둘 다 위험이 없을 때만 해소, 잔여 위험(5.2). 갈라져 나간 새 ID 의 원래 ID `split_from`(5.2, 12.3, 부록 A). 미결을 수동 교정(별도 티켓)으로 좁힘(19.1). 계약 v5.1.0: `world_state` 2.1.0(마이너, Lead 승인 2026-10-20) 선택 필드 `split_from`·`identity_revision` 설명 보강 |
| v1.3 수정 12 | 2026-10-20 | 내부 테스트 용도 녹화(S15P31A101-204, 사용자 결정): 수업 중 우연히 찍힌 사람이 있는 영상은 내부 개발·테스트에 쓴다. 보관 기한 필수·커밋 안 함·노트북과 프로젝트 Network Volume 에만·프로젝트 종료 시 삭제·공개 자료 사용 금지, 시나리오 촬영은 기존대로 동의 필수(11.5). 녹화 manifest 1.2.0 `consent.purpose`(마이너), 계약 v5.2.0 |
| v1.3 수정 13 | 2026-10-20 | API 명세 미결 정리(S15P31A101-206, 사용자 결정): 외부 LLM 은 분석 워커 `tbm_report` 안에서 호출(3.1, 5.4), M3 TBM 은 `POST /api/reports/tbm` 안에서 Risk Engine PLANNED 계산 + pgvector 검색, 일일 일지는 `report_kind` DAILY, PDF 는 브라우저 인쇄(8.3), BEV 평면도는 분석 워커 `bev_floorplan`(4.1), 사고 구간 요약은 M3·`incident_summary`·관리자 요청, VLM 입력 얼굴 흐림은 분석 워커가 VLM 호출 직전, 운영자 확인 상향은 관리자 승인 → I1 `event_escalate` → Tier 0 UPDATED 전이(5.4, 12.3), I2 토픽 형식(`/topic/world` 는 `world_state` 그대로, `/topic/health` 는 `viewer_health`, 관리자 전용 `/topic/admin/events` 의 `action_mismatch`)과 `/topic/events` 경보·확인 읽는 규칙(12.4), 12.6 에 보정·설치·실측 정물·BEV·구역 이력·레이아웃 단건·Scene 상태·이벤트 상세·요약·상향·보고서 조회·일일 일지·VAPID 공개키 경로와 이벤트 목록 페이지 기본값 추가, 단독 평가 결과 조회는 부록 C.4, 데이터(11.3), 결정 기록(16장), 계약 반영 목록(19.1). 계약 v5.3.0: `job_request` 3.1.0·`risk_event_transition` 2.1.0·`risk_event` 2.1.0(마이너), `event_escalate`·`event_escalate_result`·`viewer_health`·`action_mismatch` 1.0.0 신설 |
| v1.3 수정 14 | 2026-10-20 | 명세 정리 5차(S15P31A101-203). GPU 실행 환경(3.12 제목을 ‘GPU 실행 환경·저장소’로): 일본 AP-JP-1 H100 + Network Volume 고정 폐기, 개발·시험은 RunPod 의 그때그때 남은 GPU·지역(볼륨 없음, 컨테이너 디스크)·예비 Vast.ai, 시연 사이트 미정(엘리스 등 후보), 최소 사양(Tier 0 만 24GB급·Tier 0+실험 48GB(80GB 권장)·Ampere 이후·드라이버 최대 CUDA 13.0 이상), 시작 절차 `simai-bootstrap`(GPU 감지·저장 위치·가중치 sha256·SSH 키 3방식·`HF_TOKEN`), Docker Hub 비공개 저장소, 노트북 GPU 대체 경로 안 함(S15P31A101-94·-205). ‘Tier 0 저장 위치’로 용어를 바꾸고 재시작 복구는 저장 위치가 남는 동안만 성립·LIVE 관제는 재시작에도 남는 디스크에서(3.2, 3.12, 5.1, 11, 12.7, 14, 18, 20.4·20.5, 21, 부록 D·E), G3 강제 종료 복구 실측을 시연 환경 디스크로(S15P31A101-92, 3.2, 21.3, 19.1), 국외 이전 고지 받는 자(11.5). 노트북 앱 일시 소켓 오류 재시도(3.10, 17장, S15P31A101-199). 설계 문서·구현과 정합: 분석 워커 접수 중지 기준과 GPU 메모리 예산 초기값(3.2, S15P31A101-60), 재시작 뒤 저장 Track 을 재관측 후보로만 쓰는 규칙·`related_event_id` 연결(3.2, 16장, 부록 C, S15P31A101-54·-178), 같은 렌즈 4대 내부 파라미터 중앙값 합의(4.3, S15P31A101-192), 카메라 간 최선 우선 병합·임시 바닥 가설·측정 없는 시각의 `position_xy`(5.2), 복원 구역 이벤트 운영 절차(18.5)·운영자 종료 REST `POST /api/events/{id}/close`(12.6, S15P31A101-200). 결정 기록(16장), 한계(17장), 용어(부록 A), 미결 정리·추가(19.1), 요구사항 갱신 목록(이 문서). 계약 변경 없음 |
| v1.3 수정 15 | 2026-10-21 | RAG 임베딩 모델 추가(S15P31A101-207, 사용자 결정): BAAI/bge-m3 dense 1024차원, float32·L2 정규화·cosine, 문서·질문 같은 revision, 실행 위치는 추후(3.2 GPU 메모리 예산표, 3.8, 5.4, 16장, 19.1, 부록 A·B). 다른 모델(YOLO26x-pose, TrackTrack, Cosmos3-Nano, 외부 LLM)과 PPE 확장 범위는 바꾸지 않음 |
| v1.3 수정 16 | 2026-10-21 | DB 테이블 보완(S15P31A101-207, ERD 1단계, 사용자 결정): 11장 첫머리에 hypertable(`system_health`·`device_heartbeat`)과 pgvector 임베딩(BAAI/bge-m3 1024차원·cosine), 11.1·11.2·11.3 에 테이블 추가(구성 묶음 항목·적용 응답·정적 객체·엔드포인트, 하트비트·관측 요약, 운영자 확인·종료·상향, 저널 수신 상태, 이벤트 프레임, 티켓 이력, 클립 열람·업로드 발급, 푸시 구독·시험·수신 확인, RAG 조항, `sim.*` 두 테이블), 운영자 기록 구조, 11.6 계정·기기·분석 작업, 11.7 보정·설치 신설, 부록 C.2~C.6 확장 DB 테이블 |
| v1.3 수정 17 | 2026-10-21 | 19.1 에 외부 LLM 미결 추가(S15P31A101-207): 제공사(11.5 국외 이전 고지, 참여자 동의 전)와 호출 설정(I5 작업 대여 시간 제한) |
| v1.3 수정 18 | 2026-10-21 | MVP 현장·계정 범위(S15P31A101-209, 사용자 결정): 공사장 1개 + 계정 JWT(역할 3종) 유지, 기업 → 공사 → 안전관리자 임명 계층은 확장(14장, 16장, 19.1, 부록 C.9). JWT 현장 클레임·`site_id` 확장 대비는 채택 전 |
| v1.3 수정 19 | 2026-10-21 | DB 검증 결정(S15P31A101-210, 사용자 결정): 클립 자동 삭제는 EC2 삭제 작업만(수명 주기 규칙은 클립에 쓰지 않음), 계정·기기·워커 토큰은 EC2 관리 명령, `site`·`camera`·`rule_version` 은 Flyway 시드, 운영자 종료의 REST 멱등 키는 `close_id` 와 따로 저장, hypertable 보관·압축(EC2 디스크 320 GB: 좌표·골격·health 는 상시 삭제 없이 7일 뒤 압축, 운영 지표 30일, 디스크 70% 초과 시 골격 30일)(3.1, 3.8, 11.3, 11.4, 11.5, 11.6, 12.6, 16장, 18.4, 19.1). 계약 변경 없음 |
| v1.3 수정 20 | 2026-10-21 | 기준 데이터 시드(S15P31A101-211, 사용자 결정): 현장 `classroom-1`(교실 테스트베드)·카메라 `cam1`~`cam4` 는 Flyway 시드, `rule_version` 은 AI 규칙 등록부(`simai_ai.risk.rule_catalog`)에서 자동 생성한 Flyway 반복 마이그레이션(11.1, 11.6, 16장). 계약 변경 없음 |
| v1.3 수정 21 | 2026-10-22 | TBM PLANNED 판정 구간(S15P31A101-216, 사용자 결정): EC2 Spring ↔ Risk Engine 컨테이너는 compose 내부망 HTTP/JSON(공개 포트 없음), 메시지 `risk_plan_request`·`risk_plan_result`(12.2). 계획 작업 위치는 선택이고 없으면 컨테이너가 작업면·구역에서 만들며, 결과에 REQUESTED·GENERATED 로 표시하고 EC2 가 위치 목록을 `sim.*` 에 저장(8.3, 11.1, 16장). 기술 스택에 FastAPI(3.8, 부록 B), 생성 규칙·컨테이너 구현은 미결(19.1). 계약 v5.5.0: `risk_plan_request`·`risk_plan_result` 1.0.0 신설 |
| v1.3 수정 22 | 2026-10-22 | TBM 계획 작업 위치 생성 규칙과 Risk Engine 컨테이너 구현(S15P31A101-99): 작업면마다 상판 가운데(넓은 면은 가장 긴 변 가운데에서 0.5 m 안쪽 하나 더), 판정 시각에 유효한 개구부 구역마다 경계 바깥 0.2 m 바닥 위치, 다른 구역 유형에는 만들지 않음, 머리 높이 없음, ID `gen-<작업면·구역 ID>-<순번>`, 결정적(8.3). 19.1 미결 닫음. 계약 변경 없음 |
| v1.3 수정 23 | 2026-10-22 | 명세 정리 6차(S15P31A101-217). Tier 0 공개 주소 환경 변수는 사이트 무관 `SIMAI_PUBLIC_HOST`·`SIMAI_PUBLIC_PORT_<번호>` 먼저, 없으면 `RUNPOD_PUBLIC_IP`·`RUNPOD_TCP_PORT_<번호>`(3.12, 14, S15P31A101-208), Run 기록 `gpu.provider` RUNPOD·VAST·OTHER·LOCAL(20.5). 실험 이미지 HMR `exp02-fs3db`·MapAnything `mapanything`(torch 2.7.1+cu128, Detectron2 sm_80\~sm_120), 시작 절차의 이미지별 가중치 묶음과 sha256, Tier 0 이미지 층 분할, Docker Hub 단일 비공개 저장소 태그 접두, 최소 사양에 Blackwell, 다이제스트를 모르는 Run 의 `env_hash`(3.12, 20.5, 21.5, S15P31A101-207). EC2 컨테이너는 MVP 에서 PLANNED 만·운영 REPLAY 재계산은 확장(3.2)과 Tier 0 파일 입력 시나리오 재생(REPLAY 출처, I1·I0 제어 미송신, JSONL, `input_provenance.frames.source` FILE 구분)(21.3, S15P31A101-215). 외부 파라미터 실행 도구는 한 명 걷기부터·여러 명은 후속·World 정렬 입력은 S15P31A101-102(4.3, S15P31A101-214). 평가한 작업 위치 없음은 ‘위험 없음’이 아님(8.3). 19.1: -208·-207 닫음, 2차 GPU 세션 확인·c3 판정 구조와 기준(S15P31A101-212)·여러 명 외부 파라미터·컨테이너의 위치 없음 표시 채우기 추가, 계약 반영 목록. 요구사항 갱신 목록(이 문서). 계약 v5.6.0: `risk_plan_result` 1.1.0 `no_evaluated_positions`(선택, 마이너) |
| v1.3 수정 24 | 2026-10-22 | 보정 상태의 “확인 필요”(S15P31A101-218, Lead 승인 2026-10-22): `PROVISIONAL_CALIB` 는 판정 위치를 만든 보정이 CALIBRATED 가 아님(PROVISIONAL·DRIFTED, UNCALIBRATED 에서 판정이 나온 경우 방어적으로, UNCALIBRATED 는 여전히 위치 기반 판단 불가)이고 R2·R3·R4 에만 붙으며 등급·발생·해소는 그대로, PLANNED 계획 위치는 해당 없음, 보정 상태를 모르면 CALIBRATED 로 보지 않음(4.4, 7.1). 규칙은 활성 보정 상태를 `world_state` 밖 판정 문맥으로 받음(3.2, 7.1). 계약 v6.0.0: 공통 `confidence_reason` 의미 확장(메이저, enum·메시지 형식 그대로) |
| v1.3 수정 25 | 2026-10-22 | R1 넓은 불확실성(S15P31A101-233): 보정이 CALIBRATED 가 아니면(PROVISIONAL·DRIFTED, UNCALIBRATED 판정은 방어적으로) R1 은 불확실성 범위를 보정 여유 초기값 0.3m(CALIBRATED 기준 한도 15cm × k=2)만큼 수평·VOLUME_3D 높이 모두 넓혀 발생을 이르게·해소를 늦게 하고, 등급·해소 기준은 그대로, `PROVISIONAL_CALIB` 는 붙이지 않음(R2·R3·R4 만), PLANNED 는 넓히지 않음. 4.4 표의 DRIFTED·UNCALIBRATED 행에 R1 을 더함(4.4, 7.1). 첫 운영 배포 전 규칙 보완 예외(Lead 승인 2026-10-22): 첫 실제 운영 배포 전에는 규칙 동작·값을 같은 `rule_version` 안에서 고칠 수 있고(지킬 운영 판정 기록 없음, 시드는 등록부에서 다시 생성), 첫 운영 배포부터는 바뀌면 반드시 새 버전, 배포 시점은 그 MR 이 개정 이력에 기록(11.1, 11.4-1). R1 v1 의 보정 여유·넓힘 상태를 규칙 값으로 시드에 적음. 계약 변경 없음 |
| v1.3 수정 26 | 2026-10-22 | Tier 0 기본 디코더 CPU(S15P31A101-236): 2차 GPU 세션(H100) 실측에서 CPU 디코딩이 NVDEC 보다 빨라(카메라별 스레드 합계 1,576 fps 대 215 fps, NVDEC 도 색 변환은 CPU) LIVE·파일 입력의 기본 디코더를 CPU 로, NVDEC 은 설정(`input.decoder`) 선택으로 둠(5.1). Tier 0 GPU 메모리 예산표의 하드웨어 디코더는 NVDEC 을 고를 때만(3.2). 최소 사양에 컨테이너 vCPU 8개 이상(초기값, 할당량 기준, 시작 절차는 아직 막지 않음)(3.12). 19.1: c3 판정 구조(CPU 카메라별 스레드, NVDEC 참고 측정) 닫고 다음 GPU 세션 확인(vCPU 기록·c3·c4) 추가. 계약 변경 없음 |
| v1.3 수정 27 | 2026-10-23 | 명세 정리 7차(S15P31A101-244, 사용자 결정 2026-10-22). World 좌표계 고정(S15P31A101-102): 2단계 바닥(Z = 0)·중력은 1단계 MapAnything 점구름의 바닥 평면, 사람 발 접지점은 있을 때 확인(어긋나면 비움)하고 3단계에서 재확인, 축은 점구름의 서로 수직(±10°)인 경계 벽 두 묶음(벽 소실점은 교차 확인, 후속), 원점은 지지 점이 가장 많은 수직 경계 벽 쌍과 바닥이 만나는 모서리, X 는 한 벽의 안쪽 법선·Y = Z × X 로 직교화(두 벽 직교 오차는 Y 가 흡수, 기록), 방이 +X·+Y(바닥 점 5% 넘게 −X·−Y 로 0.10 m 넘게 벗어나면 실패·비움), 정하지 못하면 비움, MapAnything 입력은 왜곡을 편 영상·펴진 K·사람 점 제외, 외부 파라미터 도구의 World 정렬 입력 `simai.world_alignment`(4.1, 4.3). VLM 서빙은 vLLM 0.21.0 + `vllm-cosmos3`(커밋 고정) + CUDA 13.0(cu130) + torch 2.11.0, Tier 0 와 같은 가상환경(잠금 하나)이고 프로세스는 Tier 0 / vLLM 서버 / 분석 워커로 분리, Tier 0 의 워커 스택 import 금지, Cosmos3-Nano 가중치 약 31.5GB(`vae`·`sound_tokenizer` 제외), 통합 이미지·프로세스 감시는 S15P31A101-235 2단계(3.8, 3.12, 5.4, 부록 B, S15P31A101-235). HMR 의 Detectron2 는 CPU 전용 빌드 `FORCE_CUDA=0`(3.12, S15P31A101-238). 결정 기록(16장). 19.1: World 원점·축 규칙과 World 정렬 입력 형식 닫음, 벽 소실점 교차 확인·3차 세션 좌표계 고정 수치, 3차 세션 VLM 서빙 확인 추가, 미결(측정 뒤): 3.2·3.12 48GB GPU 메모리 예산(VLM Reasoner VRAM 실측 뒤), 늦은 카메라 150 ms(남는 카메라 2대 미만이면 빼지 않음, S15P31A101-9), R1 보정 여유(G1·G2). S15P31A101-208 의 사이트 무관 공개 주소 환경 변수(3.12)·GPU 제공자 값(20.5)과 14장 표기는 수정 23 에서 반영돼 확인만 함. AGENTS.md 의 기획서 판 표기를 v1.3 으로. 요구사항 갱신 목록(이 문서), 요구사항 FR-86. 계약 변경 없음 |
| v1.3 수정 28 | 2026-10-23 | CI 도구(S15P31A101-28, 팀 결정 2026-10-21): GitLab CI 기본안 → EC2 의 Jenkins(운영 compose 서비스, Nginx `/jenkins/` 뒤). Jenkins 는 호스트 Docker 소켓·운영 DB·운영 비밀 값을 받지 않고 검사를 컨테이너 안에서 직접 돌리며, 서버 시험은 CI 전용 DB 를 쓴다(21.4). SSAFY GitLab 러너 가용 여부 미결 닫음(19.1). 계약 변경 없음 |
| v1.3 수정 29 | 2026-10-23 | 명세 정리 8차(S15P31A101-247, 사용자 결정 2026-10-23). EXP-02(Fast SAM 3D Body)를 별도 승인 확장 후보에서 시연 범위로 승격: 1단계 MHR 리깅 메시 + 포즈 스트림(상·하의 대표색), 2단계 관측 픽셀 UV 텍스처 누적, 3DGS 미사용, Tier 1 안전 판단 권한 없음·관절 inferred·root 위치는 Tier 0 `world_state` 정본·위험 판정 미사용, E-GATE-0\~3 으로 단계별 품질·성능·폴백 검증(1.2\~1.4, 3.1, 3.6, 3.8, 5.1, 5.6, 9장, 13.1, 13.5, 15.2, 16장, 19.2, 20.3\~20.7, 21.1, 부록 B·C·D). LIVE 관제 중 오프라인·배치 EXP(`hmr_*` I5 작업 포함) 금지 규칙에 시연 범위 EXP-02 상시 아바타 스트림 루프 예외를 두고, 추론 전마다 Tier 0 지연 감시 표시 파일(`$SIMAI_ROOT/state/tier0.live`)로 지연 초과·모름이면 멈추며 뷰어는 0.5 s 뒤 캡슐로 복귀(3.2, 5.1, 5.6, 12.5, 17장, 20.5, AGENTS 작업 방식). 아바타 포즈 스트림 계약(S15P31A101-249, 묶음 6.2.0): I1 과 분리된 I6(`wss://<ec2>/ws/worker/avatar`, 워커 토큰)의 `avatar_pose_batch`·`avatar_asset` 1.0.0 과 I2 `/topic/avatar` 의 `avatar_frame` 1.0.0(축각 라디안 하나 `axis_angle`, MHR 127관절 순서·`joint_parents_sha256`, 위치 없음, 포즈마다 `/api/avatars/...` 자산 경로 포함, 브로커 밀림 시 버림), 뷰어 250 ms 재생 버퍼 slerp·캡슐 복귀 5조건(수신 끊김 0.5 s, 촬영 시각 차 500 ms, `IDENTITY_UNCERTAIN`·`identity_revision` 불일치, `calibration_version` 불일치, 자산 미준비·검증 실패), 스트림은 `native_pose` 만 싣고 `canonical_motion` 은 오프라인 기록 `experiments/human_mesh_output`(2.0.0)에 유지, EC2 `avatar_asset` 버전 추가 전용 기록·선택 1 Hz 포즈 다운샘플 저장과 관제 앱 인증(5.6, 11.5, 12장 머리말·12.1·12.2·12.4·12.5, 13.1·13.2, 14장, 16장, 19.1, 20.4). 촬영·시연·데이터 활용의 참여자 별도 동의와 시연·공개 자료 ‘사람 없음 또는 동의자만’ 제한을 제거하고 기관(SSAFY) 허가 기준으로 정리, 개인정보보호법 검토는 프로젝트 범위에서 하지 않는다는 사실을 기록(11.5, 12.5, 16장, 17장, 19.1, 20.3·20.4). 얼굴·재식별 원칙(AGENTS 규칙 8 개정): 외형 텍스처에 관측된 얼굴 픽셀 허용(`face_policy` 기본 OBSERVED, BLURRED·EXCLUDED 선택, `face_ratios` 보간 없음), 생성형 얼굴 복원·표정 추정 금지(30\~50px 물리 한계, 지어낸 얼굴 금지), 재식별(Re-ID)은 전신 임베딩을 기하 게이트 뒤 보조 비용으로만 쓰고 Tier 0 위치·ID 정본을 덮지 않으며 도입은 10/30 이후 G2 측정 뒤(지금 범위 아님, 19.1 미결), 외부 LLM 에는 텍스트만 전송(1.4, 11.5, 13.2, 16장, 17장, 19.1, 20.3, AGENTS 규칙 8). 시연 기준 GPU 는 80GB(H100)이고 48GB 는 Tier 0 + 분석 동시 실행에 모자랄 수 있음, 3.2 예산표 VLM 0.55 는 48GB 기준 초기값이라 근거가 약해 3차 GPU 세션 v1 에서 Reasoner VRAM 실측 뒤 고침(값은 지금 불변)(3.12, 19.1). 요구사항 FR-3·92·97·98·99·101·103\~106·109·110·NFR-11·12·14·16·19·23·33 반영(요구사항 갱신 목록의 NFR-16 이월 해소). 이 커밋의 계약 변경 없음(-249 에서 계약 6.2.0 반영됨) |
| v1.3 수정 30 | 2026-10-23 | 명세 정리 9차(S15P31A101-265). 통합 이미지(`unified`) 같은 가상환경(torch 2.11.0+cu130)에 MapAnything(`map-anything` 커밋 고정 휠 + 잠금 `scene` 묶음 + DINOv2 허브 사본)을 넣고(Tier 0 코드의 워커·장면 스택 import 금지), 시작 절차(`simai-bootstrap`)에 상시 감시 모드(`SIMAI_SERVICES=tier0,vllm,worker`)와 일회성 장면 준비 모드(`simai-bootstrap scene`: LIVE 표시 파일·GPU 사용 중 > 1024 MiB 시 종료 코드 75 거부, `SIMAI_SERVICES` 에 `scene` 혼합 시 64, 실행 중 Tier 0 기동 시 장면 그룹 선점 종료 75), Tier 0 시작 잠금·장면 그룹 회수(최악 약 40초, 미회수·신원 미확인 시에도 경고 및 종료 75 기록 후 Tier 0 우선 시작, 의도된 정책), torch 2.7.1 `mapanything` 이미지와의 동등성 비교(`compare_envs.py`, 3차 GPU 세션 u3·u4 ↔ m1·m2) 반영(3.12, 19.1, 20.5, 부록 B, S15P31A101-246·-250). Jenkins CI(21.4, 19.1): GPU·torch 없는 Linux 로직 층, 첫 단계 `git clean`(`.venv`·`node_modules`·`.gradle` 제외, S15P31A101-258), MR 빌드는 바뀐 경로 파트만 검사(`deploy/` 변경도 `ai` 단계 포함)하고 `ai` 단계는 정적 import 그래프·보조 모듈 전이·경로 규칙(`ai/scripts/select_tests.py`)으로 고른 시험만 실행(빈 출력 생략, `ALL` 전체), `dev`·`master` push 는 전체 실행(머지 뒤 안전망)(S15P31A101-28·-255·-260). S15P31A101-208 환경 변수·제공자(3.12·14·20.5) 반영 재확인(수정 23 에서 반영). 요구사항 FR-102·106·NFR-19·28 반영(요구사항 갱신 목록의 NFR-19 이월 해소). 계약 변경 없음 |
| v1.3 수정 31 | 2026-10-26 | 명세 정리 10차(S15P31A101-284). R3 근거 조항: 산업안전보건기준규칙 제32조(①2호 안전대)·제44조(①안전대 부착설비), 운영 R3 의 `legal_basis` 에 조 단위 ID 로 연결(교실 시험 프로파일 없음), 규칙 값 시드가 비어 있으면 채우고 다른 값은 첫 운영 배포 전 갱신·뒤 멈춤(7.1, 7.2, 11.1, S15P31A101-27·-256). 운영자 상향 등급은 관측 해소까지 바닥값·재개하면 비움, `risk_event_transition` 2.1.0 일괄 송신(5.4, 12.3, S15P31A101-272). 외부 LLM: SSAFY GMS(OpenAI API 중계)를 거쳐 OpenAI, 모델 `gpt-6.1-sol`, `temperature` 미전송, JSON 응답의 인용·등급·확률 검사와 작업 안 1회 재생성, 국외 이전은 ‘SSAFY GMS 를 거쳐 OpenAI(국외)로 텍스트만’(3.1·3.8, 5.4, 11.5, 12.5, 16장, 17장, 부록 A·B, S15P31A101-225). `tbm_report` 입출력(`job_request` 3.3.0·`job_callback` 2.3.0, 묶음 6.3.0)과 `bev_floorplan` 입력(`job_request` 3.2.0, S15P31A101-239), 워커 객체 서명 API `POST /api/worker/jobs/{job_id}/objects/sign`(12.5). RAG: bge-m3 revision `5617a9f…` 고정·CLS + L2·노트북 CPU fp32 배치 1·조 단위(1,000자 넘으면 항·호 경계) 청크·질문 템플릿 사전 임베딩 `rag_query_template`(3.2 GPU 예산표, 3.8, 5.4, 11장, 부록 B, S15P31A101-27). VLM 입력 얼굴 처리 세부(keypoint 영역, 머리 윗부분 유지, BLURRED 모자이크·EXCLUDED 단색, 처리 못 한 프레임은 넘기지 않음)(5.4, S15P31A101-270). G3 결과 보고서 `docs/design/g3-report.md` 연결과 나눈 항목의 판정 규칙(Tier 0·노트북·EC2 몫 모두 PASS)(15.1, 부록 D.4, S15P31A101-113). 19.1: RAG 임베딩 설정·외부 LLM 제공사와 호출 설정·R3 근거 조항 닫음, 외부 LLM 호출 방식(GMS 공식 OpenAI Responses API 전환, S15P31A101-285) 추가. 설계 문서 숫자: `db-erd.md`·`db-verification.md` 테이블 70개, `contracts/README.md` 생성 타입 개수(Java 44, 센 방법). 요구사항 FR-20·68·89·116·142 반영. 계약 변경 없음 |

v1.0 \~ v1.2 r4 수정 7 의 이력은 [`history.md`](history.md) 에 있다.

## 장과 파일

장 번호(§)와 파일이 일대일이다. Jira·MR·코드 주석의 "기획서 §5.2" 는 `05-processing.md` 의 5.2 절을 뜻한다.

| 장 | 제목 | 파일 |
|---|---|---|
| §1 | 개요와 범위 | [`01-overview.md`](01-overview.md) |
| §2 | 설계 원칙 | [`02-principles.md`](02-principles.md) |
| §3 | 시스템 구성과 배치 | [`03-system.md`](03-system.md) |
| §4 | 좌표·단위 규약과 캘리브레이션 | [`04-coordinates.md`](04-coordinates.md) |
| §5 | 처리 계층 | [`05-processing.md`](05-processing.md) |
| §6 | 신뢰성 명세 | [`06-reliability.md`](06-reliability.md) |
| §7 | 위험 규칙과 대응 | [`07-risk-rules.md`](07-risk-rules.md) |
| §8 | 기능 명세 | [`08-features.md`](08-features.md) |
| §9 | 문제-기능-게이트 추적 요약 | [`09-traceability.md`](09-traceability.md) |
| §10 | 알림 채널 | [`10-alerts.md`](10-alerts.md) |
| §11 | 데이터 | [`11-data.md`](11-data.md) |
| §12 | 인터페이스 명세 | [`12-interfaces.md`](12-interfaces.md) |
| §13 | 뷰어 | [`13-viewer.md`](13-viewer.md) |
| §14 | 보안 | [`14-security.md`](14-security.md) |
| §15 | 검증 게이트와 시나리오 데이터셋 | [`15-gates.md`](15-gates.md) |
| §16 | 결정 기록 | [`16-decisions.md`](16-decisions.md) |
| §17 | 한계와 위험 | [`17-risks.md`](17-risks.md) |
| §18 | 운영 절차 | [`18-operations.md`](18-operations.md) |
| §19 | 미결 사항과 로드맵 | [`19-roadmap.md`](19-roadmap.md) |
| §20 | GPU 확보 이후 대형 Experiment 운영과 채택 절차 | [`20-experiments.md`](20-experiments.md) |
| §21 | 개발·테스트 기반 | [`21-dev-test.md`](21-dev-test.md) |
| 부록 A | 용어집 | [`appendix-a-glossary.md`](appendix-a-glossary.md) |
| 부록 B | 모델·도구·라이선스 목록 | [`appendix-b-models.md`](appendix-b-models.md) |
| 부록 C | 확장 트랙 | [`appendix-c-extensions.md`](appendix-c-extensions.md) |
| 부록 D | 회귀 시험 목록 | [`appendix-d-regression.md`](appendix-d-regression.md) |
| 부록 E | 시연 시나리오 | [`appendix-e-demo.md`](appendix-e-demo.md) |
| — | 이전 개정 이력(v1.0 \~ v1.2 r4 수정 7) | [`history.md`](history.md) |

## 에픽별로 먼저 읽을 장

| 에픽 | 먼저 읽을 장 |
|---|---|
| -10 개발 기반 | §3.12, §20.5, §21, 부록 B, 부록 D |
| -2 관제 플랫폼 기반 | §3.1~3.2, §3.8, §11, §12.1~12.2, §14 |
| -3 카메라·현장 게이트웨이 | §3.3~3.5, §3.9~3.10, §3.12, §12.7(I0), §18 |
| -11 자동 보정·좌표계 | §4, §15.1(G1), 부록 D |
| -12 실시간 추적·World 상태 | §5.1~5.2, §6.1~6.2, §12.3(I1), §15.1(G0·G2) |
| -4 위험 판정 | §7, §6.4~6.5, §15.1(G3·G6), 부록 D |
| -13 관제 백엔드·알림 | §3.2, §6.5, §10, §11, §12.4~12.6(I2·I3·I5) |
| -14 VLM 근거·TBM 보고서 | §5.4, §8.3, §12.5(I5), §14, §15.1(G7) |
| -5 3D 관제 뷰어 | §4.2, §7.5, §13, §15.1(G5) |
| -15 EXP-01 정적 배경 복원 | §5.3, §20.1~20.2, §20.4~20.6 |
| -16 EXP-02 인체 메시·외형 | §5.6, §20.1, §20.3~20.6 |
| -6 검증·시연 | §15, §17, §18, 부록 D, 부록 E |
| -17 기획·발표 | §1, §9, §16, §19, 부록 C, 부록 E |

에픽이 바뀌면 이 표만 고친다. 명세 파일 구조는 시스템 구조를 따르고 작업 단위(에픽)를 따르지 않는다.

## 갱신 규칙

- 명세를 바꾸는 MR 은 위 **개정 이력**에 한 줄을 더한다(버전 `v1.3 수정 N`, 날짜, 내용, 바뀐 절).
- 본문에는 개정 꼬리표를 달지 않는다. 무엇이 언제 바뀌었는지는 개정 이력에만 적는다.
- 절 번호를 바꾸거나 재사용하지 않는다. 새 내용은 뒤에 붙이고, 없애는 절은 “폐기(버전)”, 다른 곳으로 옮긴 절은 “이동(버전) → 옮긴 곳” 한 줄만 남긴다.
- 코드·계약과 함께 바뀌는 명세는 같은 MR 에서 바꾼다. 계약 필드 정의의 정본은 `contracts/` 이고 명세는 의미와 규칙을 적는다.
- 요구사항 명세서에 영향이 있으면 MR 본문에 `요구사항 FR-n 갱신 필요` 를 적는다. 요구사항 명세서는 마일스톤(M1·M2·M3)과 제출 전에 몰아서 맞춘다.
- 장 파일 안의 제목 단계는 `# n. 장` → `## n.m 절` → `### 소절` 이다.

## 요구사항 갱신 목록

요구사항 명세서(`../requirements.md`)는 마일스톤·제출 전에 몰아서 맞춘다. 그때까지 명세 MR 이 남긴 `요구사항 FR-n 갱신 필요` 를 여기에 모으고, 요구사항 명세서에 반영하면 그 줄을 지운다.

| ID | 지금 요구사항 | 바꿀 내용 | 근거 |
|---|---|---|---|
| FR-23 | 내부 파라미터 고전 기준 잠정 기본·C110 비교로 확정, 같은 렌즈 4대 공유 | AnyCalib 기본·GeoCalib 예비·고전 기준 예비와 직선성 확인, 실패하면 비움, 4대 추정값 중앙값 합의(과반이 없으면 비움) | v1.3 수정 8·14, 4.3 (S15P31A101-192) |
| FR-64 | 병합만 | 자동 분할 추가(`split_from`), 수동 교정 없음(별도 티켓) | v1.3 수정 11, 5.2 (S15P31A101-201) |
| FR-129 | world_state 트랙 필드 | `split_from` 추가 | v1.3 수정 11, 12.3 (S15P31A101-201) |
| FR-78 | 관측 근거로만 해소 | 운영자 종료 예외(구역 대상만, 검증 상태 불변, `POST /api/events/{id}/close` → I1 `event_close`) | v1.3 수정 10·14, 6.4, 12.6 (S15P31A101-200) |
| FR-82 | World Track 은 복원하지 않고 새로 시작 | 저장 Track 은 재관측 후보로만(새 ID 유지, `RESTORED_TRACK_CANDIDATE`, 30초, 도달 거리), 재발생 이벤트 `related_event_id` | v1.3 수정 14, 3.2 (S15P31A101-54·-178) |
| FR-101 | I5 작업 대여 | Tier 0 우선으로 시작하지 않았거나 멈춘 작업은 시도 횟수를 쓰지 않고 반납 | v1.3 수정 9, 3.2, 12.5 (S15P31A101-197) |
| FR-50 | RunPod Tier 0(일본 AP-JP-1)로 전송 | 실행 사이트 무관(3.12) | v1.3 수정 14 |
| FR-18 | 개구부·가설 비계·장비 주차 구역 가상 배치 | 계획 작업 위치는 선택(없으면 Risk Engine 컨테이너가 작업면·구역에서 생성) | v1.3 수정 21, 8.3, 11.1 (S15P31A101-216) |
| FR-19 | 계획 모드 평가(PLANNED, 수평 거리·높이 차) | EC2 내부 `risk_plan_request`·`risk_plan_result`(compose 내부망 HTTP/JSON), 판정에 쓴 위치 목록(REQUESTED·GENERATED)을 `sim.*` 에 저장 | v1.3 수정 21, 8.3, 12.2 (S15P31A101-216) |
| FR-54 | 장애별 표시 | 노트북 앱 일시 소켓 오류(WinError 10014)는 짧게 다시 시도, 넘기면 재접속, 그동안 정상으로 보지 않음, 앱이 멈추지 않음 | v1.3 수정 14, 3.10 (S15P31A101-199) |
| FR-59·FR-99·FR-141·NFR-32 | Network Volume 에 클립·2D 관측 보관·정리 | Tier 0 저장 위치(3.12)로 | v1.3 수정 14 |
| FR-131 | 노트북 GPU 현장 프로파일 대체 경로 유지·측정 | 하지 않음(현장 프로파일은 코드 구조로만) | v1.3 수정 14, 3.12 (S15P31A101-94) |
| FR-138 | 공개 포트 `RUNPOD_TCP_PORT_*` | 사이트 무관 `SIMAI_PUBLIC_HOST`·`SIMAI_PUBLIC_PORT_<번호>` 먼저, 없으면 `RUNPOD_PUBLIC_IP`·`RUNPOD_TCP_PORT_<번호>`(값이 틀리면 오류, 둘 다 없으면 등록·기동 오류) | v1.3 수정 14·23, 3.12 (S15P31A101-208) |
| NFR-13 | RunPod 볼륨의 비밀 값 | 실행 사이트 비밀 환경 변수·저장 위치 비공개 폴더, `HF_TOKEN` 은 가중치 받을 때만 | v1.3 수정 14, 3.12 (S15P31A101-205) |
| NFR-19 | GPU 격리, 상한 | GPU 메모리 예산 초기값(Tier 0 0.20·분석 워커 0.70·VLM 0.55/동시 4)과 시작 검사, 48GB 미만 GPU 에는 분석 부하 안 올림, 접수 중지 기준(창 최댓값 600ms·대기 상한 초과 1건·30초 유지) | v1.3 수정 14, 3.2 (S15P31A101-60) |
| NFR-20 | 운영 GPU·운영 리전(일본 AP-JP-1 H100) 실측 | 시연 사이트·시연 GPU 실측 | v1.3 수정 14, 6.6 |
| NFR-30 | 한 시점에 한 리전 | 한 시점에 GPU 하나, 사이트·지역 고정 안 함, 볼륨이 없으면 지우기 전에 결과 회수 | v1.3 수정 14, 3.12 |
| 개요·기획서 대응표 | 시스템 개요의 ‘일본 AP-JP-1 H100’, 대응표 8·37·42 행(3.12 제목, 19.1 Network Volume 구조) | 3.12 ‘GPU 실행 환경·저장소’, 19.1 ‘Tier 0 저장 위치 구조’ | v1.3 수정 14 |
| FR-99·NFR-32 | 클립은 객체 저장소 수명 주기 규칙으로 삭제·확인 | EC2 클립 삭제 작업(`delete_after` 지난, 보존 표시 없는 클립만)으로 삭제·확인, 수명 주기 규칙은 클립에 쓰지 않음 | v1.3 수정 19, 11.5 (S15P31A101-210) |
| FR-107 | Run 기록 필드 | GPU 제공 사이트 `gpu.provider`(RUNPOD·VAST·OTHER·LOCAL), 이미지 다이제스트를 모르는 Run 은 `env_hash` 를 다이제스트로 채우지 않음 | v1.3 수정 23, 20.5, 21.5 (S15P31A101-208·-207) |
| FR-23 | 한 명이 걷고 부족하면 여러 명(모두 보정에 씀) | 지금 외부 파라미터 실행 도구는 한 명 걷기 녹화만(여러 명 동시는 카메라 간 사람 대응 단계 후속), World 정렬 입력 형식은 S15P31A101-102 | v1.3 수정 23, 4.3 (S15P31A101-214) |
| FR-127·NFR-28 | 녹화 파일 입력·결정적 시나리오 재생 | Tier 0 파일 입력 `input.mode: file`: REPLAY 출처, I1·I0 제어 채널 없음(실제 알림·저널 없음), `world_state`·`risk_event` JSONL, 파일에서는 `input_provenance.frames.source` FILE 로 LIVE 와 구분 | v1.3 수정 23, 21.3 (S15P31A101-215) |
| FR-128 | EC2 컨테이너(PLANNED·REPLAY·WHAT_IF) | MVP 내부 API 는 PLANNED 만, 운영 REPLAY·WHAT_IF 재계산은 확장(부록 C), Tier 0 파일 재생은 개발·시험 도구 | v1.3 수정 23, 3.2, 21.3 (S15P31A101-215) |
| FR-19 | 계획 모드 평가 결과 | 평가한 계획 위치가 없으면 ‘위험 없음’이 아니라 ‘평가한 작업 위치 없음’으로 보임(`risk_plan_result` 1.1.0 `no_evaluated_positions`, 선택) | v1.3 수정 23, 8.3 (S15P31A101-217) |
| FR-66 | R1 위험 구역 진입(불확실성 범위 k=2 + 0.3m) | 보정이 CALIBRATED 가 아니면(PROVISIONAL·DRIFTED, UNCALIBRATED 방어적) 범위를 보정 여유 0.3m 만큼 넓힘(수평·높이), 확인 필요는 붙이지 않음, PLANNED 해당 없음 | v1.3 수정 25, 4.4, 7.1 (S15P31A101-233) |
| FR-78 | “확인 필요” 이유 PROVISIONAL_CALIB(보정 PROVISIONAL) | 보정이 CALIBRATED 가 아님(PROVISIONAL·DRIFTED, UNCALIBRATED 판정은 방어적으로), R2·R3·R4 만, PLANNED 해당 없음 | v1.3 수정 24, 4.4, 7.1 (S15P31A101-218) |
| FR-23 | MapAnything 초기 추정 | 2단계 World 좌표계 고정: MapAnything 점구름 바닥 평면으로 Z = 0·중력(발 접지점은 있을 때 확인, 어긋나면 비움), 서로 수직인 경계 벽 두 묶음으로 축, 지지 점이 가장 많은 수직 경계 벽 쌍과 바닥의 모서리로 원점, X 는 한 벽의 안쪽 법선·Y = Z × X(방이 +X·+Y, 바닥 점 5% 넘게 −X·−Y 로 0.10 m 넘게 벗어나면 실패), 정하지 못하면 비움(PROVISIONAL) | v1.3 수정 27, 4.1, 4.3 (S15P31A101-102) |
