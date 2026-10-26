# MHR → 스키닝 glTF 내보내기와 포즈 변환 (S15P31A101-88)

EXP-02 1단계(MHR 리깅 메시 + 포즈 스트림)의 AI 쪽 산출물이다. MHR 자산에서 사람별 스키닝 glTF(.glb)를 만들고, HMR 출력을
아바타 스트림 계약(`mhr_pose`, `docs/design/avatar-pose-stream.md`)의 관절 회전으로 바꾸며, 둘을 합친 LBS 가 HMR 메시와 같은지 잰다.
책임 나눔(Jira -88 댓글): 이 티켓은 MHR → skinned glTF·리그·포즈 산출물과 검증 샘플, Three.js 제품 표시·World 정렬·폴백은 -65,
워커 송신·업로드는 -227 이 이 티켓의 함수를 부른다. 계약·명세는 바꾸지 않았다(다른 점은 아래 '계약·문서와 다른 점').

## 관련 명세

| 절 | 내용 | 이 문서에서 |
|---|---|---|
| 20.3 4·1단계 | MHR 리깅 메시(SkinnedMesh) + 포즈 스트림, 기본 재질 | glTF 구성, 텍스처 없음 |
| 20.4 | 스트림은 native_pose(127관절 부모 기준 축각, root_rotation, 위치 없음), 모르는 관절 null, 위치는 Tier 0 | 포즈 변환, 손가락 null, pelvis 맞춤 |
| 5.6 | HMR 관절은 inferred, Tier 0 정본을 덮지 않음(AGENTS 규칙 1) | 위치는 버리고 회전·체형만 |
| AGENTS 규칙 8 | 생성형 얼굴 없음 | 메시만, 텍스처 없음(외형은 -89) |

## 코드

| 위치 | 내용 |
|---|---|
| `ai/src/simai_ai/hmr/rig.py` | 자산 읽기(`load_mhr_rig`, sha256 확인, torch 는 여기서만), 상위 4 가중치(`reduce_skin_weights`), 체형 굽기(`bake_person_rig`, `bake_person_rig_from_model_params`), `joint_parents_sha256` |
| `ai/src/simai_ai/hmr/gltf_export.py` | `.glb` 쓰기(`build_skinned_glb` → `SkinnedGlb`: 바이트·sha256·정점·면 수), 읽기(`read_glb`, `accessor_array`) |
| `ai/src/simai_ai/hmr/pose.py` | HMR → 계약 포즈(`pose_from_model_params` 권장, `pose_from_hmr`), `world_root_rotation`, `mhr_pose` |
| `ai/src/simai_ai/hmr/lbs.py` | glTF 노드 순기구학·LBS 참조 구현(뷰어와 같은 식) |
| `ai/src/simai_ai/hmr/rotation.py` | 쿼터니언(xyzw)·축각·행렬·MHR 오일러 변환 |
| `ai/experiments/hmr/verify_mhr_gltf.py` | 교실 20장 검증·샘플 산출물 |
| `ai/experiments/hmr/mhr_pose_viewer.html` | 확인용 정적 페이지(Three.js 0.160.0 jsdelivr 고정, 제품 뷰어 아님) |
| `ai/tests/hmr/` | 합성 리그(관절 5개) 구조·역바인드·회전 왕복·LBS 일치·계약 시험, 실자산 시험(`SIMAI_MHR_ASSET` 있을 때만) |

새 의존성은 없다. glTF 는 표준 라이브러리(struct·json·hashlib) + numpy 로 쓴다. 자산을 읽을 때만 torch(hmr extra 에 이미 있음)를 쓴다.

## 리그 추출

자산 `mhr_model.pt`(TorchScript, sha256 `352e271a…77bc` = `deploy/weights.json` 의 `sam-3d-body-dinov3-mhr`)의 `character_torch` 버퍼를 numpy 로 꺼낸다.

