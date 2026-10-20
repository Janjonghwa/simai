# 부록 A. 용어집

| 용어 | 정의 |
|---|---|
| World | 첫 CALIBRATED 때 교실 바닥·벽으로 정한 단일 미터 좌표계(오른손, Z-up, 바닥 Z=0) |
| World ID | 카메라 간에 연결된 한 사람·객체의 세션 단위 익명 번호 |
| Tier 0 | 실시간 안전 경로. 관측에서 LIVE 위험 판정까지, RunPod Tier 0 서비스에서 실행(v1.2), 위치의 정본(5장). Tier 1은 비동기 3D 인체 강화(SAM 3D Body 등), 안전 판단 권한 없음 |
| 분석 워커 | Tier 0 와 같은 GPU Pod 안의 별도 프로세스. EC2 에서 작업을 가져가 VLM·정적 3D·마스크·보정 번들 조정·HMR 을 처리. 안전 판단 권한 없음(3.1, 12.5) |
| 게이트웨이 | 교실망에서 카메라 영상을 받아 추정 촬영 시각(RECEIVE_ESTIMATED)을 붙여 RunPod로 중계하고 현장 경보를 울리는 노트북 |
| DeploymentProfile | 같은 코드의 배치 구성. “클라우드”(Tier 0 를 RunPod 에서 실행, 기본)와 “현장”(노트북·현장 GPU 에서 실행) |
| RECEIVE_ESTIMATED | 촬영 시각을 노트북 수신 시각과 카메라 PTS 로 추정한 값이라는 표시(3.9). C110 의 기본값. 그 밖의 값은 CAMERA_STREAM / FILE / SIM |
| 삼각측량 | 2대 이상 카메라의 같은 관절 2D 위치로 3D 위치를 구하는 계산 |
| 재투영 오차 | 3D 점을 카메라 화면에 다시 투영했을 때 실제 검출 위치와의 픽셀 차이 |
| 번들 조정 | 카메라 자세와 3D 점을 함께 최적화해 재투영 오차를 줄이는 방법 |
| 게이지 고정 | 번들 조정 결과가 매번 통째로 이동·회전·축척되지 않도록 좌표계를 제약으로 묶는 것 |
| 실측 정물 | 치수를 미리 잰 단단한 시연용 안전 장비(안전봉·라바콘 등). 1순위 스케일 근거(4.3) |
| observation_gap | 전체 카메라 최신 촬영 시각 − 객체의 마지막 유효 관측 시각 |
| pipeline_lag | 판정 시각 − 전체 카메라 최신 촬영 시각(노트북–Pod 시계 차이 `laptop_clock_offset` 으로 Tier 0 시계에 옮긴 값, 6.1). 시계 차이나 최신 촬영 시각을 모르면 `health.pipeline_lag_ms` 는 null 이고 처리 정체 경과로 아는 하한은 `pipeline_lag_lower_bound_ms` 로 따로 싣는다 |
| staleness | gap + lag. 객체 정보의 총 경과 시간 |
| OBSERVED / PREDICTED / UNKNOWN | 객체 관측 상태(6.1) |
| NOMINAL / DEGRADED / UNSAFE_UNKNOWN | System Health(6.2) |
| VALID / CHANGING / UNKNOWN | Region Geometry 상태 |
| CANDIDATE / VALIDATING / COMMITTED / REJECTED | Scene 버전 생명주기 |
| UNCALIBRATED / PROVISIONAL / CALIBRATED / DRIFTED | 보정 상태(4.4) |
| RAISED / UPDATED / RESOLVED | RiskEvent 생명주기 |
| VERIFIED / UNVERIFIABLE | 해소 여부를 관측으로 확인할 수 있는지 |
| ACKNOWLEDGED | 운영자(관리자·반장·현장 확인 버튼)가 경보를 확인했다는 속성(해소 아님) |
| severity / confidence | 위험 정도 / 증거 신뢰도. 서로 독립 |
| “확인 필요” | 신뢰도가 낮은 경보에 붙는 표시. `confidence_reasons[]`가 비어 있지 않은 상태. 등급은 유지(7.1) |
| confidence_reasons | “확인 필요”의 이유 목록: Z_UNKNOWN, PREDICTED, PROVISIONAL_CALIB, SINGLE_VIEW, JOINT_UNCERTAIN, IDENTITY_UNCERTAIN(7.1) |
| 발생 지속 조건 | 이벤트 발생 전 조건이 짧게 이어져야 하는 규칙. 초기값 10Hz 3프레임 중 2프레임(6.4) |
| 이벤트 재개 | 같은 사람·같은 구역이 해소 후 10초 안에 다시 걸리면 새 이벤트 대신 기존 이벤트를 다시 활성으로 돌리는 것(6.4) |
| LIVE / PLANNED / REPLAY / WHAT_IF | Risk Engine 입력 출처. LIVE만 실제 알림 가능 |
| 시험 알림 | 노트북 로컬 시험음과 시험 푸시(`POST /api/push/test`). RiskEvent 가 아니며 “시험”을 표시(3.10) |
| local_alert | Tier 0 가 CRITICAL·HIGH 판정 직후 노트북으로 바로 보내는 현장 경보 메시지(I0) |
| 노트북 로컬 경보 | MVP 현장 경보의 1차 채널. Tier 0 의 `local_alert`로 노트북 스피커·화면이 울림(3.10) |
| 관제 화면 경보 | MVP 현장 경보의 2차 채널. EC2 를 거쳐 관제 화면에서 소리·점멸(3.10) |
| 현장 경보 재동기화 | Tier 0 가 (재)접속 직후와 1Hz health 마다 활성 현장 경보 목록을 보내 노트북 소리·표시를 맞추는 것(3.10) |
| 엔드포인트 등록 | Tier 0 가 기동 시 공개 IP·포트를 EC2 에 등록하고, 노트북이 EC2 에서 받아 접속하는 절차(3.12) |
| 공개키 고정 | 노트북이 Tier 0 TLS 공개키 지문만 신뢰하는 서버 확인 방식(12.7) |
| glass-to-alarm | 카메라가 보는 실제 사건(LED)부터 노트북 소리·화면까지를 한 화면 촬영으로 재는 종단 지연(15.1) |
| 작업면 | 관리자가 OBB 와 상판 높이로 등록한 단상·책상 등. R2·R3 가장자리와 ContactEstimator 의 지지 표면 근거(7.5) |
| VLM | 비전-언어 모델. NVIDIA Cosmos3-Nano(16B) Reasoner 모드(5.4) |
| LLM | 텍스트 생성 모델. 외부 API “GPT 6.1 sol”, 텍스트만 보냄(5.4) |
| RAG | 안전 수칙 문서를 검색해 인용하게 하는 방식 |
| TBM | 작업 전 안전점검회의 |
| MV3DT 방식 | 카메라별 추적을 공유 바닥 평면에서 전역 ID로 융합하는 방식 |
| TrackTrack | 카메라별 Local Tracker 기본값(CVPR 2025, Ultralytics `tracktrack.yaml`)(5.2) |
| E2E(NMS-free) | YOLO26 의 NMS 후처리 없는 출력 경로. Tier 0 기본(5.1) |
| HMR | 영상에서 사람의 3D 메시·자세를 복원하는 모델 |
| MHR | SAM 3D Body의 파라메트릭 바디 표현 |
| 캡슐 아바타 | MVP 사람 표현. Tier 0 골격의 관절 사이를 캡슐/막대로 잇는 InstancedMesh(13.1) |
| 3DGS | 3D Gaussian Splatting. 시각용 정적 장면 표현(확장 후보) |
| Scene | 정적 3D 장면의 한 버전. 자산 주소, 생성에 쓴 보정 버전, 상태(CANDIDATE\~REJECTED)로 구성 |
| Operational Geometry | 안전 판단에 쓰는 검증된 형상(보정으로 확정한 바닥 평면, 등록 정적 객체·작업면의 보수적 OBB, 위험 구역). 보기용 형상(glTF, 3DGS)과 구분 |
| 링 버퍼 | RunPod Tier 0 서비스가 카메라별 최근 영상을 메모리에 잠시 보관하는 순환 저장 공간. 이벤트 클립의 출처 |
| outbox | 이벤트 저널에서 아직 EC2 ACK를 받지 않은 구간(acked_offset 이후). 별도 저장소가 아님 |
| 구성 묶음(config bundle) | RunPod Tier 0 판정 기준(구역·규칙·보정·형상)의 버전 묶음 |
| CURRENT / PENDING / UNVERIFIED / STALE | Config Sync 상태. UNVERIFIED는 연결 단절로 최신 여부를 모르는 상태, STALE은 불일치가 확인된 상태. 어느 쪽이든 판정은 계속 |
| DELAYED_ACTIVE | 재동기화 후 최종 상태가 여전히 활성으로 확인된 이벤트의 지연 알림 표시 |
| observed / inferred | 관절 출처(`joint_source`). observed는 Tier 0 삼각측량, inferred는 Tier 1 HMR 보강. 위험 판단은 observed만 |
| joint_source / surface_source | 관절 출처(observed/inferred) / 메시 표면 출처(관측/보간, `human_mesh_output` 의 `surface_source`). 추정을 관측으로 표시하지 않기 위한 필드(20.4) |
| 지지 표면 출처(`world_state` 의 `surface_source`) | I1 `world_state` 트랙의 접지점 출처. observed 는 이번 시각 Tier 0 가 관측한 VALID 발목 근거, inferred 는 가려진 동안 마지막 유효 지지 표면을 유지한 값(5.2). 접지점이 있을 때만 싣는다. 메시 표면 출처와 이름만 같고 다른 필드이며 값도 다르다(observed/inferred 와 관측/보간) |
| input_provenance | 산출물이 소비한 Provider 출력의 출처(`provider_source`)와 버전 기록(20.4) |
| DATASET_GT / SIM / MODEL_ESTIMATE | Provider 출처 값. 공개 데이터 정답 / 시뮬레이터 / 이전 Run 의 모델 추정. 이 입력으로 얻은 지표는 상한 성능이며 제품 성능으로 쓰지 않음(20.4) |
| z_validity | 사람 높이(Z)의 유효성. 지지 표면을 모르면 UNAVAILABLE이며 Z=0을 가정하지 않음 |
| ContactEstimator | 발 keypoint·발목 정보·등록 작업면으로 발의 접지 여부와 지지 표면을 추정하는 모듈. 근거가 부족하면 support_surface = UNKNOWN |
| TENTATIVE / CONFIRMED / OCCLUDED / LOST | World Track의 관리 생명주기. 관측 상태와 별도 |
| IDENTITY_UNCERTAIN | 카메라 간 매칭이 애매해 같은 사람인지 확신할 수 없다는 표시. 합치지 않고 이 표시를 붙인다 |
| stream_epoch | 카메라 스트림 재연결마다 증가하는 번호. camera_id·frame_sequence와 함께 프레임을 식별(12.7) |
| pelvis_xyz / position_xy / ground_anchor_xyz | 삼각측량 골반 위치 / 구역 판정용 수평 위치 / 검증된 지지 표면 위 위치(모르면 비움). 정의는 5.2 |
| head_z / head_z_validity | 머리 꼭대기 높이 추정. 귀·눈 중점 높이 + 고정 오프셋(5.2). 관절 유효성 게이트가 VALID·UNCERTAIN 으로 낸 값을 싣고(UNAVAILABLE 이면 비움), 게이트 결과(VALID/UNCERTAIN/UNAVAILABLE)는 `head_z_validity` 로 싣는다. 값이 있어도 유효성을 확인하고 UNCERTAIN 값은 확정 근거로 쓰지 않는다 |
| tier0_warnings | `health` 에 싣는 Tier 0 운영 경고: CHECKPOINT_STALLED·CHECKPOINT_ERROR·JOURNAL_STALLED·JOURNAL_FAILED·OUTBOX_BLOCKED·SNAPSHOT_BROKEN·LIVE_MARKER_WRITE_FAILED(GPU 상태 표시 파일 쓰기 실패, 정보 경고) 과 처음 확인한 시각. 빈 배열은 경고 없음, 필드 없음은 모름(6.5) |
| sync_valid | `health` 카메라별 시간 동기화 근거. true 는 동기화 측정으로 확인, false 는 무효 판정(융합 제외), null 은 근거 없음(6.1) |
| aliases | 병합(ID 교정)으로 이 World ID 에 합쳐진 이전 World ID 목록. 뒤에만 붙고 Track 이 살아 있는 동안 빠지지 않는다(5.2) |
| split_from | 분할(ID 교정)로 갈라져 나간 새 World ID 가 싣는 원래 World ID. 원래 ID 에는 없고, 분할을 확정한 시각부터 그 ID 가 살아 있는 동안 같은 값이다(5.2, `world_state` 2.1.0) |
| 해소 기록(tombstone) | 재시작 복구용 체크포인트에 남기는 해소된 이벤트의 마지막 sequence·판정 시각. 저널을 다시 적용할 때 해소된 이벤트를 되살리지 않게 한다(3.2) |
| queue_wait_ms / frame_age_ms | 추론 대기열에서 기다린 시간 / 촬영부터 추론 시점까지 전체 경과 시간 |
| 원격 관제 장애 | EC2만 끊긴 상태의 노트북 표시. 판정과 현장 경보는 정상이고 PWA·관제 화면 전달만 지연 |
| GROUND_FOOTPRINT / VOLUME_3D | 구역 판정 유형. 바닥 다각형만 보는 구역 / 높이 범위까지 보는 입체 구역(8.2) |
| 이벤트 저널 | 모든 이벤트 전이를 추가만 하는 내구성 로그. 재시작 복구의 정본이며 min(acked_offset, last_applied_journal_offset) 이전만 정리. outbox는 acked_offset 이후 구간 |
| journal_offset / acked_offset | 저널 전체의 전역 단조 증가 위치 / EC2가 받았다고 확인한 마지막 저널 위치. 이벤트별 event_sequence와 다름 |
| site_ack | 노트북 현장 확인 버튼의 확인. EC2 단절 중에도 소리를 멈추고 저널에 기록 |
| BEV 평면도 | 카메라·정적 3D 를 위에서 본 화면으로 자동 생성한 간략한 2D 개요. 좌표·스케일 기준이 아니며 안전 판단에 쓰지 않음. 비상 화면과 같은 컴포넌트 |
| EXP-01 / EXP-02 / INT-01 | 정적 배경 복원 실험 / 인체 메시·외형 실험 / 두 결과를 같은 World 에 합치는 통합 검증(20장) |
| Reviewer | 구현자와 독립된 사람 또는 리뷰 에이전트. 공통 체크리스트로 실험을 검증(20.1) |
| Run 기록 | 실험 Run 마다 남기는 재현 기록(`run_record`: 커밋·환경·가중치·데이터·설정·지표)(20.5) |
| 확장 기능 용어 | Visual Hull 후보·Temporary Collider·UNKNOWN_OCCUPANCY, BOUND/UNBOUND/LOST, 움직임 대조, WORN/NOT_WORN/UNKNOWN 은 부록 C 를 본다 |
