# 3D 맵(GLB) 시각 보강 설계 (S15P31A101-481)

관련 명세: 5.3(정적 3D 복원·시각 자산), 규칙 1·7(시각용은 Tier 0 값·위험 판정에 쓰지 않는다).
코드: `ai/src/simai_ai/scene/glb_enhance.py`, 연결 `reconstruct_tasks.py`(`enhance_visual_glb`·bake·preview·fallback), `reconstruct.py`(metrics·notes). 시험: `ai/tests/scene/test_glb_enhance.py`.

## 하는 일
texrecon 으로 굽은 `textured.glb` 는 면의 약 46%가 미관측 회색(`unobserved_neutral`)이고 가려진 바닥은 메시가 없어 검게 뚫린다. 보기만 좋게 두 가지를 더한다.

- (A) 바닥 평면: 관측 바닥 정점(z < 0.08 m)의 xy 1~99 분위 사각형, z = (z 5분위 − 0.02 m), 관측 바닥 텍셀 중앙색 단색. 기존 메시 아래에 깔려 겹침이 없다.
- (B) 미관측 면 색: 미관측 정점마다 반경 0.35 m 안 관측 정점 최근접 6개의 역거리 가중 평균색을 `COLOR_0` 으로 넣고 재질을 정점색 사용(baseColorFactor 1)으로 바꾼 사본으로 교체. 35 cm 밖은 회색 유지.
- (C) 작은 부유 조각 제거는 벽에 구멍이 나서 기각(넣지 않음).

## 결정과 근거
- trimesh 없이 glTF JSON/BIN 을 직접 읽고 BIN 뒤에 덧붙인다. Windows tier0 extra 에 trimesh 가 없고 Pod 이미지에 Blender 가 없다. 관측 메시·텍스처 바이트·좌표는 바이트 그대로(시험이 접두 동일을 단언).
- World→glTF `(x, z, −y)` 는 `texture_mesh.to_glb_points` 한 곳만 쓴다(규칙 3).
- 채택 검사: 보정 카메라(K·R·t)와 사람 지운 배경 사진이 있으면 정점·면 투영 래스터라이저(NumPy)로 원본·보강본을 카메라 시점에 그려 마스크 밖 화소 MAE 를 비교한다. 평균이 `check_tolerance_mean`(0.002), 카메라 한 대가 `check_tolerance_camera`(0.005) 넘게 나빠지면 원본 유지. 메시 없는 화소는 뷰어 바탕색(검정)으로 본다. 입력이 없거나 쓸 카메라가 없으면 메시 지표(빈 바닥 비율·미관측 회색 면적이 늘지 않고 줄어듦)만으로 정하고 `check.mode=MESH_METRICS_ONLY` 로 남긴다.
- 연결은 별도 stage 를 만들지 않고 `bake`·`preview`·`fallback` 끝에서 호출한다(기존 시험이 stage 순서를 고정). 맵 재생성도 같은 StaticReconstructor 경로라 자동 적용. 실패·시간 초과·불채택이어도 단계를 실패시키지 않고 원본을 둔다. 스위치 `SIMAI_GLB_ENHANCE=0` 또는 작업 옵션 `enhance:false`(기본 켬).
- 바닥 평면은 출처 지도에 없는 추정 면이라 `observed_surface_ratio`·SURFACE_SOURCE_MAP 은 건드리지 않고 `extras.simai.enhanced.estimated_floor_plane.surface_source=ESTIMATED_PLANE` 으로 표시. 보강된 GLB 는 `ALREADY_ENHANCED` 로 다시 처리하지 않는다. 삼각형 수는 바닥 적용 시 +2.

## 기록
`bake.json`/`preview.json` 의 `enhance`(요약+detail) → `metrics.json` 의 `enhance`. `quality.notes` 에 `glb_enhance=applied(모드)` 또는 `skipped:사유`. glTF `extras.simai.enhanced`.
미결: scene_output.quality 는 `additionalProperties:false` 라 구조화 `quality.enhance` 를 못 넣는다. 필요하면 scene_output 마이너 버전에서 추가(Lead 결정).

## regen-6 로비 실측 (카메라 A·C, 사진 긴 변 2304 → 렌더 640)
| 지표 | 전 | 후 |
|---|---|---|
| 검사 MAE 평균 | 0.1696 | 0.1364 (A 0.1387→0.1082, C 0.2005→0.1646) |
| 빈 바닥 비율(위에서 본 5 cm 격자) | 0.2244 | 0.0038 (63.65 m² 평면, 14.04 m² 채움) |
| 미관측 회색 면적 | 62.04 m² | 0.75 m² (면 18.4만 중 1828) |
소요 약 17 초, GLB 18.4 → 19.9 MB.