| 항목 | 자산 버퍼 | 값 |
|---|---|---|
| 관절 | `skeleton.joint_parents`·`joint_translation_offsets`(cm)·`joint_prerotations`(xyzw), `get_joint_names()` | 127개, 부모 < 자기 번호, 관절 0 `body_world`(바닥 원점) → 1 `root`(pelvis, 92.4 cm). 부모 배열 해시 `sha256:46ebaa1f…5225`(계약 예시와 같음) |
| 관절 파라미터 | `parameter_transform` (889 × 249) | 모델 파라미터 204(전역 이동 3·전역 회전 3·몸 자세 130·체형 scale 68) + 정체 45 자리 → 관절마다 [이동 3, 오일러 3, log2 크기 1] |
| 관절 상태 | `joint_parameters_to_local_skeleton_state` | local t = 이동 + 오프셋, q = 사전 회전 · Rz·Ry·Rx(오일러), s = 2^크기. global 은 glTF 노드 TRS 곱과 같은 식 |
| 템플릿 역바인드 | `linear_blend_skinning.inverse_bind_pose` (127 × 8) | 파라미터 0 의 순기구학 역(실자산 시험으로 확인, 오차 3e-7) |
| 형상 | `blend_shape.base_shape`·`shape_vectors` (45) | 정체 blendshape. `face_expressions`(72)·`pose_correctives` 는 쓰지 않는다(아래) |
| 면·UV | `mesh.faces` (36,874), `texcoords` (19,455)·`texcoord_faces` | UV 는 위치와 다른 번호라 glTF 에서 정점을 나눈다 |
| 스킨 | `skin_*_flattened` (51,337) | 정점당 영향 최대 4개(1개 3,925·2개 3,491·3개 3,662·4개 7,361). 관절 0 에는 가중치가 없다 |

## 좌표 규약

- **리그 좌표 = MHR 자산 좌표 = glTF Y-up**, 단위는 cm → m(× 0.01). 계약 `GLTF_Y_UP`.
- HMR 의 정점·관절 위치는 리그 좌표에 **F = diag(1, −1, −1)** 을 곱한 카메라 좌표(OpenCV: x 오른쪽, y 아래, z 앞)이고, `pred_cam_t` 는 버린다.
  `pred_global_rots`·`mhr_model_params` 의 회전은 F 를 곱하지 않은 리그 좌표 값이다(Fast-SAM-3D-Body `mhr_head.py`).
- **몸 방향은 관절 1 에 있다.** MHR 의 global_rot(파라미터 3~5)는 관절 1(pelvis)을 돌리고 관절 0 은 늘 단위다. 계약은 관절 0 회전 = 0, 몸 방향 = `root_rotation` 이므로
  pelvis 의 global 회전 G_pelvis 를 통째로 root 쪽으로 옮기고 관절 0·1 회전을 `[0, 0, 0]` 으로 싣는다. 회전 중심이 pelvis 에서 리그 원점(바닥)으로 바뀌어 생기는 차이는
  이동뿐이라, 뷰어가 pelvis 관절을 Tier 0 `pelvis_xyz` 에 맞추면 사라진다(관절 0 에 스킨 가중치가 없어서 정확하다. 실자산 시험이 확인).
- **camera_from_rig = F · G_pelvis** (리그 → HMR 카메라). World 로는 `root_rotation = Rᵀ · camera_from_rig`, R 은 보정 외부 파라미터(X_cam = R·X_world + t).
  수평 카메라 앞에 똑바로 선 사람은 Rx(+π/2) 가 나온다(시험). HMR 입력 영상은 왜곡을 편 영상이고 왜곡 펴기는 카메라 축을 돌리지 않으므로 같은 R 을 쓴다.
- 관절 회전은 부모 기준 local 축각(회전각 ≤ π), 소수 넷째 자리 반올림. 손가락 43~64·79~100 은 body 모드면 null, full 모드면 값을 싣는다.
- Three.js: 뷰어 어댑터 한 곳에서 `quat(A)·quat(root_rotation)`, A = Rx(−π/2) (`avatar-pose-stream.md` 와 같음). 확인 페이지도 이렇게 한다.

## 체형 굽기

노드 휴지 TRS 에 그 사람 체형을 굽고, 역바인드는 템플릿 것을 그대로 둔다. 정점은 정체 blendshape 를 적용한 **템플릿 자세 형상**이다.
glTF 스키닝은 `노드 global · 역바인드 · 정점` 이고 MHR 도 `관절 상태(체형 반영) · 템플릿 역바인드 · 정점` 이라 식이 같다(검증 오차 0.001 mm, 반올림 없이).

