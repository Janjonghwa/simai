# 20. GPU 확보 이후 대형 Experiment 운영과 채택 절차

이 절은 §5의 모델 계층, §3.2의 단일 Tier 0 정본, §15의 G0\~G8과 독립적인 **연구·실험 운영 계약**이다. 요구사항 명세서의 FR-102\~111, NFR-18\~23과 대응한다. 기존의 작은 PoC 여러 건을 기능을 완결하는 대형 Experiment 두 개로 통합하되, 내부 Run과 체크포인트는 남긴다.

## 20.1 역할과 소유권

이 절은 사람 기준의 역할이다. 각 Owner는 작업 도구(코딩 에이전트 등)를 자유롭게 쓸 수 있지만, 아래 책임과 금지는 도구와 관계없이 Owner 본인에게 있다.

| 역할 | 책임 | 금지/제약 |
|---|---|---|
| Lead | EXP 우선순위·의존성, 월드/메시 계약(`contracts/`) 승인, GPU 사용 순서 중재, 최종 ADOPT/HOLD/REJECT | 검증 전 기능 DONE 처리 금지 |
| Feature Owner — EXP-01 | 정적 3D 환경, 실험 설계, 실행, 분석, glTF/Scene 연계 결과 | 다른 티켓의 파일 임의 변경 금지 |
| Feature Owner — EXP-02 | Fast SAM 3D Body/MHR, World ID 연결, 움직임, 외형 개선 실험 | Tier 0 위치·판정의 정본을 HMR로 치환 금지 |
| Reviewer | 구현자가 아닌 사람 또는 구현 세션과 독립된 리뷰 에이전트. 아래 리뷰 체크리스트로 검증하고 PASS / FAIL / 보완 요청을 남김 | 구현자의 자기평가만으로 REVIEW를 통과시키지 않음. 최종 채택 판정은 Lead |

**리뷰 체크리스트(EXP 공통).**

1. Run 기록(git_commit, 환경 이미지, weights_hash, dataset_hash, config)만으로 다시 실행해 지표가 허용 오차 안에서 같은지
2. 산출물이 계약 스키마를 통과하는지(좌표계·단위·버전·`input_provenance`·`joint_source`·`surface_source`)
3. 불변 규칙 위반이 없는지(Tier 0 값 덮어쓰기, Z=0 가정, 추정을 관측으로 표시, I1 LIVE 계약 변경)
4. 실패 사례(가림, 반사, 저텍스처, 교차, 재등장)가 보고서에 있는지
5. 정답 입력으로 얻은 지표를 제품 성능처럼 쓰지 않았는지(20.4 입력 대체 계약 규칙 3)

Jira는 **EXP-01 / EXP-02**를 큰 Feature 티켓으로 유지하고, 티켓 하나에 Owner 한 명을 둔다. 체크리스트와 반복 실험 Run까지 잘게 나눠 티켓을 늘리지 않는다. 독립 배포, 장기 소유권, 별도 리뷰가 필요한 작업만 새 Feature 티켓으로 승격한다. 세션이나 작업자가 바뀌어도 Issue 상태, 브랜치, Run 기록으로 이어받을 수 있어야 한다.

작업 상태: TODO → IN_PROGRESS → REVIEW → DONE 또는 BLOCKED. 실험 채택 상태: PLAN → BASELINE → ITERATE → BENCHMARK → REVIEW → ADOPT/HOLD/REJECT. 두 상태는 따로 기록한다.

## 20.2 EXP-01 — Static Scene Reconstruction

**연구 질문:** MapAnything Native의 결과(비교군: Depth Anything 3 pose-conditioned 를 외부 모델 인터페이스로 끼운 것, 5.3)를 10×10m, 4대 고정 카메라 관측과 World 좌표(4.1)에 정합해 COMMITTED Scene에 사용할 수 있는 시각 배경(glTF)으로 만들 수 있는가?

