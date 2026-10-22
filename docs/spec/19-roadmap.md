# 19. 미결 사항과 로드맵

## 19.1 미결 사항

- [ ] 싸피 Wi-Fi에서 카메라 접속과 기기 간 RTSP 수신이 가능한가, 공유기를 확보할 수 있는가
- [ ] Tapo C110 RTSP 스트림의 촬영 시각: RTCP 시각이 불안정함을 확인(1차 측정). 긴 구간 회귀 보정 가능 여부는 G0 LED 시험으로 판단
- [ ] 공유기 환경에서 카메라 1대 10분 RTSP 측정(S15P31A101-9 완료 조건)
- [ ] 시연 GPU 에서 4대 동시 처리 FPS(G0)와 노트북 → GPU 사이트 업로드 안정성
- [ ] RAG 임베딩 BAAI/bge-m3 의 가중치 revision 고정(운영 색인 생성 전), 실행 위치(CPU·GPU), 조항 청크·배치 크기
- [ ] 외부 LLM “GPT 6.1 sol” 의 제공사(11.5 국외 이전 고지의 받는 자 명칭·연락처, 참여자 동의 전)와 호출 설정(출력 길이·응답 시간을 I5 작업 대여 시간 제한 안으로, 12.5)
- [ ] 다중 공사·기업 계정(부록 C.9)의 역할 계층·12.6 경로, JWT 현장 클레임·`site_id` 확장 대비 채택 여부(MVP 이후, 팀 회의 뒤)
- [ ] 해상도·화각이 바뀐 뒤 재보정 절차(내부 파라미터 재추정 → 번들 조정)와, 틀어짐 귀속이 불확실할 때 운영자가 할 일(4.4, 18장)
- [ ] 지급 EC2 의 네트워크 전송 한도(2D 관측은 Tier 0 저장 위치에 두므로 부담이 줄었다), EC2 와 Lightsail 중 어느 것인지. 디스크는 320 GB 로 확인했다(11.4, 11.5)
- [ ] 시연 GPU 사이트·GPU 등급(엘리스 클라우드 GPU VM 등이 후보, 3.12): 최소 사양, 인스턴스를 다시 켜도 남는 디스크, I0 Direct TCP 용 공개 포트, 드라이버·Container Toolkit, 노트북에서 잰 왕복·업로드, 국외 이전 여부. 리허설 전(D-14) GPU 확보와 카메라 4대 기준 `capture_to_site_alert` 실측
- [ ] 시연 저장 위치(Network Volume 대신 시연 환경 디스크)에서 G3 강제 종료 복구 실측(S15P31A101-92, 15.1 G3·부록 D.4): 저널 fsync 지연, 원자적 rename·파일 잠금, 강제 종료 직후 잠금 해제 지연
- [ ] Network Volume 폐기 뒤 녹화 세션·내부 테스트 영상의 보관 위치(15.3, 11.5)와 그 주소 형식: `recording_manifest.storage.uri` 는 스키마 패턴으로 `volume://<datacenter>/…`·`s3://` 만 받아 데이터센터가 없는 블록 스토리지·컨테이너 디스크·노트북 보관을 적을 수 없고(패턴 변경 필요 여부), 공통 `uri` 는 패턴 제한 없이 설명에서 두 방식만 주소 의미로 정의한다(의미 정의 보강 여부)
- [ ] World 원점·축 자동 선택 규칙(어느 모서리·벽을 쓸지) 확정
- [ ] 참여자 동의서와 학교 측 촬영 허가, 국외 이전 고지 문구(개인정보 보호법 제28조의8 원문 확인, LLM API 제공사 명칭·연락처)
- [ ] 지역이 고정되지 않은 개발·시험 GPU 사이트(RunPod 남는 지역·Vast.ai)로 사람이 찍힌 영상(내부 테스트 영상 포함)을 보낼 수 있는 범위와 국외 이전 고지(11.5)
- [ ] R3 높이 기준의 정확한 근거 조항
- [x] TBM 계획 작업 위치 자동 생성 규칙과 Risk Engine 컨테이너의 FastAPI 구현(S15P31A101-99, 8.3·12.2): 작업면 상판 가운데(넓은 면은 가장자리 0.5 m 안쪽 하나 더)와 유효한 개구부 경계 바깥 0.2 m, 머리 높이 없음(8.3). 컨테이너는 `deploy/risk-engine`(CPU, compose 내부망 `risk-engine:8080`, 포트 공개 없음). 같은 커밋 두 이미지의 D-G3-04 대조는 다음 Tier 0 이미지 빌드 때 `deploy/scripts/risk_engine_g304.sh`
- [x] Tier 0 저장 위치 구조(`$SIMAI_ROOT`, 3.12): 시작 절차가 지정값 → 볼륨(`/workspace/simai`) → 컨테이너 디스크(`/srv/simai`) 순으로 고른다. 그 아래 `weights`(가중치), `datasets`, `recordings`, `obs2d`, `engines`(TensorRT 엔진 캐시), `state`(Tier 0 저널·체크포인트·구성 묶음 보관본·보정 기준·실행 중 표시 파일, 단일 작성자·손으로 지우지 않음), `queue`(GPU 큐 상태·Run 폴더), `runs`(실험·검증 산출물), `setup`, `logs`가 있다. 비공개 폴더 `.simai-private` 는 `$SIMAI_ROOT` 안이 아니라 옆(`$SIMAI_ROOT/../.simai-private/`)이며 TLS 키·토큰 파일 환경 변수가 그 경로를 가리킨다. E-GATE-0 때의 `envs`·`third_party`·`tools` 는 새로 만들지 않는다. `obs2d` 는 2D 관측 Parquet(11.2)이다: Tier 0 에 `SIMAI_OBS2D_ROOT=$SIMAI_ROOT/obs2d` 를 준다(코드 기본값 없음, 없으면 오류). 파일은 `{date}/{hour}/{camera_id}/obs2d.{camera_id}.{YYYYMMDDTHHMM}Z.{session_id}.{part:04d}.parquet`(UTC, 한 시간 단위)이고 기본 14일(7\~14일) 뒤 지운다. `recordings` 는 녹화 세션 디렉터리 `recordings/<session_id>/`(세션 목록·해시는 `recording_manifest`, 15.3. 보관 위치는 위 미결)다
- [ ] SSAFY GitLab CI 러너 사용 가능 여부(없으면 로컬 검사 스크립트로 대체)
- [ ] 아직 계약에 없는 v1.3 규칙: 노트북 송신 큐 버림·연결 없음 버림을 `laptop_heartbeat` 에 싣는 필드(Tier 0 수신 집계는 `health` 에 반영). 계약에 반영한 것: `confidence_reasons`(7.1), 이벤트 재개 `resumed`·`resume_count`(6.4), 확인 대상 `event_sequence`(3.10), `world_state.config_version`(3.2), 판정이 아닌 전이의 `evidence_ref` null(3.2), 노트북 송신 큐 버림 `health.cameras[].frames.laptop_skipped`·`unattributed_gap`(12.7), 이벤트 클립 `media_clip`·`media_clip_retention`(11.3), 현장 경보 목록 `health.active_site_alerts`(3.10), 시계 차이 `clock_sync_request`·`clock_sync_response`·`health.laptop_clock_offset`(3.9), 엔드포인트 등록 `tier0_endpoint`(3.12), 2D 관측 요약 `observation_summary`(11.2), I5 작업 대여 `job_lease_request`·`job_request`·`job_heartbeat`·`job_callback`·`job_lease_status`(12.5), 녹화 manifest `recording_manifest`(15.3), 보정 출처 값 정리 `calibration_source`(20.4), `world_state` 트랙의 지지 표면 출처 `surface_source`·병합 별칭 `aliases`와 `health.ec2_world_state_drops`(Tier 0 → EC2 world_state 송신 버림 누적)(5.2, S15P31A101-170), `world_state` 트랙의 `head_z_validity`(5.2)와 `event_state_snapshot.journal_offset` null(아직 저널 기록 없음, 3.2)(S15P31A101-172), `world_state` 2.0.0 UNCERTAIN `head_z` 값 싣기(5.2)와 `health` 2.0.0 `pipeline_lag_ms` null·`pipeline_lag_lower_bound_ms`(6.1)·`tier0_warnings`(6.5)·`cameras[].sync_valid`(6.1)(S15P31A101-179), I5 반납 `job_callback.error.consume_attempt`·`job_request` 3.0.0 시도 번호(12.5)·`health` 2.1.0 `tier0_warnings` `LIVE_MARKER_WRITE_FAILED`(6.5)·`run_record.experiment_id` `VAL-01`(20.5)(S15P31A101-197), 운영자 종료 `event_close`·`event_close_result` 와 `risk_event_transition`·`risk_event` 2.0.0 운영자 종료 해소 `resolution`(6.4)(S15P31A101-200), `world_state` 2.1.0 분할 출처 `split_from`(5.2)(S15P31A101-201), 운영자 상향 `event_escalate`·`event_escalate_result`·전이 `escalation`(5.4), I5 `bev_floorplan`·`incident_summary`·`tbm_report` `report_kind`(12.5), I2 `/topic/world`(`world_state` 그대로)·`/topic/health`(`viewer_health`)·`/topic/admin/events`(`action_mismatch`)(12.4)(S15P31A101-206), `risk_plan_result` 1.1.0 평가한 위치 없음 `no_evaluated_positions`(8.3)(S15P31A101-217)
- [ ] 카메라 간 시간 동기화 측정과 무효 판정(지금은 측정이 없어 `health.cameras[].sync_valid` 를 null 로 보고하고, Tier 0 내부 융합은 모든 카메라를 유효로 본다, 6.1·6.5)
- [ ] World ID 수동 교정(운영자 분할·병합 명령. 설계 문서에 계약안 `identity_correction`·`identity_correction_result` 만 있고 구현하지 않음, 5.2. 서버·화면 작업과 맞춰 별도 티켓), 노트북 “AI 분석 지연” 표시에 쓸 신호(지금 노트북에 오는 신호가 없어 표시하지 않는다, 3.10)
- [x] Tier 0 공개 주소 환경 변수(S15P31A101-208, 3.12): `SIMAI_PUBLIC_HOST`·`SIMAI_PUBLIC_PORT_<번호>` 먼저, 없으면 `RUNPOD_PUBLIC_IP`·`RUNPOD_TCP_PORT_<번호>`. Vast 이름은 문서로 확인하지 못해 읽지 않는다. Run 기록 `gpu.provider` 에 VAST·OTHER 추가(run_record 2.2.0, 20.5)
- [x] 실험 이미지(HMR `exp02-fs3db`·MapAnything `mapanything`, torch 2.7.1+cu128, 3.12)와 가중치 묶음(`exp02`·`static`), GPU 세션 검증 큐의 EXP 항목(g·m)(S15P31A101-207)
- [ ] 세 이미지(Tier 0·HMR·MapAnything)의 GPU 실행, Blackwell(sm_100·sm_120) GPU 에서의 실제 실행, 실험 이미지와 E-GATE-0 결과 대조: 2차 GPU 세션(b·g·m 항목, S15P31A101-207, 3.12·20.6)
- [ ] c3(NVDEC 디코딩 처리량) 판정 구조와 기준(2차 GPU 세션 재측정 뒤): 지금 기준(한 스레드 번갈아 p95 ≤ 10 ms·합계 ≥ 100 fps)은 설계 문서 값이고 명세에는 디코딩 구간 예산이 없다. LIVE 구조(카메라별 스레드) 기준으로 바꿀지는 재측정 뒤 정한다(S15P31A101-212, `docs/design/tier0-nvdec-throughput.md`)
- [ ] 외부 파라미터 실행 도구의 여러 명 동시 걷기 대응(카메라 간 사람 대응 단계, 4.3)과 2단계 World 정렬 입력 형식(S15P31A101-102)
- [ ] Risk Engine 컨테이너가 `risk_plan_result` 1.1.0 의 `no_evaluated_positions` 를 채우기(지금 응답은 1.0.0 이고 빈 `positions` 가 같은 뜻, 8.3)
- [ ] 설계 문서와 명세 차이(판단 필요): T_lag 불확실성에 `timestamp_uncertainty_ms` 를 더할지(6.1 은 더한다고 적고 구현은 시계 차이 불확실성만 쓴다), 학습 내부 파라미터의 직선성 확인을 할 수 없을 때 값을 지우지 않고 `UNVERIFIED` 표시로 넘기는 것(4.3 은 ‘곧지 않으면 비운다’만 적음), 번들 조정이 주점을 초기값에 고정하는 한계(4.3 3단계), 2대 관측 경쟁 후보를 `IDENTITY_UNCERTAIN` 으로 두는 조건을 2대 클러스터·엉덩이 관절로 좁힌 것(5.2 관절 유효성 게이트), 복원한 구역 대상(R7) 이벤트의 근거 있는 해소를 D-G3-09 ‘자동 해소’로 보지 않는 해석(S15P31A101-198, 3.2·6.4), 재시작마다 첫 판정에 잠깐 나는 R7(아직 프레임이 없는 카메라)
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

**3D 고도화 트랙(2026-10-16 GPU 확보로 선행 착수):** EXP-01 정적 배경(MapAnything Native), EXP-02 인체 메시(Fast SAM 3D Body/MHR), INT-01 동일 월드 통합을 큰 Experiment 단위로 먼저 검증한다. PPE 워커·워치·정적 공간 온라인 갱신·Unreal 배경 빌드는 확장(부록 C)이다. 해당 Experiment가 성공했다고 곧바로 MVP 요구사항으로 확정하지 않는다. EXP-01은 M3 정적 3D(G4)의 선행 검증이며 별도 작업이 아니다.
