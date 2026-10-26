# 19. 미결 사항과 로드맵

## 19.1 미결 사항

- [ ] 싸피 Wi-Fi에서 카메라 접속과 기기 간 RTSP 수신이 가능한가, 공유기를 확보할 수 있는가
- [ ] Tapo C110 RTSP 스트림의 촬영 시각: RTCP 시각이 불안정함을 확인(1차 측정). 긴 구간 회귀 보정 가능 여부는 G0 LED 시험으로 판단
- [ ] 공유기 환경에서 카메라 1대 10분 RTSP 측정(S15P31A101-9 완료 조건)
- [ ] 시연 GPU 에서 4대 동시 처리 FPS(G0)와 노트북 → GPU 사이트 업로드 안정성
- [x] RAG 임베딩 BAAI/bge-m3 의 가중치 revision·실행 위치·조항 청크·배치 크기(S15P31A101-27, 5.4·11장): revision `5617a9f61b028005a4858fdac845db406aefb181`, CLS 풀링 + L2 정규화, 노트북 CPU fp32·배치 1, 조 단위(1,000자 넘는 조만 항·호 경계로 나눈 청크 `#k`), 질문은 TBM 템플릿 사전 임베딩(`rag_query_template`). 자유 문장 질의 임베딩 위치는 확장
- [x] 외부 LLM “GPT 6.1 sol” 의 제공사와 호출 설정(S15P31A101-225, 5.4·11.5·12.5): SSAFY GMS(OpenAI API 중계, 엔드포인트 `https://gms.ssafy.io/gmsapi/` + OpenAI 경로, 형식은 OpenAI 공식 레퍼런스와 같음)를 거쳐 OpenAI 로 보낸다. 모델 `gpt-6.1-sol`, `temperature` 미전송(기본값만 받음), 응답 대기 기본 60초로 작업 시간 제한 안에서 두 번까지(실측 약 19초). 국외 이전은 11.5. 호출 방식은 아래 미결
- [ ] 외부 LLM 호출 방식: GMS 공식 방식(OpenAI Responses API)으로 전환, 지금은 Chat Completions 로 동작 확인(2026-10-26). S15P31A101-285
- [ ] 다중 공사·기업 계정(부록 C.9)의 역할 계층·12.6 경로, JWT 현장 클레임·`site_id` 확장 대비 채택 여부(MVP 이후, 팀 회의 뒤)
- [ ] 해상도·화각이 바뀐 뒤 재보정 절차(내부 파라미터 재추정 → 번들 조정)와, 틀어짐 귀속이 불확실할 때 운영자가 할 일(4.4, 18장)
- [ ] 지급 EC2 의 네트워크 전송 한도(2D 관측은 Tier 0 저장 위치에 두므로 부담이 줄었다), EC2 와 Lightsail 중 어느 것인지. 디스크는 320 GB 로 확인했다(11.4, 11.5)
- [ ] 시연 GPU 사이트(엘리스 클라우드 GPU VM 등이 후보, 시연 기준 GPU 는 80GB(H100 급), 3.12): 최소 사양, 인스턴스를 다시 켜도 남는 디스크, I0 Direct TCP 용 공개 포트, 드라이버·Container Toolkit, 노트북에서 잰 왕복·업로드. 리허설 전(D-14) GPU 확보와 카메라 4대 기준 `capture_to_site_alert` 실측
- [ ] 시연 저장 위치(Network Volume 대신 시연 환경 디스크)에서 G3 강제 종료 복구 실측(S15P31A101-92, 15.1 G3·부록 D.4): 저널 fsync 지연, 원자적 rename·파일 잠금, 강제 종료 직후 잠금 해제 지연
- [ ] Network Volume 폐기 뒤 녹화 세션·내부 테스트 영상의 보관 위치(15.3, 11.5)와 그 주소 형식: `recording_manifest.storage.uri` 는 스키마 패턴으로 `volume://<datacenter>/…`·`s3://` 만 받아 데이터센터가 없는 블록 스토리지·컨테이너 디스크·노트북 보관을 적을 수 없고(패턴 변경 필요 여부), 공통 `uri` 는 패턴 제한 없이 설명에서 두 방식만 주소 의미로 정의한다(의미 정의 보강 여부)
- [x] World 원점·축 자동 선택 규칙(S15P31A101-102, 4.1·4.3 2단계): 바닥은 MapAnything 점구름 바닥 평면, 축은 서로 수직인 경계 벽 두 묶음, 원점은 지지 점이 가장 많은 수직 경계 벽 쌍과 바닥이 만나는 모서리, X 는 한 벽의 안쪽 법선·Y = Z × X 로 직교화(방이 +X·+Y 쪽, 바닥 점 5% 넘게 −X·−Y 로 0.10 m 넘게 벗어나면 실패·비움)
- [ ] World 축의 영상 쪽 교차 확인(펴진 영상 직선의 벽 소실점, 4.1·4.3)과, 3차 GPU 세션의 알려진 K MapAnything 실행(기본·apache 가중치)으로 좌표계 고정 정식 수치(바닥·벽·축 근거, 카메라 간 재투영)를 내고 가중치 하나를 고르기(`docs/design/world-frame-from-mapanything.md`)
- [x] 촬영·시연·데이터 활용 허가 기준(2026-10-23 사용자 결정, 11.5·16장): 참여자 별도 동의서·비참여자 안내·시연 및 공개 자료의 ‘사람 없음 또는 동의자만’ 제한은 두지 않고 기관(SSAFY) 허가 기준으로 정리했다. 개인정보보호법 검토는 이 프로젝트 범위에서 하지 않는다
- [x] 개발·시험 GPU 사이트(RunPod 남는 지역·Vast.ai)로 영상(내부 테스트·녹화 영상 포함) 전송 범위(2026-10-23 사용자 결정, 11.5·16장): 기관(SSAFY) 허가 기준으로 정리했고 별도 참여자 동의·국외 이전 고지 조건은 두지 않는다
- [x] R3 높이 기준의 정확한 근거 조항(S15P31A101-27·-256, 7.1·7.2): 산업안전보건기준규칙 제32조(①2호 안전대)·제44조(①안전대 부착설비). 운영 R3 의 `legal_basis` 에 조 단위 ID 로 연결(교실 시험 프로파일은 없음), 규칙 값 시드가 비어 있으면 채움
- [x] TBM 계획 작업 위치 자동 생성 규칙과 Risk Engine 컨테이너의 FastAPI 구현(S15P31A101-99, 8.3·12.2): 작업면 상판 가운데(넓은 면은 가장자리 0.5 m 안쪽 하나 더)와 유효한 개구부 경계 바깥 0.2 m, 머리 높이 없음(8.3). 컨테이너는 `deploy/risk-engine`(CPU, compose 내부망 `risk-engine:8080`, 포트 공개 없음). 같은 커밋 두 이미지의 D-G3-04 대조는 다음 Tier 0 이미지 빌드 때 `deploy/scripts/risk_engine_g304.sh`
- [x] Tier 0 저장 위치 구조(`$SIMAI_ROOT`, 3.12): 시작 절차가 지정값 → 볼륨(`/workspace/simai`) → 컨테이너 디스크(`/srv/simai`) 순으로 고른다. 그 아래 `weights`(가중치), `datasets`, `recordings`, `obs2d`, `engines`(TensorRT 엔진 캐시), `state`(Tier 0 저널·체크포인트·구성 묶음 보관본·보정 기준·실행 중 표시 파일, 단일 작성자·손으로 지우지 않음), `locks`(Tier 0·장면 준비 잠금), `queue`(GPU 큐 상태·Run 폴더), `runs`(실험·검증 산출물), `setup`, `logs`가 있다. 비공개 폴더 `.simai-private` 는 `$SIMAI_ROOT` 안이 아니라 옆(`$SIMAI_ROOT/../.simai-private/`)이며 TLS 키·토큰 파일 환경 변수가 그 경로를 가리킨다. E-GATE-0 때의 `envs`·`third_party`·`tools` 는 새로 만들지 않는다. `obs2d` 는 2D 관측 Parquet(11.2)이다: Tier 0 에 `SIMAI_OBS2D_ROOT=$SIMAI_ROOT/obs2d` 를 준다(코드 기본값 없음, 없으면 오류). 파일은 `{date}/{hour}/{camera_id}/obs2d.{camera_id}.{YYYYMMDDTHHMM}Z.{session_id}.{part:04d}.parquet`(UTC, 한 시간 단위)이고 기본 14일(7\~14일) 뒤 지운다. `recordings` 는 녹화 세션 디렉터리 `recordings/<session_id>/`(세션 목록·해시는 `recording_manifest`, 15.3. 보관 위치는 위 미결)다
- [x] SSAFY GitLab CI 러너 사용 가능 여부와 Jenkins CI 구성: 공유·그룹 러너가 없고 Container Registry 도 꺼져 있다(2026-10-21 확인). CI 는 EC2 의 Jenkins(GPU·torch 없는 Linux 로직 층, 첫 단계 `git clean`, 21.4)로 하며, MR 빌드는 정적 import 그래프 기반 선택기(`ai/scripts/select_tests.py`)가 고른 `ai` 시험만 돌리고(빈 출력 생략, `ALL` 전체, `deploy/` 변경 포함) `dev`·`master` push 는 전체를 돌린다(S15P31A101-28·-255·-258·-260)
- [ ] 아직 계약에 없는 v1.3 규칙: 노트북 송신 큐 버림·연결 없음 버림을 `laptop_heartbeat` 에 싣는 필드(Tier 0 수신 집계는 `health` 에 반영), 오프라인 실험 기록 `experiments/human_mesh_output` 의 `face_policy` OBSERVED 추가·`consent_ref` 정리(실시간 아바타 스트림은 반영됨, 기록 스키마 판 변경은 S15P31A101-227 과 함께). 계약에 반영한 것: `confidence_reasons`(7.1), 이벤트 재개 `resumed`·`resume_count`(6.4), 확인 대상 `event_sequence`(3.10), `world_state.config_version`(3.2), 판정이 아닌 전이의 `evidence_ref` null(3.2), 노트북 송신 큐 버림 `health.cameras[].frames.laptop_skipped`·`unattributed_gap`(12.7), 이벤트 클립 `media_clip`·`media_clip_retention`(11.3), 현장 경보 목록 `health.active_site_alerts`(3.10), 시계 차이 `clock_sync_request`·`clock_sync_response`·`health.laptop_clock_offset`(3.9), 엔드포인트 등록 `tier0_endpoint`(3.12), 2D 관측 요약 `observation_summary`(11.2), I5 작업 대여 `job_lease_request`·`job_request`·`job_heartbeat`·`job_callback`·`job_lease_status`(12.5), 녹화 manifest `recording_manifest`(15.3), 보정 출처 값 정리 `calibration_source`(20.4), `world_state` 트랙의 지지 표면 출처 `surface_source`·병합 별칭 `aliases`와 `health.ec2_world_state_drops`(Tier 0 → EC2 world_state 송신 버림 누적)(5.2, S15P31A101-170), `world_state` 트랙의 `head_z_validity`(5.2)와 `event_state_snapshot.journal_offset` null(아직 저널 기록 없음, 3.2)(S15P31A101-172), `world_state` 2.0.0 UNCERTAIN `head_z` 값 싣기(5.2)와 `health` 2.0.0 `pipeline_lag_ms` null·`pipeline_lag_lower_bound_ms`(6.1)·`tier0_warnings`(6.5)·`cameras[].sync_valid`(6.1)(S15P31A101-179), I5 반납 `job_callback.error.consume_attempt`·`job_request` 3.0.0 시도 번호(12.5)·`health` 2.1.0 `tier0_warnings` `LIVE_MARKER_WRITE_FAILED`(6.5)·`run_record.experiment_id` `VAL-01`(20.5)(S15P31A101-197), 운영자 종료 `event_close`·`event_close_result` 와 `risk_event_transition`·`risk_event` 2.0.0 운영자 종료 해소 `resolution`(6.4)(S15P31A101-200), `world_state` 2.1.0 분할 출처 `split_from`(5.2)(S15P31A101-201), 운영자 상향 `event_escalate`·`event_escalate_result`·전이 `escalation`(5.4), I5 `bev_floorplan`·`incident_summary`·`tbm_report` `report_kind`(12.5), I2 `/topic/world`(`world_state` 그대로)·`/topic/health`(`viewer_health`)·`/topic/admin/events`(`action_mismatch`)(12.4)(S15P31A101-206), `risk_plan_result` 1.1.0 평가한 위치 없음 `no_evaluated_positions`(8.3)(S15P31A101-217), EXP-02 아바타 포즈 스트림 I6 `avatar_pose_batch`·`avatar_asset` 과 I2 `/topic/avatar` 의 `avatar_frame` 1.0.0(계약 묶음 6.2.0, 12.2·12.4)(S15P31A101-249)
- [ ] 카메라 간 시간 동기화 측정과 무효 판정(지금은 측정이 없어 `health.cameras[].sync_valid` 를 null 로 보고하고, Tier 0 내부 융합은 모든 카메라를 유효로 본다, 6.1·6.5)
- [ ] World ID 수동 교정(운영자 분할·병합 명령. 설계 문서에 계약안 `identity_correction`·`identity_correction_result` 만 있고 구현하지 않음, 5.2. 서버·화면 작업과 맞춰 별도 티켓), 노트북 “AI 분석 지연” 표시에 쓸 신호(지금 노트북에 오는 신호가 없어 표시하지 않는다, 3.10)
- [x] Tier 0 공개 주소 환경 변수(S15P31A101-208, 3.12): `SIMAI_PUBLIC_HOST`·`SIMAI_PUBLIC_PORT_<번호>` 먼저, 없으면 `RUNPOD_PUBLIC_IP`·`RUNPOD_TCP_PORT_<번호>`. Vast 이름은 문서로 확인하지 못해 읽지 않는다. Run 기록 `gpu.provider` 에 VAST·OTHER 추가(run_record 2.2.0, 20.5)
- [x] 실험 이미지(HMR `exp02-fs3db`·MapAnything `mapanything`, torch 2.7.1+cu128, 3.12)와 가중치 묶음(`exp02`·`static`), GPU 세션 검증 큐의 EXP 항목(g·m)(S15P31A101-207)
- [ ] 세 이미지(Tier 0·HMR·MapAnything)의 GPU 실행, Blackwell(sm_100·sm_120) GPU 에서의 실제 실행, 실험 이미지와 E-GATE-0 결과 대조: 2차 GPU 세션(b·g·m 항목, S15P31A101-207, 3.12·20.6)
- [x] c3(디코딩 처리량) 판정 구조와 기준, Tier 0 기본 디코더(S15P31A101-236, `docs/design/tier0-nvdec-throughput.md`): 2차 GPU 세션(H100)에서 CPU 디코딩이 NVDEC 보다 빨라(카메라별 스레드 합계 1,576 fps 대 215 fps) 기본 디코더를 CPU 로, NVDEC 은 설정 선택으로 두고(5.1), c3 판정을 CPU 카메라별 스레드(그림 p95 ≤ 10 ms·합계 ≥ 100 fps)로 바꿨다. NVDEC 은 참고 측정
- [ ] 다음 GPU 세션에서 확인: 컨테이너가 받는 vCPU·CPU 모델(큐 c0 기록, 3.12 최소 사양 vCPU 8 초기값 확인), 기본 디코더 CPU 로 c3·c4(LIVE 기동·frame_age·pipeline_lag) 통과
- [ ] 외부 파라미터 실행 도구의 여러 명 동시 걷기 대응(카메라 간 사람 대응 단계, 4.3). 2단계 World 정렬 입력 형식은 S15P31A101-102 에서 `simai.world_alignment` 로 정했다
- [ ] Risk Engine 컨테이너가 `risk_plan_result` 1.1.0 의 `no_evaluated_positions` 를 채우기(지금 응답은 1.0.0 이고 빈 `positions` 가 같은 뜻, 8.3)
- [ ] 설계 문서와 명세 차이(판단 필요): T_lag 불확실성에 `timestamp_uncertainty_ms` 를 더할지(6.1 은 더한다고 적고 구현은 시계 차이 불확실성만 쓴다), 학습 내부 파라미터의 직선성 확인을 할 수 없을 때 값을 지우지 않고 `UNVERIFIED` 표시로 넘기는 것(4.3 은 ‘곧지 않으면 비운다’만 적음), 번들 조정이 주점을 초기값에 고정하는 한계(4.3 3단계), 2대 관측 경쟁 후보를 `IDENTITY_UNCERTAIN` 으로 두는 조건을 2대 클러스터·엉덩이 관절로 좁힌 것(5.2 관절 유효성 게이트), 복원한 구역 대상(R7) 이벤트의 근거 있는 해소를 D-G3-09 ‘자동 해소’로 보지 않는 해석(S15P31A101-198, 3.2·6.4), 재시작마다 첫 판정에 잠깐 나는 R7(아직 프레임이 없는 카메라)
- [x] 통합 이미지(`unified`)와 `simai-bootstrap` 감시·장면 준비 모드(S15P31A101-246·-250, 3.12): 한 가상환경(torch 2.11.0+cu130)에 Tier 0·vLLM·HMR·MapAnything 을 담고(Tier 0 코드의 워커·장면 스택 import 금지), 상시 프로세스는 `SIMAI_SERVICES=tier0,vllm,worker` 로 감시하며, MapAnything 은 일회성 `simai-bootstrap scene`(LIVE 표시 파일·GPU 사용 중 > 1024 MiB 이면 종료 코드 75 거부, `SIMAI_SERVICES` 에 `scene` 혼합 시 64, 실행 중 Tier 0 기동 시 장면 그룹 선점 종료 75, Tier 0 시작 잠금·장면 그룹 회수 최악 약 40초·미회수 시에도 경고 및 종료 75 기록 후 Tier 0 우선 시작)으로만 돌린다
- [ ] 다음 GPU 세션(3차)에서 확인할 통합 이미지·VLM 서빙·MapAnything 동등성(S15P31A101-235·-246·-250, 3.12·5.4): 통합 이미지 GPU 점검(u1)·엔진 빌드(b, torch 2.11·CPU onnxruntime)·HMR 보정 K 경로(u2), MapAnything 장면 준비 모드 실행(u3·u4)과 torch 2.7.1 `mapanything` 이미지(m1·m2) 대비 `compare_envs.py` 동등성 통과, Cosmos3-Nano 가 `vae`·`sound_tokenizer` 없이(약 31.5GB) 뜨는지와 적재 VRAM·KV 캐시(v1), VLM 빠른 경로 지연(v2), 감시 모드에서 Tier 0 + VLM 동시 실행 때 Tier 0 지연·CPU 경합·프로세스 그룹 정리(v3)
- [ ] 측정 뒤 정할 값: Tier 0 + 분석 워커 GPU 메모리 예산과 48GB 최소 사양(3.2 예산표의 VLM 0.55 는 48GB 기준 초기값이라 근거가 약하고 48GB 는 Tier 0 + 분석 동시 실행에 모자랄 수 있음. 시연 기준 GPU 는 80GB(H100)로 두었고, 3차 GPU 세션 v1 에서 Reasoner VRAM 실측 뒤 3.2·3.12 값을 고친다), Tier 0 융합 시각을 고를 때 최신 촬영 시각이 `t_latest_global` 보다 150 ms 넘게 뒤처진 카메라를 그 tick 에서 빼는 기준(빼고 남는 카메라가 2대 미만이면 빼지 않음, `docs/design/tier0-fusion-time.md`, S15P31A101-9 RTSP 측정 뒤 본문 반영), R1 보정 여유 0.3 m(7.1, G1·G2 실측 뒤)
- [ ] 전신 임베딩 기반 재식별(Re-ID) 보조 비용 도입 여부(5.2, 11.5, 16장, AGENTS 규칙 8): 10/30 이후 G2 ID 유지율 측정 뒤 결정한다(지금 범위 아님). 도입하더라도 기하 게이트 뒤 보조 비용으로만 쓰고 Tier 0 위치·ID 정본은 덮지 않으며, 얼굴 재식별은 하지 않는다
- [ ] G2 ID 정답 주석 분량, G7 사람 평가의 평가자 수·채점 기준(15.1)
- [ ] 워치 지급 시점과 모델(확장, 부록 C)