1. **Baseline:** 소수의 빈 교실 프레임, K·pose, 가중치·CUDA 환경을 고정하고 depth/point map/camera 출력을 기록.
2. **복원:** 뷰 간 정합→이상점 제거→표면 생성/메시→단순화. MVP 산출물은 점군 → 단순 메시 glTF 이고 텍스처 메시는 확장(부록 C). 동적 사람 영역은 YOLO26-seg 마스크로 제거.
3. **정렬:** §4의 월드 미터/Z-up, World 원점/축/스케일, calibration_version에 정렬. 관측된 형상과 추정된 공간 구분.
4. **검증:** G4 개발 교실 줄자 실측 대비 10cm, 책상 상판 높이 ±5cm·위치 10cm, 공동 관측 점 중앙값 5cm. 실패/가림/반사/저텍스처 사례도 보고.
5. **통합:** glTF와 Scene 버전/해시/검증 결과를 내보내고 Three.js 실제 화면에서 배경을 확인.

**실패 대안:** G4 미달 시 MoGe-3(단안) + 스케일 보정, 그래도 실패하면 관리자가 등록한 단순 형상(바닥 평면 + 보수적 OBB). MapAnything 포인트/깊이가 정상 생성됐다는 사실만으로 glTF가 성공했다고 판정하지 않는다.

**필수 산출물:** 재현 명령, 모델/입력 해시, 카메라 매개변수, 샘플 점군·glTF, 월드 정렬 결과, 성능(p50/p95·VRAM peak), 실패/복구 기록, Reviewer 판정.

## 20.3 EXP-02 — Human Mesh Reconstruction 및 Appearance Update

**연구 질문:** Fast SAM 3D Body의 MHR 메시/포즈를 World ID에 결합해 동적으로 움직이는 메시 아바타를 만들고, 같은 인물의 연속 관측이 늘 때 실제 외형에 가까워지는 시각 표현을 향상시킬 수 있는가?

1. **Baseline:** 단일 인물 샘플에서 MHR 메시·native_pose·canonical_motion을 출력하고 Three.js 메시 렌더링 재현.
2. **다인·동작:** 4대 영상의 World ID 및 촬영 시각과 대응, 교차·가림·재등장/포즈 안정성 검증. Local Track ID를 인물 정체성의 단일 정본으로 사용하지 않음.
3. **좌표 정렬:** §5.2 Tier 0 pelvis_xyz·position_xy·ground_anchor_xyz와 카메라/월드 변환을 계약으로 연결. 지지면 모를 때 Z=0을 가정하지 않음.
4. **외형 개선:** 인물별로 관측된 표면의 텍스처와 관측되지 않은/보간된 영역을 구분해 누적 갱신한다. 외형 개선과 신원 재식별은 분리하고, 얼굴 복원·표정 추정·실명 인식은 범위 밖이다. 두 단계로 나눈다.
   - 1단계(E-GATE-2): MHR 메시를 Tier 0 포즈로 구동하고 단순 색 투영으로 칠한다.
   - 2단계: 관측 픽셀만 쓰는 다시점 UV 누적. 가시성 필터(z-buffer·시선 각도·마스크) → UV 투영(PyTorch3D 또는 nvdiffrast) → 신뢰도 가중 누적 `T_new = (W_old·T_old + w·C_obs) / (W_old + w)` → 이상 관측 거절 → 버전 기록·되돌리기. 사람별 canonical 메시(MHR 스키닝으로 기준 자세에 정렬) 위에 쌓는다. 별도 동의가 확정된 뒤에만 진행한다. MHR↔SMPL 변환은 https://github.com/facebookresearch/MHR/blob/main/tools/mhr_smpl_conversion/README.md 를 참고한다.
   - 보이지 않는 면·얼굴을 생성하는 방식(SiTH·FISHuman)과 옷 형상 복원(ReMu, ECON)은 쓰지 않는다(16장).
   - World ID 가 `IDENTITY_UNCERTAIN` 이거나 `identity_revision` 이 바뀐 직후(병합·분할·교정 뒤 다시 안정될 때까지)에는 텍스처 누적을 멈추고 마지막 확정 외형을 유지한다. 다른 사람의 외형이 같은 아바타에 섞이지 않게 하기 위해서다.
   - 얼굴 영역은 텍스처 누적에서 제외하거나 흐림 처리한 색으로만 채우고, 원본 얼굴 픽셀을 자산에 저장하지 않는다(5.4의 VLM 입력 얼굴 흐림과 같은 원칙). 관측 crop 보관은 얼굴 흐림·보관 기간·동의 규칙(11.5)을 따른다.
