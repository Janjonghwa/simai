# 20. GPU 확보 이후 대형 Experiment 운영과 채택 절차

이 절은 §5의 모델 계층, §3.2의 단일 Tier 0 정본, §15의 G0\~G9과 독립적인 **연구·실험 운영 계약**이다. 요구사항 명세서의 FR-102\~111, NFR-18\~23과 대응한다. 기존의 작은 PoC 여러 건을 기능을 완결하는 대형 Experiment 두 개로 통합하되, 내부 Run과 체크포인트는 남긴다.

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

**연구 질문:** MapAnything Native의 결과(비교군: Depth Anything 3 의 두 경로, 5.3)를 10×10m, 4대 고정 카메라 관측과 World 좌표(4.1)에 정합해 COMMITTED Scene에 사용할 수 있는 시각 배경(glTF)으로 만들 수 있는가?

1. **Baseline:** 소수의 빈 교실 프레임, K·pose, 가중치·CUDA 환경을 고정하고 depth/point map/camera 출력을 기록.
2. **복원:** 뷰 간 정합→이상점 제거→표면 생성/메시→단순화. 분석 워커의 I5 `static_reconstruct` 작업은 Tier 0 가 누적한 사람 없는 배경(`CLEAN_BACKGROUND`) 또는 `YOLO26x-seg` 마스크로 사람을 지운 프레임(`FRAME`)으로 MapAnything(known-K) → Open3D TSDF → `texrecon` 텍스처 메시(`GLTF_VISUAL`)와 표면 출처 지도(`SURFACE_SOURCE_MAP`)를 생성하며, 도구 미설치 환경에서는 정점색 단순 메시 glTF 로 폴백한다(5.3, `docs/design/worker-static-reconstruct.md`).
3. **정렬:** §4의 월드 미터/Z-up, World 원점/축/스케일, calibration_version에 정렬. 관측된 형상과 추정된 공간 구분.
4. **검증:** G4 개발 교실 줄자 실측 대비 10cm, 책상 상판 높이 ±5cm·위치 10cm, 공동 관측 점 중앙값 5cm. 실패/가림/반사/저텍스처 사례도 보고.
5. **통합:** glTF와 Scene 버전/해시/검증 결과를 내보내고 Three.js 실제 화면에서 배경을 확인.

**비교 경로와 채택 기준(`docs/design/exp01-hybrid-depth.md`).** 비교군은 (a) MapAnything 자세 고정 + DA3 자세 조건 깊이(깊이 단위 = MapAnything 단위, 배경 밀도용, 독립 축척 근거 아님)와 (b) 자세 없이 DA3NESTED-GIANT-LARGE-1.1 단독(미터 추정, 축척 교차 확인 후보, 우선순위 가장 낮음)이고, MoGe 단안 경로는 큐에서 뺐다(5.3, Lead 결정 2026-10-26). 기준선은 MapAnything 깊이를 원본 해상도로 올리기만 한 결과(fake)다. 같은 입력에서 DA3 결과가 기준선보다 카메라 간 깊이 차(|Δd|/d)와 왕복 재투영이 작고, 바닥 rms 1.5 cm 이하, 충돌로 뺀 점 비율 5% 이하, 분석 워커 GPU 예산 안이면 배경용 깊이로 쓸 만하다고 보고 G4 실측으로 넘어간다. MapAnything 입력 해상도 비교(518·1036·1554·원본 2296×1288, 보정용·배경용 따로 판정)는 5.3 을 따른다.

**실패 대안:** G4 미달 시 DA3 혼합 경로(MapAnything 자세 + 원본 해상도 깊이, S15P31A101-283) + 실측 정물 축척(S15P31A101-100), 그래도 실패하면 관리자가 등록한 단순 형상(바닥 평면 + 보수적 OBB). MapAnything 포인트/깊이가 정상 생성됐다는 사실만으로 glTF가 성공했다고 판정하지 않는다.

**필수 산출물:** 재현 명령, 모델/입력 해시, 카메라 매개변수, 샘플 점군·glTF, 월드 정렬 결과, 성능(p50/p95·VRAM peak), 실패/복구 기록, Reviewer 판정.

## 20.3 EXP-02 — Human Mesh Reconstruction 및 Appearance Update

