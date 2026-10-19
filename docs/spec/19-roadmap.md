# 19. 미결 사항과 로드맵

## 19.1 미결 사항

- [ ] 싸피 Wi-Fi에서 카메라 접속과 기기 간 RTSP 수신이 가능한가, 공유기를 확보할 수 있는가
- [ ] Tapo C110 RTSP 스트림의 촬영 시각: RTCP 시각이 불안정함을 확인(1차 측정). 긴 구간 회귀 보정 가능 여부는 G0 LED 시험으로 판단
- [ ] 공유기 환경에서 카메라 1대 10분 RTSP 측정(S15P31A101-9 완료 조건)
- [ ] H100 에서 4대 동시 처리 FPS(G0)와 노트북 → RunPod 업로드 안정성
- [x] VLM 확정: NVIDIA Cosmos3-Nano(16B) Reasoner 모드, vLLM `vllm-cosmos3`(5.4). G7 은 합격 여부만 판정
- [x] HumanCalib 사용 범위: 코드·3D lifter 를 쓰지 않고 원리만 따른 자체 번들 조정으로 정했다(4.3). 실측 정물·키를 아는 사람은 스케일 제약으로 넣는다
- [ ] 해상도·화각이 바뀐 뒤 재보정 절차(내부 파라미터 재추정 → 번들 조정)와, 틀어짐 귀속이 불확실할 때 운영자가 할 일(4.4, 18장)
- [ ] 지급 EC2 의 디스크 용량과 네트워크 전송 한도(2D 관측은 RunPod 에 두므로 부담이 줄었다), EC2 와 Lightsail 중 어느 것인지
- [x] RunPod 리전 확정: 개발·시연 일본 AP-JP-1 H100(3.12). 남은 일: D-14 GPU 확보 확인과 카메라 4대 기준 `capture_to_site_alert` 실측
- [ ] 일본 H100 실측(왕복 43ms·업로드 39 Mbps)을 어느 망에서 했는지
- [ ] Network Volume 을 붙인 Pod 를 Stop 할 수 있는지
- [ ] World 원점·축 자동 선택 규칙(어느 모서리·벽을 쓸지) 확정
- [ ] 참여자 동의서와 학교 측 촬영 허가, 국외 이전 고지 문구(개인정보 보호법 제28조의8 원문 확인, LLM API 제공사 명칭·연락처)
- [ ] R3 높이 기준의 정확한 근거 조항
- [ ] 노트북 RTX 4070에서 4대 동시 Tier 0 처리 FPS·지연(현장 프로파일 대체 경로 채택 여부)
- [x] Network Volume 마운트 경로 `/workspace`(AP-JP-1, 볼륨 구조는 `/workspace/simai/{envs,third_party,weights,datasets,runs,logs,setup,tools,obs2d,recordings}`). `obs2d` 는 2D 관측 Parquet(11.2)이다: Tier 0 에 `SIMAI_OBS2D_ROOT=/workspace/simai/obs2d` 를 준다(코드 기본값 없음, 없으면 오류). 파일은 `{date}/{hour}/{camera_id}/obs2d.{camera_id}.{YYYYMMDDTHHMM}Z.{session_id}.{part:04d}.parquet`(UTC, 한 시간 단위)이고 기본 14일(7\~14일) 뒤 지운다. `recordings` 는 녹화 세션 디렉터리 `recordings/<session_id>/`(`volume://AP-JP-1/simai/recordings/<session_id>/`, 세션 목록·해시는 `recording_manifest`, 15.3)다
- [ ] SSAFY GitLab CI 러너 사용 가능 여부(없으면 로컬 검사 스크립트로 대체)
- [x] 요구사항 ID 추적 위치: 9장은 요약만 두고 세부 추적은 요구사항 명세서(`docs/requirements.md`) 비고 열에서 한다
- [ ] 아직 계약에 없는 v1.3 규칙: 노트북 송신 큐 버림·연결 없음 버림을 `laptop_heartbeat` 에 싣는 필드(Tier 0 수신 집계는 `health` 에 반영). 계약에 반영한 것: `confidence_reasons`(7.1), 이벤트 재개 `resumed`·`resume_count`(6.4), 확인 대상 `event_sequence`(3.10), `world_state.config_version`(3.2), 판정이 아닌 전이의 `evidence_ref` null(3.2), 노트북 송신 큐 버림 `health.cameras[].frames.laptop_skipped`·`unattributed_gap`(12.7), 이벤트 클립 `media_clip`·`media_clip_retention`(11.3), 현장 경보 목록 `health.active_site_alerts`(3.10), 시계 차이 `clock_sync_request`·`clock_sync_response`·`health.laptop_clock_offset`(3.9), 엔드포인트 등록 `tier0_endpoint`(3.12), 2D 관측 요약 `observation_summary`(11.2), I5 작업 대여 `job_lease_request`·`job_request`·`job_heartbeat`·`job_callback`·`job_lease_status`(12.5), 녹화 manifest `recording_manifest`(15.3), 보정 출처 값 정리 `calibration_source`(20.4), `world_state` 트랙의 지지 표면 출처 `surface_source`·병합 별칭 `aliases`와 `health.ec2_world_state_drops`(Tier 0 → EC2 world_state 송신 버림 누적)(5.2, S15P31A101-170), `world_state` 트랙의 `head_z_validity`(5.2)와 `event_state_snapshot.journal_offset` null(아직 저널 기록 없음, 3.2)(S15P31A101-172), `world_state` 2.0.0 UNCERTAIN `head_z` 값 싣기(5.2)와 `health` 2.0.0 `pipeline_lag_ms` null·`pipeline_lag_lower_bound_ms`(6.1)·`tier0_warnings`(6.5)·`cameras[].sync_valid`(6.1)(S15P31A101-179)
- [ ] 카메라 간 시간 동기화 측정과 무효 판정(지금은 측정이 없어 `health.cameras[].sync_valid` 를 null 로 보고하고, Tier 0 내부 융합은 모든 카메라를 유효로 본다, 6.1·6.5), `world_state` 1.x 문서를 더 받지 않게 되면 R4 의 skeleton 머리 높이 재계산(오프셋 이중 관리)을 없앰(5.2)
- [ ] 재시작 뒤 체크포인트에 저장한 World Track 과 재관측의 매칭 규칙(저장만 됨, 3.2), 잘못 합친 World ID 의 분할·수동 교정(5.2), 노트북 “AI 분석 지연” 표시에 쓸 신호(지금 노트북에 오는 신호가 없어 표시하지 않는다, 3.10)
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