5. **정량·정성 검증:** 원본 SAM 3D Body/기본 골격 기준선과 추론 지연, VRAM, 메시 품질, 다인 안정성, 연속 프레임 드리프트, 관측 누적 전후 시각 품질 비교.
6. **Scene 통합:** 같은 월드에 배경과 인물 메시를 배치하고 라이브 경보/관측 상태가 HMR 지연 시에도 정상 표시되는지 확인.

**필수 산출물:** MHR 메시/포즈, 변환 스키마·샘플 JSON, 다인/연속 프레임 데모, 관측 기반 외형 개선 비교, 렌더링/성능 측정, 실패 사례와 개인정보 점검 기록.

**불합격 조건:** 메시만 생성하고 동적 아바타를 못 보여주는 경우, 추정 외형을 실측으로 표현하는 경우, 외형으로 Tier 0 위험 규칙을 변경하는 경우. 관측 기반 외형의 개선 효과가 없으면 HMR 포즈만 부분 채택 또는 HOLD를 검토한다.

## 20.4 INT-01 · 공간과 인체의 인터페이스 계약

**정본 불변:** World XYZ·검증된 관절·RiskEvent와 CALIBRATED 상태는 §5.2와 §3.2가 관리한다. HMR/Scene은 Visual Geometry 자산이다.

| 필드/형식 | 계약 |
|---|---|
| camera_id, timestamp_source, estimated_capture_time | 기존 §3.9의 카메라 식별/촬영 시각 계약과 통일 |
| world_id, local_track_id | 익명 World ID 기준, 카메라 로컬 추적 ID는 출처로 보존 |
| scene_version, calibration_version | 결과를 생성한 정적 장면/보정 버전과 일치 |
| coordinate_frame, unit, transform | Canonical Metric World, m, 오른손 +Z-up. Three.js 변환은 §4.2의 (x,z,-y) 한 곳에서 수행 |
| mesh_geometry, native_pose, canonical_motion | MHR 정점/면 형상과 포즈 데이터, 최소 렌더링 예제 |
| observation_status, joint_source, surface_source | OBSERVED/PREDICTED/UNKNOWN, observed/inferred, 관측/보간 표면 분리 |
| model_version, input_hash, run_id | 재현과 감사 추적 메타데이터 |
| 아바타 구동 | root 위치와 접지는 Tier 0(5.2)가 정하고, HMR은 관절 회전과 체형만 제공한다. 메시의 발은 접지가 확인되면 `ground_anchor_xyz`에 맞춘다. HMR 결과가 Tier 0 최신 상태보다 기준(초기값 500ms) 넘게 늦거나 실패하면 Tier 0 골격 구동 캡슐 아바타로 돌아간다. G5 발-지지면 오차는 Tier 0 지표다(15.1) |

별도 버전의 실험 메시지로 시작하되, I5의 Job 유형/콜백 결과만 변경하고 I1 LIVE World 계약은 Reviewer 검토와 Lead 승인 없이 수정하지 않는다. HMR 업데이트가 늦으면 마지막 유효 시각 자산을 표시하되 staleness를 노출하고, 실패하면 §13의 캡슐 아바타 또는 비상 2D 화면을 유지한다.

**입력 대체 계약.** EXP와 개발 작업은 아직 없는 상류 결과(G1 보정, Tier 0 World 상태)를 기다리지 않도록, 상류 입력을 Provider 인터페이스 뒤에서 받는다. 지금은 대체 구현으로 개발하고, 상류가 준비되면 설정만 바꿔 실제 구현으로 교체한다. 소비하는 코드는 바뀌지 않는다.

| Provider | 제공하는 것 | 필드 의미의 기준 |
|---|---|---|
| FrameSource | 카메라 ID + 촬영 시각(3.9 촬영 시각 계약) + 프레임 | 5.1 입력 추상화 |
| CalibrationProvider | 카메라별 K, 왜곡 모델·계수, R·t(OpenCV 규약, World → 카메라), 영상 크기, 보정 상태(4.4), 스케일 근거, `calibration_version` | 4장 |
| WorldTrackProvider | 촬영 시각별 사람 목록: `world_id`, 카메라별 `local_track_id`·bbox·2D keypoint, `pelvis_xyz`, `position_xy`, `ground_anchor_xyz`, `support_surface_validity`, `head_z`, `observation_state`, `identity_confidence`, `calibration_version` | 5.2. I1 `world_state`의 트랙 필드에 `observation_2d`의 카메라별 연결을 더한 것 |