**연구 질문·시연 범위:** Fast SAM 3D Body의 MHR 리깅 메시/포즈를 World ID에 결합해 동적으로 움직이는 메시 아바타를 만들고, 같은 인물의 연속 관측이 늘 때 실제 외형에 가까워지는 시각 표현을 향상시킬 수 있는가? (2026-10-23 사용자 결정으로 EXP-02는 별도 승인 확장 후보가 아니라 시연 범위로 확정했고, 1단계 MHR 리깅 메시 + 포즈 스트림(상·하의 대표색)과 2단계 관측 픽셀 UV 텍스처 누적으로 점진 도입하며 3DGS는 쓰지 않는다. Tier 1은 계속 안전 판단 권한이 없고 관절은 inferred, root 위치는 Tier 0 `world_state`가 정본이며 위험 판정에 쓰지 않는다, 5.6·16장.)

1. **Baseline:** 단일 인물 샘플에서 MHR 메시·native_pose·canonical_motion을 출력하고 Three.js 메시 렌더링 재현.
2. **다인·동작:** 4대 영상의 World ID 및 촬영 시각과 대응, 교차·가림·재등장/포즈 안정성 검증. Local Track ID를 인물 정체성의 단일 정본으로 사용하지 않음.
3. **좌표 정렬:** §5.2 Tier 0 pelvis_xyz·position_xy·ground_anchor_xyz와 카메라/월드 변환을 계약으로 연결. 지지면 모를 때 Z=0을 가정하지 않음.
4. **외형 개선:** 인물별로 관측된 표면의 텍스처와 관측되지 않은/보간된 영역을 구분해 누적 갱신한다. 외형 개선과 신원 재식별은 분리하고, 생성형 얼굴 복원·표정 추정·실명 인식은 범위 밖이다(재식별은 전신 임베딩을 기하 게이트 뒤 보조 비용으로만 쓴다, G2 측정 뒤 도입, 5.2). 두 단계로 점진 도입한다(3DGS 미사용).
   - 1단계(E-GATE-2): MHR 리깅 메시(SkinnedMesh) + 포즈 스트림(I6 `avatar_pose_batch` → I2 `avatar_frame`, 12.2)으로 구동하고 기본 재질(상·하의 대표색)로 칠한다.
   - 2단계: 관측 픽셀만 쓰는 다시점 UV 텍스처 누적(`SIMAI_AVATAR_APPEARANCE=auto`, CUDA 기본 켬, 2026-10-30 사용자 결정). 가시성 필터(z-buffer·시선 각도·아바타 루프 `YOLO26x-seg` 전경 마스크 `SIMAI_AVATAR_SEG=auto`·메시 실루엣 침식 및 감쇠) → UV 투영(구현은 새 외부 의존성 없는 `torch`/`numpy` 원근 보정 z-buffer 래스터화, 후보 PyTorch3D·nvdiffrast) → 품질 가중(라플라시안 선명도·Tier 0 대 HMR COCO17 재투영 오차 가중·카메라 간 공통 고품질 텍셀 RGB 채널 이득 EMA 맞춤) 및 신뢰도 가중 누적 `T_new = (W_old·T_old + w·C_obs) / (W_old + w)` → 이상 관측 거절 → 버전 기록(`appearance_version`, 추가만)·되돌리기(`docs/design/hmr-appearance-texture.md`, `docs/design/hmr-avatar-loop.md`). 사람별 canonical 메시(MHR 스키닝으로 기준 자세에 정렬) 위에 쌓는다. 촬영·외형 누적은 기관(SSAFY) 허가 기준이며 별도 참여자 동의 조건은 두지 않는다(11.5). MHR↔SMPL 변환은 https://github.com/facebookresearch/MHR/blob/main/tools/mhr_smpl_conversion/README.md 를 참고한다.
   - 보이지 않는 면·얼굴을 생성하는 방식(SiTH·FISHuman, 30\~50px 물리 한계)과 옷 형상 복원(ReMu, ECON), 3DGS 아바타는 쓰지 않는다(16장).
   - World ID 가 `IDENTITY_UNCERTAIN` 이거나 `identity_revision` 이 바뀐 직후(병합·분할·교정 뒤 다시 안정될 때까지, 초기값 30 프레임)에는 텍스처 누적을 멈추고 마지막 확정 외형을 유지한다(자산 차수 ≤ 포즈 차수 허용; 순수 병합 `pure_merge` 시에는 직전 체형 표본·메시·UV 외형 자산을 새 `identity_revision` 으로 승계하되 안정 카운트만 0으로 되돌리고, 뷰어도 같은 revision 의 짧은 `IDENTITY_UNCERTAIN` 은 `1.5 s` 까지 메시를 유지한다. 반면 갈라져 나간 분할 자식 ID 는 원래 ID 자산을 물려받지 않고 새 자산 전까지 캡슐로 그린다, 5.6·13.2, `docs/design/S15P31A101-661-merge-carryover.md`). 다른 사람의 외형이 같은 아바타에 섞이지 않게 하기 위해서다.
   - 얼굴 영역은 관측된 얼굴 픽셀을 그대로 쓸 수 있고(`face_policy` 기본 OBSERVED, BLURRED·EXCLUDED 선택), 안 보이는 얼굴을 지어내는 생성형 복원·표정 추정은 하지 않는다. 표면 출처 지도(무손실 8비트 PNG, 값 0·1·2 와 얼굴 +128)와 비율(`surface_source.ratios`·`face_ratios`, 얼굴 영역은 보간 없음)로 관측·보간·미관측을 구분하며, 안 보인 얼굴 면(`face_ratios.UNOBSERVED`)은 기본 재질로 둔다(11.5, 13.2).