## 19.2 로드맵

**준비 작업(진행 현황은 Jira):** 개발 교실 게이트용 실측 지점 정하기, 시뮬레이터(가상 교실·사람·C110 사양 가상 카메라), Risk Engine과 단위 테스트, 공개 다시점 데이터셋(WILDTRACK, CMU Panoptic)으로 사전 검증, 백엔드·뷰어·PWA 골격, RAG 문서 준비, 동의서·촬영 목록.

**2026-10-16 GPU 확보 이후 선행 실험:** EXP-01 정적 장면과 EXP-02 HMR을 각각 하나의 대형 Feature 티켓으로 병렬 준비한다. 중량 GPU Run은 큐에서 직렬로 실행하고, 결과 비교와 독립 재현 검증을 거쳐 INT-01 최소 공간+메시 통합 데모를 만든다. M1→M3 및 G0→G8 제품 게이트 순서는 그대로 유지한다.

**MVP 순서:**

1. M1: G0 기반 → G1 자동 보정 → G2 위치·추적 → G3 실패 대응, G6-R1·R6·R7, 반장 PWA 알림
2. M2: G5 3D 골격·뷰어, G6-R2·R3·R5, G8 PWA(조치 상태 포함)
3. M3: G4 정적 3D → G7 VLM·보고서, 최소 리플레이, 부록 D 전체 회귀

기능 통합 범위는 1.3의 M1 → M2 → M3 표를 따른다.

**확장 순서:** 부록 C 를 따른다.

**3D 고도화 트랙(2026-10-16 GPU 확보로 선행 착수):** EXP-01 정적 배경(MapAnything Native), EXP-02 인체 메시(Fast SAM 3D Body/MHR), INT-01 동일 월드 통합을 큰 Experiment 단위로 검증한다. PPE 워커·워치·정적 공간 온라인 갱신·Unreal 배경 빌드는 확장(부록 C)이다. EXP-01은 M3 정적 3D(G4)의 선행 검증이며(E-GATE-0\~4 채택 절차), EXP-02는 시연 범위(1단계 MHR 리깅 메시 + 포즈 스트림, 2단계 관측 픽셀 UV 텍스처 누적, 5.6·20.3)로 확정해 E-GATE-0\~3 으로 품질·성능·Tier 0 독립과 캡슐 폴백을 검증한다(20.6).