- 체형 scale 68 은 노드 이동(뼈 길이, 99개 관절의 x 등), 손목 2개의 균일 크기(손 크기), 무릎 2개의 휴지 회전(안쪽 휨)으로 간다.
- 몸 자세 파라미터 중 `*_flexible` 6개(spine·neck·arm·leg 길이, shoulder·hip 폭)는 **이동 채널**을 움직인다. 스트림은 회전만 싣으므로 이 값은 자산에 굽는다
  (`bake_person_rig(..., body_pose=...)`). 체형을 고정하는 30 프레임의 평균을 넣는다(-227). 아래 검증에서 이것이 가장 큰 잔여 오차다.
- 노드 휴지 회전 = 사전 회전 · 오일러(체형 파라미터 기여만). 포즈가 null 인 관절은 이 바인드 회전을 둔다.
- scale 68 = `scale_mean + scale_params · scale_comps` 인데 이 두 값은 MHR 자산이 아니라 SAM 3D Body 모델의 `head_pose.*` 다(`scales_from_scale_params`).
  모델의 `mhr_model_params[136:204]` 가 같은 값이다.

## glTF 구성

| 항목 | 값 |
|---|---|
| 노드 | 0~126 = MHR 관절 i(이름 = 자산 관절 이름, 자식 = 부모 배열), 127 = 메시 노드(루트, skin 0). 장면 루트 = [0, 127] |
| skin | `joints` = [0..126](= MHR 관절 i), `skeleton` 0, `inverseBindMatrices` MAT4 FLOAT(열 우선) |
| 정점 속성 | POSITION(min/max)·NORMAL(면적 가중, 단위)·JOINTS_0(UNSIGNED_BYTE, 가중치 0 칸은 0)·WEIGHTS_0(FLOAT, float32 합 1 보정)·TEXCOORD_0(v → 1 − v)·`_MHR_VERTEX_INDEX`(FLOAT, 분할 전 MHR 정점 번호) |
| 인덱스 | UNSIGNED_SHORT(정점 ≤ 65,535) |
| 재질 | 기본 회색 하나(텍스처 없음, 외형은 -89). 얼굴은 메시만 |
| `extras.simai` | rig·joint_count·joint_parents_sha256·pelvis_joint_index·rig_source·axis·unit·mhr_vertex_count·역바인드 방식 |
| 크기 | 정점 19,455(UV 이음매 분할), 면 36,874, 약 1.34 MB |

- `_MHR_VERTEX_INDEX` 는 분할 정점 → MHR 정점 대응이다. 외형 투영(-89)·얼굴 영역 정점 목록(-227)·검증이 MHR 정점 번호로 일하므로 넣었다. 밑줄로 시작하는 응용 속성이라 glTF 규약상 허용된다.
- 같은 입력이면 같은 바이트(sha256)가 나온다.
- 검증: Khronos glTF-Validator 2.0.0-dev.3.10 으로 샘플 .glb 를 확인했다(오류 0, 경고 0, 정보 1: 텍스처가 없어 TEXCOORD_0 미사용). 시험은 검증기 없이 구조(청크·4바이트 정렬·accessor min/max·가중치 합·관절 범위·노드 계층)를 본다.

## 상위 4 가중치

`reduce_skin_weights` 가 정점마다 가중치 큰 4개를 남기고 합 1 로 나눈다. MHR 자산은 영향이 정점당 최대 4개라 **버린 가중치 합 최대값 = 0** 이고 상위 4 로 줄인 영향은 없다
(다른 자산 판에서 5개 이상이 생기면 `MhrRig.dropped_weight_max` 에 남는다). glTF float32 로 바꾼 뒤 합 오차는 가장 큰 가중치에 더한다.

## 포즈 변환 두 경로

| 함수 | 입력 | 쓸 때 |
|---|---|---|
| `pose_from_model_params(rig, mhr_model_params, hands_estimated=)` | 모델 `mhr_model_params` (204, 손 IK 뒤 재계산 값) | **권장.** 자산과 같은 계산(파라미터 변환 → 사전 회전 · 오일러) |
| `pose_from_hmr(pred_global_rots, parents, hands_estimated=)` | 추정기 출력 `pred_global_rots` (127, 3, 3) | body 모드, 또는 global 회전을 재계산 뒤 값으로 받을 때 |