5. **정량·정성 검증:** 원본 SAM 3D Body/기본 골격 기준선과 추론 지연, VRAM, 메시 품질, 다인 안정성, 연속 프레임 드리프트, 관측 누적 전후 시각 품질 비교.
6. **Scene 통합:** 같은 월드에 배경과 인물 메시를 배치하고 라이브 경보/관측 상태가 HMR 지연 시에도 정상 표시되는지 확인.

**필수 산출물:** MHR 메시/포즈, 변환 스키마·샘플 JSON, 다인/연속 프레임 데모, 관측 기반 외형 개선 비교, 렌더링/성능 측정, 실패 사례와 개인정보 점검 기록.

**불합격 조건:** 메시만 생성하고 동적 아바타를 못 보여주는 경우, 추정 외형을 실측으로 표현하는 경우, 외형으로 Tier 0 위험 규칙을 변경하는 경우. 2단계 관측 기반 외형의 품질·성능이 기준에 못 미치면 1단계(MHR 리깅 메시 + 포즈 스트림·대표색)까지만 시연에 쓰고 2단계는 보류(HOLD)한다.

## 20.4 INT-01 · 공간과 인체의 인터페이스 계약

**정본 불변:** World XYZ·검증된 관절·RiskEvent와 CALIBRATED 상태는 §5.2와 §3.2가 관리한다. HMR/Scene은 Visual Geometry 자산이다.

| 필드/형식 | 계약 |
|---|---|
| camera_id, timestamp_source, estimated_capture_time | 기존 §3.9의 카메라 식별/촬영 시각 계약과 통일 |
| world_id, local_track_id | 익명 World ID 기준, 카메라 로컬 추적 ID는 출처로 보존 |
| scene_version, calibration_version | 결과를 생성한 정적 장면/보정 버전과 일치 |
| coordinate_frame, unit, transform | Canonical Metric World, m, 오른손 +Z-up. Three.js 변환은 §4.2의 (x,z,-y) 한 곳에서 수행 |
| mesh_geometry, native_pose, canonical_motion | MHR 정점/면 형상과 포즈 데이터, 최소 렌더링 예제. 실시간 아바타 스트림(I6 `avatar_pose_batch`·`avatar_asset` → I2 `avatar_frame`, 12.2)은 `native_pose`(MHR 127관절 부모 기준 축각 라디안 회전과 `root_rotation`, 위치 없음)만 싣고, `canonical_motion` 은 오프라인 Run 기록(`experiments/human_mesh_output`)에만 둔다(5.6) |
| observation_status, joint_source, surface_source | OBSERVED/PREDICTED/UNKNOWN, observed/inferred(HMR 스트림은 inferred 고정), 관측/보간/미관측 표면과 얼굴 영역(`face_ratios`, 보간 없음) 분리 |
| model_version, input_hash, run_id | 재현과 감사 추적 메타데이터 |
| 아바타 구동 | 사람의 존재·root 위치·접지·관측 상태는 Tier 0(`/topic/world`, 5.2)가 정본이고, HMR(`/topic/avatar`)은 관절 회전(축각 라디안, MHR 127관절 순서·부모 트리 해시 검증, 모르는 관절은 null)과 체형·외형 자산만 제공한다. 메시의 발은 접지가 확인되면 `ground_anchor_xyz`에 맞춘다. 뷰어는 root 위치를 지연시키지 않고 관절 회전만 250 ms 버퍼로 slerp 하며, 수신 끊김(0.5 s)·촬영 시각 지연(500 ms)·ID 불확실/차수 불일치·보정 버전 불일치·자산 미준비(13.2의 5조건) 시 즉시 캡슐 아바타로 돌아간다(세부는 `docs/design/avatar-pose-stream.md`·`contracts/`). G5 발-지지면 오차는 Tier 0 지표다(15.1) |