## 잔여 위험
- 검사는 단순 래스터(조명·그림자 없음)라 절대 점수보다 전후 차이만 쓴다. 카메라 2대만 사진이 있어 실제 파이프라인(4대)에서는 더 안정적.
- 정점색은 정점 해상도라 큰 미관측 면은 번진 색으로 보인다(주변 색 추정, 사실이 아님).
- 압축 GLB(meshopt/draco/KTX2)는 보강하지 않는다(압축 전 단계에 둔다).

---

# 4카메라 장면 GLB 3D 물체 복원 설계 (S15P31A101-515)

관련 명세: 5.3(정적 3D 복원·시각 자산), 규칙 1·3·7(시각용은 Tier 0 값·위험 판정에 쓰지 않으며 관측으로 승격하지 않는다).
코드: `ai/src/simai_ai/scene/object_completion.py`, `ai/src/simai_ai/scene/texture_provenance.py`(`GENERATED = 4`).
실험·GPU 스크립트: `ai/experiments/object_completion/verify_session12_notebook.py`, `ai/experiments/object_completion/gpu/install_3dfixer.sh`, `ai/experiments/object_completion/gpu/run_3dfixer_s12.sh`.
시험: `ai/tests/scene/test_object_completion.py`.

## 하는 일
4대 고정 카메라 TSDF·texrecon으로 만든 정적 배경 GLB에서 바닥·벽 평면을 뺀 나머지 메시를 연결 성분과 높이·색상으로 묶어 물체 후보(`traffic_cone`, `signboard`, `general`)로 분리하고, 카메라 뒤에 가려 뚫린 뒷면·옆면·바닥 지지부를 제자리(World 좌표계)에서 완성해 단일 GLB 안에 물체별 노드로 합친다.

1. **물체 후보 분리 및 2D 마스크**:
   - `-511`(`glb_fill`)과 호환되는 바닥 높이(`z0`) 및 수직 벽 평면 순차 RANSAC으로 바닥·벽·천장 면을 제거한다.
   - 주황색 원뿔 시드(`traffic_cone`), 수평 단면 PCA 기반 얇은 평판(`signboard`), 일반 입체 물체(`general`)로 후보를 분리한다.
   - 4대 보정 카메라(`K·R·t`, 왜곡 보정 배경 사진)에 장면 전체 z-버퍼와 함께 투영해 가림을 반영한 2D 마스크(`masks_2d`)와 최적 시점(`best_camera_id`)을 고른다(`SIMAI_OBJECT_MASK_BACKEND=projection|color_refined|sam`).
2. **완성 백엔드 인터페이스 (`CompletionBackend`)**:
   - 입력(`CompletionInput`): 최적 시점 사진·2D 마스크·4대 카메라 내외부 파라미터·관측 부분 점(`observed_points_world`)과 경계 마스크(`points_mask`: 중심 1.0, 외곽 15% 0.5)·관측 메시 조각.
   - 출력(`CompletionOutput`): World 좌표계(`+Z` 상방, 바닥 `Z=0`, 미터) 완성 정점·면·UV·텍스처·면/텍셀 출처 지도.
   - 구현 백엔드:
     - `ThreeDFixerBackend` (`3d_fixer`): 1순위 GPU 백엔드(`HorizonRobotics/3D-Fixer`, CVPR 2026, TRELLIS 기반). 무거운 의존성(`torch`, `threeDFixer`)은 함수 안에서만 지연 import 하며 GPU VRAM 최소 기준(기본 20 GB, 공식 요구 24 GB) 미달 시 즉시 건너뛴다.
     - `ConvexHullCompletionBackend` (`convex_hull`): 노트북·단위 시험용 가짜 백엔드. 관측 표면 조각(`OBSERVED=2`)은 그대로 보존하고 미관측 뒷면·바닥 지지부는 관측 점 + 바닥 투영점의 3D 볼록 껍질(`GENERATED=4`)과 가림 고려 다중 시점 사진 투영으로 제자리 완성한다.
   - 정형 도형 템플릿:
     - **라바콘(`traffic_cone`)**: 높이 `67.5 cm`(`0.675 m`), 밑면 한 변 `37 cm`(`0.37 m`) 정사각형(모서리 둥글림 무시), 꼭대기 지름 `4.5 cm`(`0.045 m`). 관측 전면 점들이 원뿔 옆면 반지름 $r(z)$에 맞도록 중심축과 밑판 방향을 정합하고 관측 사진을 투영한다.
     - **안내판(`signboard`)**: 관측 점(또는 `SignboardConfig`)에서 폭·높이·주축 방향을 맞춘 평판 도형에 관측 사진을 투영한다.