Fast-SAM-3D-Body full 모드는 손 디코더 결과로 손·손목을 바꾼 뒤 메시를 다시 계산하는데, 그 블록(`sam3d_body.py`)이 `pred_vertices`·`pred_joint_coords`·`mhr_model_params` 는 갱신하고
`joint_global_rots` 는 갱신하지 않는다. 그래서 손이 바뀐 사람은 `pred_global_rots` 로 만든 손 회전이 메시와 다르다(교실 69명 중 7명, p95 34~78 mm). 추정기의 `process_one_image` 는
`mhr_model_params` 를 내보내지 않으므로 -227 의 워커가 `pose_output["mhr"]["mhr_model_params"]` 를 꺼내 쓴다(추정기 결과 dict 에 한 줄 더하기).

## 검증

방법(`verify_mhr_gltf.py`): 사람마다 `.glb` 를 만들고 **파일에서 다시 읽은** 노드 TRS·역바인드·float32 가중치와, 계약 형식으로 반올림한 포즈(`root_rotation` 대신 camera_from_rig)로
LBS 를 돌린다. pelvis 관절 위치만 HMR `pred_joint_coords[1]` 에 맞추고(뷰어가 Tier 0 pelvis 에 맞추는 것과 같음, 회전·크기 맞춤 없음) `pred_vertices` 와 정점마다 거리(mm)를 잰다.
입력은 3차 GPU 세션 `H7u_calib_k_unified_img_20261026`(교실 20장, 69명, full 모드, `MHR_NO_CORRECTIVES=1`). npz 에 `mhr_model_params` 가 없어 mhr_head 의 조립
(global_rot·몸 자세·손 PCA → 오일러·scale)을 스크립트에서 다시 하고, 자산 forward 와 `pred_vertices` 가 최대 0.001 mm 로 같은 것을 먼저 확인했다.

**합격선: 제품 경로(A) 모든 사람의 정점 오차 중앙값 ≤ 5 mm, p95 ≤ 15 mm.** 근거: 내보내기는 HMR 자체 오차(단안 MPJPE 수십 mm)에 비해 무시할 만해야 하고(그 10% 이하),
15 mm 는 손가락 한 마디 굵기 정도로 손·발이 메시에서 어긋나 보이기 시작하는 크기다. 계약 반올림(소수 넷째 자리, 1 m 팔 끝 0.1 mm)과 float32 저장으로 생기는 몫은 0.1 mm 수준이라 여유가 크다.

**판정: 합격.** 69명 모두 A 중앙값 0.010~0.063 mm, p95 0.030~0.119 mm, 최대 0.123 mm(관절 위치 최대 0.122 mm). 반올림하지 않으면 0.001 mm.

사진별 요약(사람별 값의 최대, mm. A = .glb + 계약 포즈, 나머지는 원인 분리용 변형):

| 사진 | 사람 | A 중앙값 | A p95 | A 최대 | pred_global_rots 경로 p95 | flexible 평균 고정 p95 | 보정 형상 켰다면 p95 |
|---|---|---|---|---|---|---|---|
| cam_A_01 | 3 | 0.032 | 0.055 | 0.060 | 0.1 | 20.2 | 18.1 |
| cam_A_02 | 2 | 0.045 | 0.060 | 0.063 | 0.1 | 19.3 | 18.2 |
| cam_A_03 | 3 | 0.044 | 0.089 | 0.093 | 0.1 | 19.3 | 18.0 |
| cam_A_04 | 2 | 0.063 | 0.084 | 0.097 | 0.1 | 19.4 | 17.9 |
| cam_A_05 | 3 | 0.058 | 0.119 | 0.123 | 0.1 | 18.2 | 18.2 |
| cam_B_01 | 5 | 0.050 | 0.090 | 0.095 | 38.9 | 37.6 | 17.1 |
| cam_B_02 | 4 | 0.043 | 0.099 | 0.106 | 45.6 | 42.9 | 17.8 |
| cam_B_03 | 4 | 0.044 | 0.085 | 0.091 | 34.2 | 37.5 | 17.5 |
| cam_B_04 | 4 | 0.043 | 0.074 | 0.081 | 45.5 | 46.2 | 17.7 |
| cam_B_05 | 4 | 0.041 | 0.065 | 0.071 | 38.4 | 40.0 | 17.4 |
| cam_C_01 | 4 | 0.039 | 0.065 | 0.071 | 0.1 | 37.4 | 17.6 |
| cam_C_02 | 5 | 0.039 | 0.071 | 0.087 | 0.1 | 34.7 | 17.7 |
| cam_C_03 | 6 | 0.035 | 0.098 | 0.104 | 0.1 | 38.1 | 17.5 |
| cam_C_04 | 5 | 0.042 | 0.076 | 0.079 | 78.1 | 41.8 | 17.8 |
| cam_C_05 | 5 | 0.056 | 0.073 | 0.079 | 70.0 | 35.0 | 18.7 |
| cam_D_01 | 2 | 0.052 | 0.084 | 0.091 | 0.1 | 49.5 | 17.5 |
| cam_D_02 | 2 | 0.032 | 0.073 | 0.081 | 0.1 | 33.2 | 17.3 |
| cam_D_03 | 2 | 0.039 | 0.077 | 0.084 | 0.1 | 49.8 | 17.1 |
| cam_D_04 | 2 | 0.044 | 0.070 | 0.081 | 0.1 | 47.3 | 16.8 |
| cam_D_05 | 2 | 0.026 | 0.045 | 0.049 | 0.0 | 39.1 | 17.4 |