| Provider | 지금(대체) | 상류 준비 후(실제) |
|---|---|---|
| FrameSource | 공개 다시점 데이터, 시나리오 녹화 파일, 시뮬레이터 | 같음(녹화·라이브) |
| CalibrationProvider | `DATASET_GT`(공개 데이터의 정답 카메라), `SIM`, `MODEL_ESTIMATE`(이전 Run 에서 모델이 추정한 카메라를 다시 입력. 예: EXP-01 known 모드 시험) | `G1_AUTO`(G1 자동 보정 결과) |
| WorldTrackProvider | `DATASET_GT`(공개 데이터의 정답 3D 자세·ID), `SIM` | `TIER0_LOG`(Tier 0를 녹화 입력으로 돌린 결과, 또는 Spatial DB에 기록된 `world_state`와 Tier 0 저장 위치의 2D 관측 Parquet(11.2)) |

1. **같은 필드 의미.** 대체 구현도 실제 구현과 같은 의미를 지킨다. 접지를 모르면 `ground_anchor_xyz`를 비우고 Z=0으로 채우지 않는다. World 좌표는 미터, 오른손, Z-up이다.
2. **출처 기록.** 모든 Provider 출력에 `provider_source`(위 표의 값)와 버전을 붙이고, 이를 소비한 결과물(`scene_output`, `human_mesh_output`, Run 기록)에 `input_provenance`로 그대로 남긴다.
3. **정답 입력은 상한 성능.** `DATASET_GT`나 `SIM` 입력으로 얻은 지표는 "입력이 완벽할 때의 상한"으로 보고하고 제품 성능으로 주장하지 않는다. 채택 판정(E-GATE-4)에는 `TIER0_LOG`·`G1_AUTO` 입력으로 다시 잰 결과가 필요하며, 그 전에는 HOLD 이하로만 판정한다.
4. **제품 Scene의 조건.** COMMITTED Scene은 4.5대로 CALIBRATED 보정에서만 생성한다. CALIBRATED는 `G1_AUTO` 보정이 4.4의 독립 검증점을 통과했을 때만 된다. 계약의 보정 출처 값(`calibration_source`)은 `G1_AUTO`·`DATASET_GT`·`SIM`·`MODEL_ESTIMATE` 뿐이다(마커 보정·평면도 대응점 출처는 v1.3 에서 뺐다, 4.3). `DATASET_GT`·`SIM`·`MODEL_ESTIMATE` 보정은 검증된 현장 보정이 아니므로 CALIBRATED가 될 수 없고, 그 보정으로 만든 결과는 연구 근거로만 쓴다. 보정 버전이 바뀌면 해당 Scene을 재생성 대상으로 표시한다.
5. **Tier 0 코드를 복제하지 않는다.** 오프라인용으로 삼각측량·추적을 따로 구현하지 않는다. `TIER0_LOG`는 Tier 0 서비스를 파일 입력 모드로 돌려 얻는다.
6. **계약 시험.** 모든 Provider 구현은 같은 계약 시험(스키마 검증, R의 직교성, 단위·축 검사, Z=0 대체 금지)을 통과해야 한다(21.3).
7. **데이터 출처.** 공개 데이터는 라이선스(연구용 여부)를 확인한 뒤 쓰고 현장 데이터와 분리해 평가한다(15.4). 우리 녹화는 11.5 동의 범위 안에서만 쓰고, 외형 실험은 별도 동의 승인 뒤에 쓴다.

스키마 파일: `contracts/providers/calibration.schema.json`, `contracts/providers/world_tracks.schema.json`(21.1).

## 20.5 RunPod GPU Job Queue, 자원 보호와 실험 이력