## 배치·검증 기준 (`ValidationThresholds`)
완성 메시는 관측 부분 점에 대해 수평 스케일·이동 제한 ICP(`align_completed_mesh`)로 정합하고 바닥(`Z=0`) 접촉을 보정한 뒤 아래 5개 지표를 계산한다. **하나라도 기준 미달이면 완성 메시를 버리고 원래 메시를 그대로 유지한다.**

| 지표 | 기본 기준 | 근거 |
|---|---|---|
| 4대 카메라 재투영 실루엣 IoU (`min_reprojection_iou`) | `>= 0.40` (유효/최상 시점) | 부분 관측 조각의 2D 마스크 대비 완성 물체 실루엣이 크게 어긋나거나 엉뚱한 위치에 생성되는 현상 차단 |
| 관측 점 → 완성 메시 표면 거리 중앙값 (`max_obs_to_mesh_median_m`) | `<= 0.05 m` (정형 도형 `0.075 m`) | TSDF 복셀(4 cm) 수준 이내로 관측 표면을 충실히 따르는지 확인 |
| 관측 점 → 완성 메시 표면 거리 p95 (`max_obs_to_mesh_p95_m`) | `<= 0.16 m` (정형 도형 `0.24 m`) | TSDF 절단 거리(16 cm) 밖으로 튀는 이상치 차단 |
| 바닥 접촉 오차 (`max_floor_contact_error_m`) | `<= 0.03 m` (`|z_min - floor_z|`) | 공중에 뜨거나 바닥 아래로 파고드는 아티팩트 방지 |
| 물체당 삼각형 수 / 용량 / 시간 | `<= 20,000 면` / `<= 5 MB` / `<= 60 초` | 뷰어 렌더링 부하 및 GLB 용량 폭증 방지 |

## 출처 기록 (`surface_source`) 및 비활성화 스위치
- `-514`와 동일한 `texture_provenance.py` 출처 체계를 사용한다: `UNOBSERVED = 0`, `INTERPOLATED = 1`, `OBSERVED = 2`, `ESTIMATED_PLANE = 3`, `GENERATED = 4`.
- 복원·생성된 면과 텍셀은 반드시 `GENERATED = 4`로 기록하며 관측(`OBSERVED = 2`)으로 승격하지 않는다(규칙 7).
- 각 물체 노드의 `extras.simai`에 `safety_use: false`, `geometry_role: "VISUAL"`, `surface_source: "GENERATED"`, `face_source_ratios_area`, `source_map_image`(텍셀별 출처 PNG 이미지 인덱스), `alignment`, `validation`을 기록하고, 최상위 `extras.simai.object_completion`에 전체 요약을 남긴다.
- 비활성화 스위치: `SIMAI_GLB_OBJECTS=off`(`0`, `false`, `no`, `disabled`) 또는 옵션 `{"objects": False}` 이면 물체 완성을 건너뛰고 원본 GLB를 유지한다.

## GPU Pod (H100) 실행 방법 및 3D-Fixer 입력 주의사항
노트북에서 만든 12차 로비 입력 번들(`C:/Users/SSAFY/simai-local/object-completion/s12_3dfixer_bundle.tar.gz`)을 GPU Pod(Linux, CUDA >= 11.8, VRAM >= 24 GB)에 복사한 뒤 아래 두 스크립트로 한 번에 설치·실행한다.

```bash
# 1. HorizonRobotics/3D-Fixer 설치 및 Hugging Face 가중치 다운로드
bash ai/experiments/object_completion/gpu/install_3dfixer.sh

# 2. 12차 로비 번들로 3D-Fixer 실행 -> 완성 GLB 및 리포트 생성
bash ai/experiments/object_completion/gpu/run_3dfixer_s12.sh /workspace/s12_3dfixer_bundle.tar.gz /workspace/s12_3dfixer_run
```