변형별 해석(69명 p95 범위):

- **pred_global_rots 경로**: 손 IK 가 바뀐 7명만 34~78 mm(손·손목), 나머지는 A 와 같다. 위 '포즈 변환 두 경로'의 추정기 출력 문제다. `pose_from_model_params` 로 피한다.
- **flexible 0 으로 굽기**: 23~89 mm. `*_flexible` 이동을 버리면 다리·팔 길이가 틀린다. 굽기가 필수인 근거다.
- **flexible 을 69명 평균으로 굽기**: 8~50 mm. 사람 사이 차이(leg_length_flexible 표준편차 0.29 × 관절당 5 cm 등)라 같은 사람의 프레임 사이 변동보다 크게 잡힌 비관 추정이다.
  자산을 30 프레임 평균으로 고정하면 오차는 그 사람의 프레임 간 변동만큼이고, 영상으로 재야 한다(-227, 잔여 위험 1).
- **보정 형상(pose correctives)을 켰다면**: 14~19 mm(최대 31~40 mm, 관절 주변 살 모양). glTF 는 자세에 따른 비선형 보정 형상을 실을 수 없다.
  스트림 실행 설정(opt 프리셋)은 `MHR_NO_CORRECTIVES=1` 이라 지금은 차이가 없다. 공식 설정(보정 켬)으로 바꾸면 이만큼 HMR 메시와 달라진다.

결과 파일은 커밋하지 않는다: `verify_report.json`(사람별 전체 값), 샘플 `.glb`, 포즈 시퀀스 JSON(아래).

## 샘플 산출물과 확인 페이지

- `mhr_cam_B_01_p0.glb`: cam_B_01 의 사람 0 체형. `avatar_pose_sequence.json`: avatar_pose_batch 메시지 21개(바인드 자세 + 사진 20장의 각 사람 0, 200 ms 간격 = 5 Hz).
  서로 다른 사람의 포즈를 한 체형에 입힌 것이고 시간 연속이 아니다. `root_rotation` 은 수평 카메라 가정(보정 R 아님, `calibration_version` "sample-level-camera")이라
  내려다보는 카메라의 사람은 앞뒤로 기울어 보인다. 메시지마다 계약 스키마(avatar_pose_batch 1.0.0)와 스키마 밖 규칙(길이 127, 관절 0 = 0, 회전각 ≤ π)을 통과한다.
- `mhr_pose_viewer.html`: 브라우저로 열고 두 파일을 고르면 재생한다(관절마다 slerp, null 은 바인드 회전, pelvis 를 바닥 위 1 m 에 둠). 같은 폴더를 `python -m http.server` 로 띄우면
  `?glb=<파일>&poses=<파일>&t=<초>` 로 바로 열린다. 헤드리스 Chrome 으로 바인드·앉은 자세 프레임을 그려 확인했다.

## 인계

### -227 (워커 송신·업로드)