오프라인 실험 산출물은 `experiments/human_mesh_output`(2.0.0)에 두고, 실시간 아바타 스트림은 I1 과 분리된 I6(`avatar_pose_batch`·`avatar_asset` 1.0.0)과 I2 `/topic/avatar`(`avatar_frame` 1.0.0, 계약 묶음 6.2.0, 12.2·12.4)로 보낸다. I1 LIVE World 계약은 Reviewer 검토와 Lead 승인 없이 수정하지 않는다. HMR 업데이트가 늦거나 되돌아가기 5조건에 걸리면 §13의 캡슐 아바타(또는 비상 2D 화면)로 즉시 돌아가고 staleness를 노출한다.

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
3. **정답 입력은 상한 성능.** `DATASET_GT`나 `SIM` 입력으로 얻은 지표는 "입력이 완벽할 때의 상한"으로 보고하고 제품 성능으로 주장하지 않는다. 최종 검증·채택 판정(E-GATE-3·4)에는 `TIER0_LOG`·`G1_AUTO` 입력으로 다시 잰 결과가 필요하며, 그 전에는 HOLD 이하로만 판정한다.
4. **제품 Scene의 조건.** COMMITTED Scene은 4.5대로 CALIBRATED 보정에서만 생성한다. CALIBRATED는 `G1_AUTO` 보정이 4.4의 독립 검증점을 통과했을 때만 된다. 계약의 보정 출처 값(`calibration_source`)은 `G1_AUTO`·`DATASET_GT`·`SIM`·`MODEL_ESTIMATE` 뿐이다(마커 보정·평면도 대응점 출처는 v1.3 에서 뺐다, 4.3). `DATASET_GT`·`SIM`·`MODEL_ESTIMATE` 보정은 검증된 현장 보정이 아니므로 CALIBRATED가 될 수 없고, 그 보정으로 만든 결과는 연구 근거로만 쓴다. 보정 버전이 바뀌면 해당 Scene을 재생성 대상으로 표시한다.
5. **Tier 0 코드를 복제하지 않는다.** 오프라인용으로 삼각측량·추적을 따로 구현하지 않는다. `TIER0_LOG`는 Tier 0 서비스를 파일 입력 모드로 돌려 얻는다.
6. **계약 시험.** 모든 Provider 구현은 같은 계약 시험(스키마 검증, R의 직교성, 단위·축 검사, Z=0 대체 금지)을 통과해야 한다(21.3).
7. **데이터 출처.** 공개 데이터는 라이선스(연구용 여부)를 확인한 뒤 쓰고 현장 데이터와 분리해 평가한다(15.4). 우리 녹화와 외형 누적은 11.5의 기관(SSAFY) 허가 기준 안에서 쓴다(참여자 별도 동의 조건 없음).

스키마 파일: `contracts/providers/calibration.schema.json`, `contracts/providers/world_tracks.schema.json`(21.1).

## 20.5 RunPod GPU Job Queue, 자원 보호와 실험 이력