- **3D-Fixer 공식 저장소 입력 규약 주의사항**: `threeDFixer/pipelines/threeD_fixer.py`의 `project_uv`는 `utils3d.torch.project_cv(pts, torch.inverse(extrinsics), intrinsics)`를 호출한 뒤 `[0, 1]` UV를 `[-1, 1]`로 변환해 `grid_sample`에 전달한다. 따라서 `extrinsics`는 OpenCV `cam2world` 4×4 행렬(`R_c2w = R.T`, `t_c2w = -R.T @ t`)이어야 하고, `intrinsics`는 이미지 해상도 `[W, H]`로 정규화된 3×3 행렬(`fx/W, fy/H, cx/W, cy/H`)이어야 한다(`inference_ours_testset.py`의 `intrinsics_from_fov_xy`와 일치; 공식 `app.py` 데모의 비정규화 픽셀 `K` 전달은 업스트림 데모 스크립트의 불일치 사항이므로 `ThreeDFixerBackend.prepare_conditioning_tensors`에서 정규화 `K`를 생성하도록 구현함).

## Pixal3D를 뒤로 미룬 까닭
- `Pixal3D`(`LookUpJh/Pixal3D`, 물리 제약 아모달 3D 복원)는 논문만 공개된 상태이며 모델 가중치와 추론 코드가 아직 공개되지 않았다(저장소에 Coming Soon 안내만 존재).
- 따라서 공개 가중치(`HorizonRobotics/3D-Fixer`)와 추론 파이프라인(`ThreeDFixerPipeline`)이 즉시 사용 가능한 `3D-Fixer`를 1순위 GPU 백엔드로 채택하고, `CompletionBackend` 프로토콜을 통해 추후 `Pixal3D` 코드·가중치 공개 시 어댑터만 추가해 교체할 수 있도록 인터페이스를 분리했다.

## S15P31A101-511: 늘어난 면 제거 + 바닥·벽 평면 채우기 (`glb_fill.py`, enhance 1.1.0)
관련 명세 5.3. 코드: `ai/src/simai_ai/scene/glb_fill.py`(진입 `run_fill`), 연결 `glb_enhance.enhance_glb`·`enhance_file`, 시험 `ai/tests/scene/test_glb_fill.py`, 근거·실측 `ai/experiments/texrecon_opt/REPORT.md`(-505 PoC 를 운영화).
**범위 결정(Lead 11-05)**: 맵은 평면 사진 투영 + AI 인페인팅 + 물체 AI 3D 로 가고 texrecon 은 뺀다. texrecon 설정 비교·관제 중 부하 제한(B)은 중단했고 `texture_mesh`/`bake` 기본값은 그대로다.

### 하는 일(보정 카메라·사진이 있을 때만, 기본 켬, `SIMAI_GLB_FILL=off` / `options.fill=false` / `EnhanceConfig.fill_enabled`)
1. 늘어난 면 제거: 보이는 면 중 보이는 모든 카메라에서 |cos(법선, 시선)| < `fill_cos_slant`(0.15)인 면을 지운다(색인만 새로 씀, 정점·텍스처 바이트 불변). 안 보인 면은 지우지 않는다. 긴 변 기준은 기본 끔(`fill_long_edge_m`=0; 단순화된 평평한 큰 면과 구분 안 됨).
2. 바닥: World Z=0(바닥 중앙값이 ±8 cm 안일 때, 밖이면 중앙값). 영역 = 메시 바닥 칸 닫힘(0.6 m) ∪ (카메라 광선이 가림 없이 Z=0 에 닿는 칸 ∩ 메시 바닥에서 2.5 m 안) → 구멍 메움 → 채택된 벽 평면 안쪽으로 자름. 메시가 이미 있는 칸은 건드리지 않고 구멍에만 5 cm 격자 면.
3. 벽: 수직 면 순차 RANSAC. 통과 조건(전부): 길이≥1.5 m·넓이≥3 m², 지배 방향 직교(±8°, 방향은 ±2° 안 면의 넓이 가중으로 다듬음), 높이 구간 5/8 이상 점유·높이 1.5 m 이상, 방 경계(바닥이 평면 반대쪽에 8 % 초과면 방 안 구조물 → 기각), 중복 병합. 평면의 u 범위(지지 정점 0.5~99.5 분위)·높이 안의 구멍만 채우고 평면이 없는 방향은 만들지 않는다. 구멍 비율 85 % 초과(지지 부족)면 건너뜀.
4. 채택 검사·실패: 기존 `run_check`(카메라 MAE) 그대로. 채움 단계의 `EnhanceSkip` 은 채움만 건너뛰고 나머지 보강을 계속, 그 밖의 예외·불채택은 원본 GLB 유지(`enhance_file` 은 파일을 안 바꾼다). 채움이 바닥을 대신하면 옛 바닥 사각형(A)은 `REPLACED_BY_FILL` 로 건너뛴다. 카메라가 없으면 채움 없이 옛 동작(`NO_CAMERAS`).