```python
from simai_ai.hmr.rig import load_mhr_rig, bake_person_rig, scales_from_scale_params
from simai_ai.hmr.gltf_export import build_skinned_glb
from simai_ai.hmr.pose import pose_from_model_params, world_root_rotation, mhr_pose

rig = load_mhr_rig(path)                                   # 기동 때 한 번(sha256 확인, 약 1 s)
# 체형 고정(30 프레임): shape 45·scale 68·flexible 의 평균. scale 은 mhr_model_params[136:204] 또는 scales_from_scale_params(scale_params, head_pose.scale_mean, head_pose.scale_comps)
person = bake_person_rig(rig, shape_mean, scales_mean, body_pose_mean)   # body_pose_mean = mhr_model_params[6:136] 평균(*_flexible 만 쓰임)
glb = build_skinned_glb(person)        # glb.data → 업로드, glb.sha256·size_bytes·vertex_count·face_count → avatar_asset.mesh
# 프레임마다
hp = pose_from_model_params(rig, mhr_model_params, hands_estimated=(mode == "full"))
pose = mhr_pose(world_root_rotation(hp.camera_from_rig, R_world_to_camera), hp.joint_rotations)  # → person_pose.pose
```

- `avatar_asset.skeleton`: `joint_count` 127, `joint_parents_sha256` = `glb.joint_parents_sha256`, `pelvis_joint_index` 1, `rig_source` = `rig.rig_source`.
- `mhr_model_params` 는 추정기 결과에 없으므로 워커에서 `pose_output["mhr"]["mhr_model_params"]` 를 꺼낸다. 없으면 `pose_from_hmr` 를 쓰되 full 모드 손은 위 문제가 있다.
- `R_world_to_camera` 는 그 포즈에 싣는 `calibration_version` 의 R 이다. 손가락 null 규칙은 `hands_estimated` 로 정한다.
- `*_flexible` 프레임 간 변동과 지연은 영상으로 재고, 크면 자산을 새 `appearance_version` 으로 다시 굽거나 Lead 에 계약 보강을 요청한다(잔여 위험 1).

### -65 (Three.js 제품 표시)

- `GLTFLoader` 로 연 SkinnedMesh 의 `skeleton.bones[i]` = MHR 관절 i. 받은 `joint_rotations[i]` 를 `bones[i].quaternion` 에 넣고, null 이면 불러올 때 저장한 바인드 쿼터니언을 둔다.
  관절 0·1 은 늘 `[0, 0, 0]` 이 온다(몸 방향은 root).
- 감싼 그룹 회전 = `quat(A)·quat(root_rotation)`, 위치는 행렬 갱신 뒤 pelvis 뼈(`bones[1]`)의 world 위치를 Tier 0 `pelvis_xyz`(Three 변환 뒤)로 옮기는 이동. 노드 이동·크기는 glTF 값 그대로 둔다(체형).
- `mesh.frustumCulled = false` 권장(바인드 경계 구로 잘림 방지). 텍스처가 없으면 기본 재질. `_MHR_VERTEX_INDEX` 속성은 그리기에 쓰지 않는다.
- 확인 페이지 `ai/experiments/hmr/mhr_pose_viewer.html` 의 `apply()` 가 같은 계산의 최소 예다.

## 명세에 없어 정한 수치

| 값 | 근거 |
|---|---|
| 합격선 중앙값 5 mm·p95 15 mm | 위 '검증'. HMR 오차의 10% 이하, 손가락 마디 굵기 |
| UV v → 1 − v | MHR UV 를 FBX·OBJ 규약(아래 왼쪽 원점)으로 보고 glTF(위 왼쪽)로. 텍스처가 없어 확인하지 못했다(잔여 위험 3) |
| 기본 재질 회색 0.7 | 관측 외형이 아님을 드러내는 무채색 |
| 확인 페이지 pelvis 높이 1 m | 위치를 싣지 않아 보기 위한 고정값(제품은 Tier 0) |

## 버린 대안