- **개발 기본 정책:** 두 Worker는 설계/구현을 병렬 진행하되 무거운 단일 GPU 추론 작업은 공유 큐로 순차 실행. 중량 Job에 잠금, TTL/heartbeat, 타임아웃, 종료 시 메모리 회수, 재시작 시 상태 복구를 적용한다.
- **안전 경로 격리:** Tier 0와 분석 워커는 같은 GPU를 쓴다(3.2). LIVE 관제 시간에는 VLM 등 관제용 분석만 자원 상한 안에서 실행하고, EXP 추론은 관제가 없는 시간에만 실행한다.
- **격리:** 모델별 Python/CUDA/Torch 환경 또는 컨테이너 격리. 코드 작업은 20.1의 티켓별 Feature Branch, 런 메타데이터와 요약 보고서는 GitLab, 중량 영상·모델·3D 아티팩트는 GPU 실행 사이트의 저장 위치(3.12)에 두고 볼륨이 없으면 결과 묶음으로 가져오며, 환경은 모델별 Docker 이미지로 관리한다.
- **측정:** GPU 실제 모델명과 드라이버·CUDA, 배치/해상도, inference+I/O+render 별 p50/p95, VRAM peak, 처리량/실패율/스토리지 사용을 기록. A40 등 특정 GPU 수치는 실측 없이 확정하지 않는다. 판정·선택에는 시연 GPU(3.12, 미정) 실측값과 공식 벤치마크를 쓰고(시연 GPU 가 정해지기 전 모델 선택은 H100 80GB 기준), 노트북 등 다른 GPU 수치는 참고값으로 구분해 기록한다.
- **재현:** run_id, experiment_id(실험 EXP-01·EXP-02·INT-01, 운영 경로 검증 Run 은 VAL-01: GPU 세션 검증 큐 등, 채택 게이트 근거 아님·`e_gate` 없음, 정답·SIM 입력 지표는 제품 성능 아님), owner, git_commit, env_hash, model_version, weights_hash, dataset_hash, config, source_camera/calibration_version, 결과 URI, 메트릭, 리뷰 의견을 필수로 남긴다.
- **운영 비용:** GPU 크레딧·사이트·디스크 비용, 이미지·가중치 받기와 결과 회수 시간을 별도로 기록한다. 사이트를 바꿀 때(3.12)의 준비 시간과 비용, 시연 사이트 GPU 등급별 시간당 비용도 기록한다.

저장소 구조는 21.1을 따른다. 실험 코드는 `ai/experiments/{static_scene,hmr}/{configs,eval,reports}`, 계약 스키마는 저장소 최상위 `contracts/`(`scene_output`, `human_mesh_output`, `run_record`), 실험 색인은 `docs/experiments/`에 둔다.

## 20.6 채택 게이트 및 변경 통제

- **E-GATE-0:** 모델/가중치·샘플 데이터·환경 설치 및 단일 입력 Baseline 재현
- **E-GATE-1:** 다인·다시점·장시간·가림·고장 등 실패 사례 포함한 정량 평가
- **E-GATE-2:** 미터 좌표/버전/시각/ID 계약과 Three.js 최소 데모 재현
- **E-GATE-3:** Tier 0 독립 동작, 장애 시 폴백, 개인정보/저장 정책, Review Agent 독립 검증
- **E-GATE-4:** Lead가 ADOPT/HOLD/REJECT, 부분 채택·대안·MVP/확장 영향/선행 조건 기록

G0\~G8은 기존 제품 품질 게이트로 유지한다. EXP 게이트는 연구 채택 승인에만 사용하며 수치 합격 기준은 RunPod 실제 Baseline 보고서 이후 결정한다. 모델 교체/정량 기준 변경/얼굴 관련 범위 변경은 §16 결정 기록과 §15의 명시적 개정을 거쳐야 한다.

## 20.7 요구사항 명세서와 추적 연결

- **FR-27·FR-90, FR-102·FR-105\~109·FR-111:** EXP-01 및 공통 GPU/Scene/채택.
- **FR-3·FR-7·FR-92, FR-103\~105·FR-109\~111:** EXP-02/INT-01와 실사형 메시 확장.
- **NFR-1\~17(기준 유지), NFR-18\~23:** 재현성·GPU 격리·지연 측정·안전 경로 독립·좌표 정합·외형 데이터 보호.
- 동일 ID/용어·MVP/확장 구분·검증 게이트는 요구사항 명세서(`docs/requirements.md`. 저장소 이전 전 원본은 Notion 261016 판)와 일치시킨다.