### 색·출처(규칙 2·7)
칸 중심(1 cm 텍셀)을 보정 카메라에 투영. 보임 = 영상 안·사람 마스크 밖·|cos|≥0.15·그 점 앞에 메시 없음(깊이 버퍼). `consistent`(기본): 메시가 깊이로 확인되는 시선이 있거나 보이는 카메라 둘 이상이고 색 최대 채널 차이 ≤55 → 사진 색 + 출처 `ESTIMATED_PLANE`(3); 그 밖은 회색(128) `UNOBSERVED`(0). `strict`(확인된 시선만), `lenient`(하나만 보여도, 가구 번짐 위험)은 `fill_color_mode`. 색을 지어내거나 번지게 하지 않는다. 시각용(`safety_use:false`), 위험 판정·`observed_surface_ratio`·SURFACE_SOURCE_MAP 에 쓰지 않는다. 축 변환은 `to_glb_points` 한 곳.
GLB: 채움 노드 `estimated_{floor|wall_k}_fill`(텍스처 재질, doubleSided) + extras.simai(`surface_source`, `source_map_image`, `texel_ratios`). `extras.simai.enhanced.fill` 에 단계·삭제 면·비율. report/`bake.json enhance` 에 `fill_ratio`(ESTIMATED_PLANE/UNOBSERVED 면적 비율), `fill_area_m2`, `fill_share_of_surface`, `stretched_removed_*`, `triangles_delta`(assets.triangle_count 보정).

### 다음 티켓(AI 인페인팅) 인터페이스: 평면별 이미지 사이드카
`enhance_file` 이 GLB 옆 `<이름>_fill/` 에 저장(`bake.json auxiliary` 에도 등록): 평면마다 `<role>_albedo.png`(사진 투영 + 회색 자리표시, RGB), `<role>_inpaint_mask.png`(255 = 채울 곳: 채움 칸 안이면서 사진 근거 없음, 0 = 건드리지 말 것), `<role>_surface_source.png`(0/3), 그리고 `fill_atlas.json`(`format simai.fill_atlas`). role = `floor`, `wall_<k>`. 한 변 4096 px 상한, 텍셀 ≈ 1 cm(상한 넘으면 굵어짐).
- 텍셀 (px, py) → World: `origin + (px+0.5)·texel_m·u_axis + (py+0.5)·texel_m·v_axis`(`frame`: origin, u_axis, v_axis, texel_m, size_px; World m, +Z 상방). 행 0 = 위(glTF UV 원점 왼쪽 위). 바닥 u=+X, v=+Y; 벽 u=수평 접선, v=+Z.
- 인페인팅은 mask 밖 픽셀을 바꾸면 안 된다(관측 보존). 채운 결과는 출처를 `GENERATED`(4, 값 자리만 예약)로 구분해야 하고 이 단계는 그 값을 내지 않는다. GLB 에 되돌려 쓰는 것은 다음 티켓이 정한다(여기선 GLB 의 알베도 이미지를 사이드카와 같은 UV 로 교체하면 된다).

### 정한 수치와 근거
cos 0.15(-505 + 스윕: 0.25 는 먼 바닥이 지워짐, 0.15 는 0.6 %), 닫힘 0.6 m·벽 허용 6 cm·직교 8°(-505 한 방 값), 광선 도달 2.5 m(명세에 없어 정함: -505 에서 닫힘 밖 가장자리가 남던 문제를 벽 클립과 함께 풀기 위한 값, 눈 확인 필요), 색 일치 55/255(카메라 노출 이득 ±20 % 흡수), 가시성 렌더 긴 변 1152(4대 9 s). 버린 대안: 긴 변 기준 기본 삭제(평평한 큰 면 오삭제), lenient 기본(가구 번짐), 벽 범위 ±10 cm 여유(끝에 가짜 띠).
### 잔여 위험·넘긴 일
벽 없는 방향의 바닥 과대, 개구부(문·유리창) 메움, Z=0 대 메시 바닥 계단, 카메라가 적으면 늘어난 면 오삭제(한 대 빼기 22~25 %), 인페인팅 결과 되돌려 쓰기와 `GENERATED` 출처는 다음 티켓.