- **체형을 정점에 미리 굽고 역바인드를 사람 휴지 자세로 바꾸기:** 표준적인 바인드지만 관절마다 다른 체형 변환을 가중 평균한 정점은 원래 LBS 와 같지 않다. 노드 TRS + 템플릿 역바인드가 정확하다.
- **pelvis 회전을 관절 1 에 남기기:** 계약의 '몸 방향 = root_rotation, 똑바로 선 사람 = Rz(ψ)·Rx(+π/2)' 와 어긋나 -227 의 World 회전이 몸 방향 없이 카메라 → World 만 담게 된다.
- **`pred_global_rots` 만 쓰기:** 추정기 출력에서 손 IK 뒤 갱신되지 않는다(위). `mhr_model_params` 경로를 권장으로 했다.
- **보정 형상을 morph target 으로:** 보정은 관절 6D 특징 → 3000 은닉 → 정점 변위의 비선형 망이라 고정 morph target 몇 개로 정확히 실을 수 없고, 가중치 스트림은 계약 변경이다. 스트림은 보정 꺼짐 설정을 쓴다.
- **trimesh·pygltflib 로 쓰기:** 잠금에 없거나(Windows) Linux 전용 extra 에만 있다. 필요한 기능(스킨·응용 속성·결정적 바이트)이 작아 직접 쓴다.
- **UV 없이 MHR 정점 수(18,439) 유지:** -89 텍스처 누적이 UV 를 쓴다. `include_uv=False` 로는 고를 수 있다.

## 잔여 위험

1. **`*_flexible` 뼈 길이 고정.** 자산은 길이를 고정하고 스트림은 회전만 싣는다. 사람 사이 차이로 잡은 비관 추정은 p95 8~50 mm 이고, 같은 사람의 프레임 간 변동은 영상이 없어 재지 못했다.
   크면 다리·팔 끝이 수 cm 어긋나 보인다(위치 정본은 Tier 0 라 판정 영향은 없음). 대응 후보: 30 프레임 평균 고정(기본), 변동이 클 때 새 `appearance_version`, 또는 계약에 길이 파라미터 추가(Lead 결정).
2. **보정 형상.** 스트림 설정을 공식(보정 켬)으로 바꾸면 glTF 와 HMR 메시가 p95 약 17 mm 다르다. 표시 품질 문제이고, 끄면 관절 주변 살이 단순 LBS 모양이 된다.
3. **UV 방향 미확인.** v 뒤집기는 규약 추정이다. -89 가 첫 텍스처를 입힐 때 확인한다(틀리면 `gltf_export.py` 한 줄).
4. **자산 판.** 관절 순서·이름·스킨 4개 제한·관절 0 무가중치·관절 1 몸 방향은 이 자산(sha256 `352e271a…`)에서 확인했다. 다른 판은 `load_mhr_rig` 가 sha256 으로 막고, 바꿀 때 실자산 시험을 다시 돌린다.
5. **얼굴.** 메시의 얼굴은 MHR 템플릿 형상에 정체 blendshape 를 더한 것이고 표정은 0 이다(생성형 복원 아님). 텍스처가 없어 외형 정보는 없다.

## 계약·문서와 다른 점 (Lead 결정 요청)

1. **관절 이름이 자산에 있다.** `avatar-pose-stream.md` '포즈 표현'·선행 PoC `COMPATIBILITY.md` 는 "관절 이름이 자산에 없어 인덱스가 기준" 이라 적었지만 이 자산은 `get_joint_names()` 로
   127개 이름(0 `body_world`, 1 `root`, 42 `r_wrist`, 43 `r_pinky0` …)을 준다. 인덱스 기준은 그대로이고 glTF 노드 이름에 넣었다. 손가락 범위 43~64·79~100 은 이름으로도 맞다.
2. **관절 1 회전이 늘 0.** 계약은 관절 0 만 0 이라고 적었다. MHR 은 몸 방향을 관절 1 에 걸어 root 로 옮긴 결과 관절 1 도 늘 `[0, 0, 0]` 이다. 계약 위반은 아니고, 뷰어가 알면 된다.
3. **`avatar_asset.mesh.vertex_count`.** 예시는 18,439(MHR 정점)인데 UV 를 넣은 glTF 의 POSITION 은 19,455 다. 파일로 확인할 수 있는 glTF 값(`SkinnedGlb.vertex_count`)을 권하고,
   MHR 정점 수는 `extras.simai.mhr_vertex_count` 에 있다. 어느 쪽인지 계약 설명에 적어 달라.
4. **`rig_source` 예시 문자열**("… @ sam-3d-body@b5c765a")은 코드 저장소 커밋이고 자산은 HF `facebook/sam-3d-body-dinov3@11aaa34` 다. 코드는 후자로 적는다(`MHR_RIG_SOURCE`).
5. **`avatar-pose-stream.md` '넘긴 일'** 의 -227 몫 중 "MHR → 스키닝 glTF 내보내기", "127 관절 순서·손가락 범위 공식 자산 확인" 은 이 티켓이 했다.