- **개발 기본 정책:** 두 Worker는 설계/구현을 병렬 진행하되 무거운 단일 GPU 추론 작업은 공유 큐로 순차 실행. 중량 Job에 잠금, TTL/heartbeat, 타임아웃, 종료 시 메모리 회수, 재시작 시 상태 복구를 적용한다.
- **안전 경로 격리:** Tier 0와 분석 워커는 같은 GPU를 쓴다(3.2). LIVE 관제 시간에는 VLM 등 기존 관제용 분석과 시연 범위인 EXP-02 상시 아바타 스트림 루프(`services/avatar`, Tier 0 지연 감시 표시 파일로 지연 초과·모름 시 즉시 멈춤, 3.2 접수 규칙)만 분석 워커 자원 상한 안에서 실행하고, 오프라인·배치 EXP 추론(I5 배치 `hmr_*`·`static_reconstruct` 포함)은 관제가 없는 시간에만 실행한다.
- **격리:** 모델별 Python/CUDA/Torch 환경 또는 컨테이너 격리. 코드 작업은 20.1의 티켓별 Feature Branch, 런 메타데이터와 요약 보고서는 GitLab, 중량 영상·모델·3D 아티팩트는 GPU 실행 사이트의 저장 위치(3.12)에 두고 볼륨이 없으면 결과 묶음으로 가져오며, 환경은 Docker 이미지(Tier 0 `tier0`, Tier 0·VLM·HMR·MapAnything 통합 `unified`, 실험 이미지 HMR `exp02-fs3db`·MapAnything `mapanything`, 3.12)로 관리한다. 통합 이미지의 MapAnything 은 LIVE 가 아닐 때 `simai-bootstrap scene` 일회성 모드로 돌리고(`mapanything` 이미지는 2.7.1↔2.11 동등성 비교까지만 유지), 별도 실험 이미지는 Tier 0 와 다른 인스턴스로 띄운다.
- **측정:** GPU 실제 모델명과 드라이버·CUDA, 배치/해상도, inference+I/O+render 별 p50/p95, VRAM peak, 처리량/실패율/스토리지 사용을 기록. A40 등 특정 GPU 수치는 실측 없이 확정하지 않는다. 판정·선택에는 시연 GPU(3.12, 미정) 실측값과 공식 벤치마크를 쓰고(시연 GPU 가 정해지기 전 모델 선택은 H100 80GB 기준), 노트북 등 다른 GPU 수치는 참고값으로 구분해 기록한다. GPU 제공 사이트는 Run 기록 `gpu.provider` 에 RUNPOD·VAST·OTHER(그 밖의 클라우드, 사이트 신호가 없으면 Job 에 적음)·LOCAL(클라우드가 아닌 로컬 장비)로 남긴다.
- **재현:** run_id, experiment_id(실험 EXP-01·EXP-02·INT-01, 운영 경로 검증 Run 은 VAL-01: GPU 세션 검증 큐 등, 채택 게이트 근거 아님·`e_gate` 없음, 정답·SIM 입력 지표는 제품 성능 아님), owner, git_commit, env_hash(이미지 다이제스트, 21.5), model_version, weights_hash, dataset_hash, config, source_camera/calibration_version, 결과 URI, 메트릭, 리뷰 의견을 필수로 남긴다.
- **운영 비용:** GPU 크레딧·사이트·디스크 비용, 이미지·가중치 받기와 결과 회수 시간을 별도로 기록한다. 사이트를 바꿀 때(3.12)의 준비 시간과 비용, 시연 사이트 GPU 등급별 시간당 비용도 기록한다.

저장소 구조는 21.1을 따른다. 실험 코드는 `ai/experiments/{static_scene,hmr}/{configs,eval,reports}`, 계약 스키마는 저장소 최상위 `contracts/`(`scene_output`, `human_mesh_output`, `run_record`), 실험 색인은 `docs/experiments/`에 둔다.

## 20.6 채택 게이트 및 변경 통제

- **E-GATE-0:** 모델/가중치·샘플 데이터·환경 설치 및 단일 입력 Baseline 재현
- **E-GATE-1:** 다인·다시점·장시간·가림·고장 등 실패 사례 포함한 정량 평가
- **E-GATE-2:** 미터 좌표/버전/시각/ID 계약과 Three.js 최소 데모 재현
- **E-GATE-3:** Tier 0 독립 동작, 장애 시 폴백, 개인정보/저장 정책, Review Agent 독립 검증
- **E-GATE-4:** Lead가 ADOPT/HOLD/REJECT, 부분 채택·대안·MVP/확장 영향/선행 조건 기록

G0\~G9은 기존 제품 품질 게이트로 유지한다. EXP-02(MHR 리깅 메시·포즈 스트림 1단계와 관측 픽셀 UV 텍스처 누적 2단계)는 별도 채택 승인을 기다리는 확장 후보가 아니라 시연 범위로 확정했고(2026-10-23 사용자 결정, 5.6·16장), E-GATE-0\~3 은 시연 범위의 단계별 품질·성능·Tier 0 독립 및 캡슐 폴백 검증 기준으로 쓴다(EXP-01 은 계속 E-GATE-0\~4 채택 절차를 따름). 수치 합격 기준은 RunPod 실제 Baseline 보고서 이후 결정한다. 모델 교체/정량 기준 변경/얼굴 관련 범위 변경은 §16 결정 기록과 §15의 명시적 개정을 거쳐야 한다.

## 20.7 요구사항 명세서와 추적 연결

- **FR-27·FR-90, FR-102·FR-105\~109·FR-111:** EXP-01 및 공통 GPU/Scene/채택.
- **FR-3·FR-7·FR-92, FR-103\~105·FR-109\~111:** EXP-02/INT-01과 시연 범위 인체 메시·외형 아바타(5.6, 20.3).
- **NFR-1\~17(기준 유지), NFR-18\~23:** 재현성·GPU 격리·지연 측정·안전 경로 독립·좌표 정합·외형 데이터 보호.
- 동일 ID/용어·MVP/확장 구분·검증 게이트는 요구사항 명세서(`docs/requirements.md`. 저장소 이전 전 원본은 Notion 261016 판)와 일치시킨다.
