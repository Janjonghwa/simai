# EXP-01 텍스처 메시: 합친 배경 메시 + 관측 텍스처 + 텍셀 출처 지도 (설계·실험 스크립트)

S15P31A101-298. 기획서 §5.3(정적 3D, 복원 후처리 순서 "뷰 간 깊이 정합 → 이상점 제거 → 표면 생성 → 메시 단순화 → G4 → glTF",
텍스처는 확장), 부록 C.9("EXP-01 텍스처 메시: MVP 의 점군 → 단순 메시 glTF 를 텍스처 메시로 올린다"), §20.2(EXP-01, 관측된 형상과
추정된 공간 구분), §20.3(EXP-02 표면 출처 지도 0·1·2), AGENTS 규칙 2·3·7·8 기준이다.
코드는 `ai/experiments/static_scene/texture_mesh.py`(명령 하나)·`texture_provenance.py`(numpy 부품), 시험은
`ai/tests/experiments/static_scene/test_texture_mesh.py`(합성, Open3D·OpenCV·Docker 없음), 굽기 도구 이미지는 `deploy/texrecon/Dockerfile`.
깊이 정합·융합 규칙은 `docs/design/exp01-hybrid-depth.md`(-283)의 것을 그대로 가져다 쓴다(`run_hybrid_depth.py` 함수 재사용).
S15P31A101-314 가 형상 정리(카메라 간 깊이 정렬·수평 평면 정리, `mesh_cleanup.py`, 시험 `test_mesh_cleanup.py`)와 glb 의 glTF 축 규약을
더했다(아래 "형상 정리(-314)", "좌표").
명세·계약·잠금(`ai/uv.lock`)은 바꾸지 않았다(아래 "넘긴 일").

## 무엇을 내나

명령 하나가 MapAnything Run 폴더(+ prepared 입력)에서 다음을 낸다. 노트북 CPU 만 쓴다(GPU 없음).

| 산출물 | 내용 |
|---|---|
| `bake-<항>/all/textured.glb` | 하나로 합친 배경 메시. 관측·보간 면은 아틀라스마다 텍스처 메시(`textured_NNNN`), 미관측 면은 텍스처 없는 중립 회색 메시(`unobserved_neutral`, RGB 128). 최상위 `extras.simai` 에 좌표·출처 값·비율·지도 파일 이름 |
| `bake-<항>/all/albedo_NNNN.png` | 관측 텍스처 아틀라스(texrecon 출력 사본, glb 에도 들어 있다) |
| `bake-<항>/all/surface_source_NNNN.png` | 텍셀 출처 지도. 무손실 8비트 단일 채널, 아틀라스와 같은 UV·해상도. 2 = OBSERVED, 1 = INTERPOLATED, 0 = UNOBSERVED |
| `bake-<항>/all/observed_camera_NNNN.png` | 관측 카메라 지도. 값 = `extras.simai.surface_source.camera_ids` 번호 + 1, 0 = 관측 아님·모름 |
| `metrics.json` | 입력 해시, 형상·거르기·구멍 판정·출처 비율, 한 카메라 빼고 굽기 지표, 시간, 경고 |
| `run_record.json` | `contracts/experiments/run_record.schema.json` 2.2.0 (검증 통과해야 쓴다) |
| `compare/*.jpg` | (a) 빼고 굽기 렌더 vs 사진, (b) 기존 `gridmesh.glb`(정점 색) vs 텍스처 메시 같은 시점 그림, `geometry-<카메라>.jpg`(-314: 사진·형상 음영·텍스처·출처(파랑 = 추정 평면)) |

결과 폴더는 저장소 밖(예: `C:/Users/SSAFY/simai-local/texture-298/`)에 둔다. 사람이 찍힌 사진·렌더가 들어 있어 커밋하지 않는다.

## 구성

### 0층: 형상과 텍스처 소스 전처리

1. **카메라별 깊이**: 기본은 MapAnything `raw.npz`(모델 해상도 518×294, 신뢰도 하위 10%·사람 마스크 뺌, `world_frame.inputs.load_mapanything_run`
   과 같은 규칙). `--depth` 를 주면 `run_hybrid_depth predict` 결과(DA3 등, 원본 해상도)를 MapAnything 깊이에 정합(`register_to_reference`,
   축척)해서 쓴다. 정합 실패 카메라는 형상에 넣지 않는다(모르면 비운다).
2. **떠 있는 점 제거**: `run_hybrid_depth.edge_sharpness` 의 규칙(3×3 폭 > 10%, 이웃 최소·최대에서 둘 다 2% 넘게 떨어진 중간값).
   MapAnything 은 `mask_edges` 로 경계를 이미 지워 w1 에서는 0 이었다.
3. **시점 간 깊이 비교**: `run_hybrid_depth.visibility_filter` 와 같은 규칙(다른 카메라가 본 표면보다 5% 넘게 앞인 경우가 3% 안 지지보다
   많으면 뺀다)을 픽셀 마스크로 적용. w1 에서 카메라별 1.5~2.3% 를 뺐다.
4. **하나의 표면**: Open3D(MIT) `ScalableTSDFVolume`(칸 2 cm, 절단 16 cm) → 마칭 큐브 → 겹친 면·비다양체 변 정리 → 200 면 미만 떨어진 조각
   제거 → 이차 오차 단순화(목표 40만 면) → 같은 정점 세 개의 면 하나로. 형상 출처는 모두 관측 깊이다(형상 구멍은 채우지 않는다).
5. **텍스처 소스 거르기**(카메라·면 쌍마다, 면의 점 7개 = 무게중심·꼭짓점·변 중점): 영상 밖·뒤 → **사람 마스크**(prepared 의 YOLO26-seg
   15 px 팽창 마스크, 점 하나라도 닿으면 뺀다) → **깊이 경계 띠**(깊이 점프 또는 깊이 모델이 스스로 지운 픽셀과 닿은 픽셀을 원본 5 px
   만큼 넓힌 띠) → **가림**(이 카메라 깊이가 면 무게중심보다 5% 넘게 앞 = 메시에 없는 가림체. 이 카메라 깊이가 무효면 가림으로 보지 않고
   texrecon 의 광선 가시성 검사에 맡긴다).
   `--texture-images` 로 빈 교실 정지 사진(같은 펴진 크기)을 주면 그 그림을 텍스처 소스로 쓴다. 마스크가 없으면 `--texture-empty-room`
   으로 빈 교실이라고 선언해야 한다(선언 없이 마스크가 없으면 거절).

### 1층: 관측 텍스처 (mvs-texturing, Waechter 2014, BSD-3)

texrecon 을 세 번 부른다(Docker CPU 컨테이너 하나, 아래 "Docker").

1. **비용**: `-d <항> --skip_*_seam_leveling --skip_hole_filling` 로 데이터 비용 파일 `tex_data_costs.spt` 만 얻는다(texrecon 의 75° 시선각 컷,
   BVH 광선 가시성 검사, 투영 면적 또는 GMI).
2. **시점 선택**: 위 0층 5 의 거르기를 비용 파일에서 (면, 시점) 항목을 지우는 것으로 적용하고 `-D` 로 넣는다(사용자 마스크를 C++ 수정 없이
   연결하는 방법. texrecon 의 유효성 마스크는 영상 모서리에서 이어진 검은 영역만 빼므로 영상 안 사람을 검게 칠해도 후보에서 빠지지 않는다).
   출력 `_labeling.vec`(면마다 0 = 미관측, k = k−1 번째 카메라).
3. **최종 굽기**: 관측 면 + 2층 기준을 통과한 작은 구멍 면만 남긴 메시와 그 라벨(`-L`)로 전역·국소 이음매 보정(국소는 Poisson 혼합으로 관측
   패치 색도 조금 바뀐다)과 구멍 채우기를 돈다. 큰 미관측 면은 넣지 않으므로 texrecon 이 채울 수 없다.

데이터 항은 `area`(투영 면적)와 `gmi`(Sobel 그래디언트 적분)를 둘 다 굽고 한 카메라 빼고 굽기로 비교한다. 사진 일관성 이상치 제거(`-o`)는
쓰지 않는다(기본 none. 켜도 인라이어 시점 4개 미만이면 멈춰 카메라 4대에서는 사실상 동작하지 않는다 → 사람은 0층 마스크로 뺀다).

### 2층: 작은 구멍 보간

라벨 0 면의 연결 성분(변 공유)마다 판정한다. 모두 만족해야 INTERPOLATED 후보다(`texture_provenance.HoleRule`).

| 기준 | 값 | 근거 |
|---|---|---|
| 둘러싸임 | 경계 변이 모두 관측 면과 맞닿음(메시 테두리에 닿으면 탈락) | 보간은 둘레 관측 색으로만 한다. 둘레가 없으면 근거가 없다 |
| 면적 | ≤ 25 cm² | TSDF 칸 2 cm 의 약 2.5×2.5 칸. 재구성 틈(양쪽 색을 아는 곳) 크기 |

미터 기준(면적·경계까지 거리)은 메시 단위에 축척 s 를 곱해 잰다(면적 s², 길이 s). `--world-alignment` 가 OK 면 s = det(M[:3,:3])^(1/3)(World 닮음 변환의 축척)이고, 아니면 s = 1 이라 단위가 'MapAnything 모델 축척(미터 아님)' 이며 `metrics.json` geometry·`extras.simai.unit`·`holes.json` `unit_scale_m` 에 그렇게 적는다. 픽셀 반경은 단위가 없어 축척과 무관하다. TSDF 칸·절단(`voxel_m`·`trunc_m`)은 메시 단위 그대로다.
| 경계까지 최대 거리(내접 반경 근사) | ≤ 2.5 cm (폭 약 5 cm) | 좁고 긴 결손을 면적만으로 통과시키지 않게. 점 = 성분 정점·면 무게중심, 경계 = 경계 변 선분 |
| 둘레 관측 카메라의 원본 픽셀 반경 | ≤ 8 px (가장 큰 값) | 사람 마스크 팽창 15 px·국소 이음매 띠 20 px 보다 작은 틈. 멀리 있는 곳은 미터 기준보다 이 기준이 먼저 걸린다 |
| 정점 수 | ≤ 100 | texrecon `MAX_HOLE_NUM_FACES`(이름과 달리 구멍 성분 정점 수와 비교) |

texrecon 의 구멍 채우기는 성공 여부를 따로 알려 주지 않는다. 최종 OBJ 에 그 면이 실제로 들어갔는지(입력 정점 번호 세 개로 면을 되짚는다)
로 판정하고, 못 채운 작은 구멍은 UNOBSERVED 로 둔다. 문헌에서 검증된 값이 아니라 실험 시작값이다(넘긴 일: 관측 영역을 일부러 가려 보간
오차를 재는 시험).

### 큰 미관측: 중립 기본 재질로 비운다

기준을 넘는 라벨 0 면은 텍스처 없이 `unobserved_neutral` 메시(RGB 128, metallic 0, roughness 1)로 낸다. 같은 평면 텍스처 복사(3a)와
확산 inpainting(3b)은 만들지 않는다(AGENTS 규칙 2·7: 관측 근거가 없는 색을 지어내지 않는다. 관제 기본 화면에서 사각지대가 그대로 보인다).

### 텍셀 출처 지도

- 값은 EXP-02 아바타 표면 출처 지도(`avatar_asset.surface_source.mask_uri`)와 같다: **2 = OBSERVED, 1 = INTERPOLATED, 0 = UNOBSERVED**.
  배경에는 얼굴 비트(+128)가 없다. **3 = ESTIMATED_PLANE** 은 `--plane-cleanup`(-314) 일 때만 낸다: 색은 그 카메라 관측 픽셀이지만 정점을
  맞춘 수평 평면에 올린 면이다(형상이 관측 그대로가 아니다, AGENTS 규칙 7). **4 = GENERATED 는 값 자리만 예약**하고 내지 않는다
  (`extras.simai.surface_source.reserved_not_emitted`). EXP-02 처럼 0/1/2 만 읽는 쪽은 3·4 를 0(미관측)으로 읽어야 한다(관측으로 승격 금지).
  계약 enum(`common/defs surface_source`)에는 3·4 가 없다(넘긴 일).
- 면 출처를 아틀라스 UV 에 칠한다(텍셀 중심이 삼각형 안). **겹치는 텍셀은 신뢰가 가장 낮은 값**을 남긴다. 신뢰 순서는 숫자 순서가 아니라
  UNOBSERVED < GENERATED < INTERPOLATED < ESTIMATED_PLANE < OBSERVED 다(`texture_provenance.TRUST`. 추정 평면은 색이 관측이라 보간보다 위,
  형상을 맞췄으니 관측보다 아래). 관측 카메라 지도는 OBSERVED·ESTIMATED_PLANE 텍셀에 값이 있다.
  읽는 쪽은 최근접 표본으로 읽고 밉맵·선형 보간을 하지 않는다(값이 섞이면 의미가 없다).
- 관측 카메라는 별도 지도(`observed_camera_NNNN.png`)로 낸다. 출처 값을 EXP-02 와 같게 두려고 비트를 섞지 않았다. 겹친 텍셀의 카메라가
  다르면 0(모름). 국소 이음매 보정을 거친 색이라 원본 픽셀 값 그대로는 아니다(처리 이력은 `metrics.json` texrecon 호출 목록).
- 미관측 면은 아틀라스에 없으므로 **비율은 표면적 기준**(`ratios_area`, `scene_output.quality.observed_surface_ratio` 와 같은 뜻)을
  주 지표로, 텍셀 기준(`ratios_texel`, 칠한 텍셀 안)은 참고로 낸다.
- 별도 텍스처 + glTF `extras` 로 낸 이유: 계약에 배경 glTF 출처 필드가 없고(`scene_output` 은 자산 목록·`observed_surface_ratio` 만),
  EXP-02 가 이미 "아틀라스와 같은 UV 의 8비트 PNG" 형식을 쓴다. glTF 확장(`extensionsUsed`)을 새로 만들면 뷰어가 모르는 확장을 받아야 한다.

### 좌표 (AGENTS 3, 계약 `scene_output` assets.axis_convention)

계산은 MapAnything world(OpenCV cam2world)에서 한다. `--world-alignment`(world_frame `fix` 결과)가 status OK 면 glb 정점에만 그 닮음 변환을
적용해 World(m, 오른손, +Z 상방, 바닥 Z=0)로 옮긴 뒤 **glTF 규약(+Y 상방, World (x, y, z) → (x, z, −y))으로 바꿔 낸다**
(`extras.simai.axis_convention = GLTF_Y_UP`, `metrics.json` `output_frame.glb_axis_convention`, Run 기록 `glb_axis_convention`).
계약 `contracts/experiments/scene_output.schema.json` 의 `assets.axis_convention` 이 GLTF_VISUAL 을 GLTF_Y_UP 으로 내고 "변환은 내보내기
어댑터 한 곳에서만" 하라고 정하고 있고, 관제 앱(-59 `SceneBackground.tsx`)은 받은 glTF 를 Three 축으로 보고 회전을 더하지 않는다. 그 한 곳이
`texture_mesh.to_glb_points` 다(정점 위치만 바꾸고 UV·출처 지도·`metrics.json` 의 지표 좌표는 World 그대로, (x, z, −y) 는 회전이라 면 감김
방향도 그대로). 없거나 FAILED 면 MapAnything world 그대로 내고 `extras.simai.frame = MAPANYTHING_WORLD`,
`axis_convention = MAPANYTHING_WORLD_OPENCV(NOT_WORLD)` 로 적는다(World·Scene 으로 쓰지 않는다). 기존 `gridmesh.glb` 는 노드 변환으로
OpenCV → OpenGL 뒤집기(diag(1, −1, −1))를 싣고 있어, 비교 렌더는 노드 변환을 빼고 기하 정점(MapAnything world)을 쓴다.

정정(-314): -298 은 glb 를 World(+Z 상방) 그대로 내고 "(x, z, −y) 변환은 뷰어 어댑터 한 곳" 이라고 적었는데, -298 리뷰 때 계약의
`axis_convention`(GLTF_VISUAL 은 GLTF_Y_UP) 을 확인하지 않고 AGENTS 규칙 3 만 보고 통과시킨 잘못이다. 내보내기 어댑터가 그 한 곳이고,
-314 부터 glb 는 GLTF_Y_UP 이다. 그 전에 만든 `texture-298/*/textured.glb`(World +Z 상방)는 관제 앱에 그대로 올리면 누워 보인다.

### 확인 (개발 교실 관측 기반, DATASET_GT 아님)

- (a) **한 카메라 빼고 굽기**: 그 카메라의 비용 항목을 모두 지운 뒤 시점 선택·최종 굽기를 다시 하고, 그 카메라 시점(펴진 K, 원본 해상도)에서
  Open3D 광선 투사로 그려 실제 사진과 비교한다. 비교 픽셀 = 메시에 맞음 ∧ 사람 마스크 밖 ∧ 렌더 출처 OBSERVED(다른 카메라 관측).
  PSNR(RGB), SSIM(밝기 BT.601, 7×7 상자 창, 창 전체가 비교 영역 안인 픽셀 평균), `coverage`(사람 밖·메시에 맞은 픽셀 중 비교 픽셀 비율).
  텍스처만 뺐고 형상(TSDF)은 네 카메라로 만들었다(형상까지 빼면 그 카메라만 본 곳이 사라져 비교 영역이 줄어든다).
- (b) 기존 `gridmesh.glb`(카메라별 격자 메시 4개, 정점 색, 사람 포함)와 텍스처 메시(첫 데이터 항)를 같은 카메라 시점에서 그린 그림.
- (c) 출처 비율(표면적·텍셀), 카메라별 관측 표면 비율, 구멍 성분 수·작은 구멍·채운 면·못 채운 면.
- **자기 시점 재현**(기준선): 네 카메라로 구운 텍스처를 각 카메라 시점에서 그려 그 카메라가 고른 면만 비교. 굽기·재표본·이음매 보정 손실의 상한이고, 빼고 굽기와의 차이가 카메라 사이 정합 오차 몫이다. 빼고 굽기에는 두 그림을 비교 영역 안에서만 σ 8 px 정규화 합성곱으로 흐린 뒤 PSNR(`psnr_db_blur8`)도 낸다(위치 어긋남과 색 차이를 가르는 보조. 비교 영역 밖 색이 경계에서 섞이지 않는다).
- 형상 지표 `depth_coverage`: 카메라마다 쓴 깊이 픽셀 중 메시를 그 시점에서 그린 깊이가 5% 안인 비율(합친 메시가 입력 표면을 잃지 않았는지).

### Run 기록 지표 (`run_record.metrics`)

`mesh_faces`, `mesh_vertices`, `mesh_area_m2`, `output_frame`, `<항>_area_ratio_{observed,interpolated,unobserved}`,
`<항>_texel_ratio_{…}`, `<항>_glb_triangles`, `<항>_interpolated_faces`, `loo_<항>_psnr_db_mean`, `loo_<항>_ssim_mean`,
`loo_<항>_coverage_mean`, `loo_<항>_psnr_db_blur8_mean`, `self_<항>_psnr_db_mean`, `self_<항>_ssim_mean`, `loo_<항>_<카메라>_psnr_db`, `loo_<항>_<카메라>_ssim`, `geometry_s`, `bake_<항>_s`, `loo_s`, `total_s`.
-314: `glb_axis_convention`, `<항>_area_ratio_estimated_plane`·`<항>_texel_ratio_estimated_plane`, `cross_view_{before,after}_{abs_rel_p50,abs_rel_p90,within_1pct,p2p_m_p50,p2p_m_p90}`,
`align_mode`, `align_reference`, `align_converged`, `align_iterations`, `align_<카메라>_{status,scale,rot_deg,center_shift}`, `planes`, `plane_faces`,
`region_<이름>_{layer_spread_m_p50,rms_m,within_1cm,rough_area_ratio,top_tilt_gt10_ratio,top_tilt_mean_deg}`. Jira 키는 `--jira-key`(기본 -298).
`experiment_state` ITERATE, `gpu.provider` LOCAL(CPU), `env_hash` = texrecon 이미지 ID, `experiment_id` EXP-01, `e_gate` 없음.

### Docker

`deploy/texrecon/Dockerfile`: 우분투 22.04(다이제스트 고정)에서 mvs-texturing @f337429 를 빌드한다. 상위 `elibs/CMakeLists.txt` 가 mve(기본
브랜치)·rayint(cuda 브랜치)를 커밋 없이 받으므로 빌드 전에 `GIT_TAG` 를 커밋으로 박는다(mve @91c0f3f, rayint @b62c890, mapmap @fa526e0 은
상위 고정, Eigen 3.3.2 는 MD5 고정). 결과 이미지 약 35 MB, 비루트 사용자, 진입점 `texrecon`.
이 노트북 Docker Desktop 은 Windows 폴더 붙이기(`-v`)가 멈춰서(사진·결과 폴더 모두, 2026-10-27), 실행기는 컨테이너 하나를 `--network none`
으로 띄워 두고 `docker cp` 로 입력(장면·메시·비용·라벨)을 넣고 결과 폴더를 꺼낸다. Linux 에서 직접 빌드한 실행 파일은 `--texrecon <경로>`.

## 실행

```sh
# 이미지(인터넷 443, GPU 없음, 한 번)
docker build -f deploy/texrecon/Dockerfile -t simai/texrecon:dev .      # 또는 docker buildx bake texrecon
# 3차 세션 w1
uv run --project ai --with open3d==0.19.0 --with opencv-python-headless --with trimesh --with pillow \
  python ai/experiments/static_scene/texture_mesh.py \
  --ma-run ai/experiments/runs/gpu-session1/20261026-s3/runs/exp01/CAL102_w1_map_anything_known_k_20261026-s3 \
  --prepared ai/experiments/recordings/mapanything-inputs/classroom4-prepared-20261026 \
  --out C:/Users/SSAFY/simai-local/texture-298/w1-full
```

Open3D 는 0.19.0 으로 고정한다. 0.20.0 Windows 휠의 `ScalableTSDFVolume` 은 평면 하나도 면 0 개를 내서 스크립트가 거절한다.
의존성은 `uv run --with` 로만 더하고 `ai/uv.lock` 은 바꾸지 않았다(실험 단계, 채택 때 `scene` extra 로 옮긴다).

### 4차 결과로 바꿀 때

| 바뀌는 입력 | 명령 |
|---|---|
| 다른 해상도 MapAnything Run(-282, 1036·1554·원본) | `--ma-run <그 Run 폴더>` (같은 prepared 로 돈 Run 이어야 한다. `metrics.json` 의 `prepared_sha256` 확인) |
| DA3 혼합 깊이(-283, `run_hybrid_depth predict` 출력) | `--ma-run <자세를 준 MapAnything Run> --depth <깊이 폴더> [--align scale]` (깊이 폴더의 `cameras_json_sha256` 이 그 Run 과 같아야 한다) |
| World 고정 성공(world_frame `fix` status OK, 실측 정물 -100 등) | `--world-alignment <fix 결과 JSON>` (같은 `cameras.json` 해시) |
| 빈 교실 정지 사진 | `--texture-images <폴더: cam_A.jpg… (+ cam_A.mask.png…)>` (+ 마스크가 없으면 `--texture-empty-room`) |
| 면 수·칸·띠·구멍 기준 | `--target-faces`, `--voxel`, `--edge-band-px`, `--hole-max-area`, `--hole-max-inradius`, `--hole-max-inradius-px` |
| 데이터 항·빼고 굽기 | `--terms area,gmi`(첫 항이 비교 그림 기본), `--loo-terms ""`(빼고 굽기 생략) |
| 형상 정리(-314) | `--align-views sim3`(또는 `scale`, 기본 `off`), `--plane-cleanup`(World 정렬 필요), `--check-region 이름=x0,x1,y0,y1,z0,z1`(여러 번) |

## 정한 수치

| 수치 | 값 | 근거 |
|---|---|---|
| TSDF 칸 | 2 cm | MapAnything 모델 해상도 픽셀이 3 m 에서 약 1.1 cm, 10 m 에서 3.8 cm. 3 cm(절단 18 cm)는 w1 깊이 덮음이 2 cm·16 cm 보다 4~5%p 낮았다 |
| TSDF 절단 | 16 cm (8 칸) | w1 시점 간 깊이 차 \|Δd\|/d p50 1.0~1.9% (5 m 에서 5~10 cm). 8 cm 는 다른 카메라가 서로의 표면을 깎아 덮음이 1.5~3%p 낮았다(16 cm: 0.90/0.89/0.86/0.84) |
| 최대 깊이 | 15 m | 교실 대각선(약 14 m) 밖은 넣지 않는다 |
| 작은 조각 | 200 면 미만 제거 | 떠 있는 점 잔여 |
| 목표 면 수 | 40만 | texrecon 세 번 합쳐 약 30 s, glb 17 MB. 관측 비율은 면 수에 거의 무관했다(같은 설정 10만 면 OBSERVED 56.2%, 40만 55.2%, 리뷰 수정 뒤 smoke). 처음 smoke1 의 43% 는 경계 띠 설정(10 px·신뢰도 구멍도 경계) 탓이었다. 40만은 형상 세부(책상 모서리 등)를 남기려는 값이고 이 비교로 정한 값은 아니다 |
| 깊이 경계 띠 | 원본 5 px(깊이 격자로 올림, 모델 해상도 2 px) | smoke1 설정은 후보의 55% 를 뺐다(10만 면). 신뢰도 하위로 뺀 픽셀은 깊이 경계가 아니라 이웃으로 보지 않는다. 지금도 후보의 28~33% 를 뺀다(가장 큰 거르기, 아래 결과) |
| 가림 허용 | 5% | `run_hybrid_depth` 충돌 기준과 같다 |
| 작은 구멍 | 위 2층 표 | 실험 시작값 |
| 미관측 재질 | RGB 128 회색, 텍스처 없음 | 관측 텍스처와 구분되고 어둡지 않은 중립 |

## 결과 (w1, 2026-10-27, 노트북 CPU 22 스레드)

입력 `CAL102_w1_map_anything_known_k_20261026-s3`(MapAnything 518, known-K) + `classroom4-prepared-20261026`(사람 마스크 15 px 팽창,
카메라별 약 15.6%). world-fix FAILED 라 **MapAnything world** 출력이다. Run `exp01-tex-w1-20261027`(아래 "실행" 명령, 기본값, 커밋 97e9ada,
깨끗한 트리. 결과 `C:/Users/SSAFY/simai-local/texture-298/w1-run3/`).

| 항목 | 값 |
|---|---|
| 시간 | 전체 7.1 분(형상 32 s, 굽기 area·gmi 각 47~49 s, 빼고 굽기 8회 243 s). texrecon 한 번 8~11 s |
| 형상 | TSDF 87.6만 면 → 40만 면·21.8만 정점, 표면 113 m². 시점 간 충돌로 뺀 픽셀 1.5~2.3%, 떠 있는 점 0(MapAnything 이 경계를 이미 지움) |
| 깊이 덮음(5% 안) | A 0.90, B 0.89, C 0.86, D 0.84 |
| 텍스처 거르기(area, 후보 대비) | 사람 3.9~4.7%, 깊이 경계 띠 27.6~32.9%, 가림 0.1~0.2% |
| 출처 비율 area(표면적) | OBSERVED 0.552, INTERPOLATED 0.020, UNOBSERVED 0.427 (카메라별 관측 A 0.131, B 0.163, C 0.134, D 0.125) |
| 출처 비율 gmi(표면적) | OBSERVED 0.509, INTERPOLATED 0.021, UNOBSERVED 0.470 |
| 텍셀 비율(칠한 텍셀) | area OBSERVED 0.962 / INTERPOLATED 0.038, gmi 0.956 / 0.044 |
| 구멍(area) | 관측 없는 성분 8,346, 둘러싸임 6,932, 작은 구멍 6,388 성분(14,379 면) 중 texrecon 이 채운 면 9,775, 못 채워 미관측 4,604 |
| glb(area) | 40만 삼각형, 텍스처 메시 25개 + 중립 1개, 17.3 MB |
| 자기 시점 재현(area / gmi) | PSNR 22.7 / 23.5 dB, SSIM 0.967 / 0.967 |
| 빼고 굽기 area | PSNR A 12.2, B 12.6, C 12.1, D 11.7 dB(평균 12.2, σ 8 px 정규화 흐림 16.2), SSIM 평균 0.437, 비교 영역 coverage 0.28~0.45(평균 0.365) |
| 빼고 굽기 gmi | PSNR 평균 12.1 dB(정규화 흐림 16.1), SSIM 0.410, coverage 0.355 |

흐린 PSNR: Run 기록(`metrics.json`·`run_record.json`)의 16.4(area)·16.3(gmi)은 고치기 전 방식(비교 영역 밖 색이 마스크 경계에서 섞임)이다. 리뷰 수정 뒤 저장된 빼고 굽기 결과(라벨·OBJ·sub.ply)에서 정규화 흐림으로 다시 계산한 값이 16.2·16.1 이다(`w1-run3/loo_blur8_recomputed.json`, 같은 재구성으로 PSNR 은 기록과 소수 셋째 자리까지 같다). 섞임이 일치를 낮출 것이라는 예상과 달리 0.2 dB 낮아졌다(경계 밖 색이 두 그림을 비슷하게 만든 쪽이었다). 결론(흐리면 약 +4 dB = 위치 어긋남이 주원인)은 그대로다. Run 기록은 덮어쓰지 않았다.

판단:
- **굽기 자체는 제대로 된다**: 자기 시점 재현 22~24 dB·SSIM 0.97(재표본·이음매 보정 손실). 기존 `gridmesh.glb` 보다 같은 시점에서 무늬가
  선명하고(조끼·바닥 전선) 사람이 배경에 섞이지 않는다(`compare/baseline-*.jpg`).
- **다른 카메라 텍스처의 정합은 나쁘다**: 빼고 굽기 12 dB. 색 이득·편차를 최소제곱으로 맞춰도 +0.5~1 dB 뿐이고 흐리면 +4 dB(σ 8)~+7 dB(σ 32)
  오른다(노트북 진단) → 색 차이가 아니라 **수십 px 위치 어긋남**이 주 원인이다. 같은 입력의 MapAnything 카메라 간 왕복 재투영이 원본 px
  중앙값 9~18, p95 78~105 px(`world_frame.inputs.cross_view_reprojection`)라 맞는다. 텍스처 품질의 다음 병목은 굽기가 아니라 깊이·자세(4차
  해상도·DA3 혼합 결과로 다시 굽기)다.
- **area vs gmi**: area 가 관측 표면이 4%p 많고 빼고 굽기 PSNR·SSIM 도 조금 낫다(0.1 dB, SSIM 0.027). gmi 는 자기 시점 재현만 0.8 dB 높다.
  기본은 area 로 둔다. 차이가 작아 4차 입력에서 다시 본다.
- 미관측 42.7%: 우리 거르기 중 가장 큰 몫은 깊이 경계 띠(후보의 28~33%)다. texrecon 단계(75° 시선각 컷·광선 가시성)의 몫은 따로 재지
  않았다. 사람 뒤 바닥처럼 어느 카메라도 깊이를 못 낸 곳은 형상 자체가 없다(렌더에서 검게 빈다, 미관측 비율에 들지 않는다).
- texrecon 기본 스레드(전부)는 실행마다 텍셀 비율이 4째 자리에서 달랐다(0.9621 / 0.96206). 표면적 비율은 같았다.

## 형상 정리 (S15P31A101-314)

로비(MapAnything r518 + World 고정 `wf-lobby.json`, -298 도구) 텍스처 메시가 바닥·벽은 좋지만 책상 윗면·소파·의자가 거칠었다(뾰족함·구멍·
겹친 면). 원인은 카메라 4대 깊이가 서로 1~2%(5 m 에서 5~10 cm) 어긋난 채 TSDF 로 평균돼 얇은 수평판이 여러 겹으로 섞이는 것이다(-298
`TRUNC_VOXELS` 근거와 같은 값). 코드는 `ai/experiments/static_scene/mesh_cleanup.py`(numpy), 연결은 `texture_mesh.geometry`, 시험은
`ai/tests/experiments/static_scene/test_mesh_cleanup.py`(합성 방). 가구 옆·아래(못 본 면)는 범위 밖이고 지어내지 않는다.

### 1. 카메라 간 깊이 정렬 (`--align-views sim3|scale`, 기본 off, TSDF 전)

- 카메라마다 카메라 중심 기준 닮음 변환(회전 ω, 이동 t, 축척 σ; `scale` 은 σ 만)을 둔다. 점 X → c + t + (1+σ)·R(ω)(X − c) 는
  "깊이 ×(1+σ), 자세 회전 R·R_cam, 중심 c + t" 와 같다. 그래서 보정은 그 카메라의 **깊이 배율 + 자세**로 남고, 텍스처도 보정된 자세로
  투영한다(`scene/*.cam`, 자기 깊이를 자기 광선 위에서만 바꾼다). 보정된 자세는 이 보기용 메시에만 쓰고 보정 결과(cameras.json·Tier 0)에
  되쓰지 않는다(AGENTS 규칙 1).
- 기준 카메라는 고정(기본: 다른 카메라와 대응이 가장 많은 카메라. 로비는 cam_A). 나머지를 **투영 대응 점-평면 거리 / 깊이**(상대)로 함께
  맞춘다: i 의 격자 표본(카메라당 3만)을 j 에 투영해 가장 가까운 픽셀의 j 점·법선과 짝(상대 깊이 차 ≤ 5%, 두 법선 ≤ 30°), Gauss-Newton
  (1차 근사, Huber 1%).
- **수렴**: 한 번 갱신에 Huber 비용 평균이 1% 넘게 줄지 않으면 멈추고 가장 좋은 상태를 쓴다(또는 |Δσ|·|Δω|·|Δt|/중앙 깊이 < 1e-5, 최대 30회).
  처음에는 매개변수 변화로만 멈췄는데, 로비는 겹치는 면 대부분이 바닥·벽이라 평면을 따라 미끄러지는 방향이 약하게 정해져 비용은
  그대로(0.0012)인데 자세가 30회 동안 계속 흘러갔다(회전 1.9°까지) → 비용 기준으로 바꿨다(로비 4회에 멈춤).
- **상한**(넘으면 그 카메라는 원래대로 두고 대응에서도 빼고 나머지로 다시 푼다, 모르면 비운다): 축척 |s − 1| ≤ 5%(시점 간 |Δd|/d p95 약
  7%), 회전 ≤ 3°(같은 입력에서 DA3 가 다시 추정한 자세와 MapAnything 자세 차 최대 1.5° 의 두 배), 중심 이동 ≤ 그 카메라 중앙 깊이의
  10%(같은 비교의 중심 차 최대 6%). 대응이 500 개 미만이어도 맞추지 않는다.
- 지표(`metrics.json` `cleanup.cross_view_{before,after}`): 짝마다·전체 |Δd|/d 분포(같이 보임 20% 안)와 1·2·5% 안 비율, 점-평면 거리(m),
  `cleanup.alignment`(기준·상태·누적 보정·반복 기록).
- `scale`(깊이 배율만)은 로비 r518 에서 |Δd|/d p50 0.82 → 0.71% 뿐이었다(탐색 실행). 어긋남이 균일한 배율이 아니라 자세(회전 1~2°)
  쪽이라 비교 실행에서는 뺐다. 옵션으로는 남겼다(자세를 바꾸지 않아야 할 때).

### 2. 수평 평면 정리 (`--plane-cleanup`, 기본 off, World 정렬 필요)

1. 찾기(World, 정렬된 깊이 점 전부): 법선이 위(+Z)와 20° 안인 점만 후보(카메라 쪽 법선이라 천장·덕트 아랫면은 빠진다). **1점 RANSAC**
   (수평 사전: 가설 = 그 점 높이의 수평면, 점 수 상위 10 가설을 발자국 칸 수로 다시 셈) → 안쪽(3 cm) 최소제곱 재적합 3회(기울기 > 5° 면
   수평으로 둔다) → XY 5 cm 칸 8-연결 **군집**마다 재적합해 받는다: 기울기 ≤ 5°, 잔차 RMS ≤ 1.5 cm, 발자국 ≥ 0.1 m². 이미 받은 평면과
   발자국이 30% 넘게 겹치고 높이 차가 6 cm(2·안쪽) 안이면 같은 표면의 어긋난 겹으로 보고 받지 않는다(바닥 위 3~4 cm 조각을 따로 맞추면
   없는 턱이 생긴다. 옆에 붙은 다른 높이 가구는 겹치지 않아 받는다).
2. 깊이 맞추기(TSDF 전): 평면 발자국(1 칸 넓힘) 안·거리 ≤ 3 cm·법선 ≤ 45° 인 픽셀의 깊이를 광선-평면 교점으로(자세 그대로, 자기 광선 위).
   TSDF 가 한 겹을 본다.
3. 메시 맞추기(TSDF 뒤): 같은 조건(정점 법선은 방향 무관)의 **정점**을 평면에 수직 투영하고, 세 정점이 같은 평면에 오른 면을 평면 면으로
   **ESTIMATED_PLANE**(3) 표시한다(색은 관측 그대로, 위 "텍셀 출처 지도"). 처음에는 면 법선 20° 로 면을 골랐는데 2 cm TSDF·40만 면 단순화
   메시는 평평한 곳도 20° 넘게 기운 면이 섞여 평면이 얼룩졌다 → 정점 기준·45° 로 바꿨다.
4. 평면 아닌 물체: 평면에서 3 cm 넘게 떨어졌거나 법선이 45° 넘게 기운 정점은 움직이지 않는다(시험: 0.04 m² 상자 윗면·반구 덮개·12° 기운 판·
   천장은 그대로).
- 정한 수치 근거: 안쪽 3 cm = 정렬 뒤 점-평면 거리 p90 3.9 cm 와 TSDF 칸(2 cm) 사이, RMS 1.5 cm = 바닥(41 m²) RMS 0.7 cm·책상 0.4~1.3 cm
  를 받고 굽은 쿠션을 거르는 값, 넓이 0.1 m² = 로비의 작은 윗면(0.38 m 높이 0.12 m², 스툴로 보임)까지 받는 값, 칸 5 cm = 칸 2.5 개(TSDF 칸 2 cm)에 점 여럿.
  실측 검증 전 시작값이다.

### 확인 지표 (`--check-region 이름=x0,x1,y0,y1,z0,z1`)

- `layers`(정렬 효과, 평면 맞추기 전 입력 깊이): 영역 안 위쪽 법선 점으로 수평면을 찾고(띠 10 cm), 카메라별 부호 거리 중앙값과 그 폭(겹 폭).
- `flatness`(최종 메시): 영역 안 위쪽 면으로 평면(띠 5 cm)을 찾아 띠 안 면 넓이 가중 RMS·1 cm 안 비율, 그리고 평면 가정 없이 윗면 쪽 면
  (기울기 ≤ 60°) 넓이 중 10° 넘게 기운 비율(`top_tilt_gt10_ratio`, 울퉁불퉁·뾰족함). 평면 정리를 켜면 RMS 는 맞춘 면 몫만큼 작아지는 것이
  당연하다(정의상). 정렬 효과는 `layers`·시점 간 거리로 본다.
- 영역: `sofa` = 소파 섬(World x 2.4~4.4, y 0.6~2.9, z 0.2~0.6), `tables` = 방 전체 z 0.6~0.8(둥근 탁자들).
- 눈 확인: `compare/geometry-<카메라>.jpg`(사진·형상 음영(시선과 면 법선 |cos|)·텍스처·출처, 파랑 = 추정 평면).

### 비교 실행 (2026-10-28, 노트북 CPU, texrecon `simai/texrecon:dev`, 굽기 `--terms area --loo-terms ""`)

공통: `--ma-run …/20261028-s4/…/CAL102_w1_map_anything_known_k_r518_20261028-s4lobby --prepared …/lobby4-prepared-20261028
--world-alignment wf-lobby.json --check-region sofa=2.4,4.4,0.6,2.9,0.2,0.6 --check-region tables=0,8,0,10,0.6,0.8 --jira-key S15P31A101-314`.
DA3 깊이는 `--depth …/20261028-s5/…/HYB283_da3_pose_{1008,2304}_20261028-s5lobby --align scale`. 결과 `C:/Users/SSAFY/simai-local/texture-298/lobby314-*`
(커밋하지 않음). 개발 공간 관측 기반 지표(DATASET_GT 아님).

| 결과 폴더(`lobby314-`) | 깊이 | 정렬 | 평면 | 시점 간 \|Δd\|/d p50 | 시점 간 점-평면 p50 | 소파 겹 폭 | 탁자 겹 폭 | 소파 RMS / 기운 윗면 | 탁자 RMS / 기운 윗면 | 자기 시점 PSNR | 관측 / 추정 평면 / 보간 / 미관측(표면적) | 평면 수 | 시간 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `r518-base` | MA r518 | 끔 | 끔 | 0.82% | 2.10 cm | 5.4 cm | 3.9 cm | 2.0 cm / 0.70 | 1.5 cm / 0.77 | 25.1 dB | 65.4 / 0 / 2.0 / 32.6% | − | 163 s |
| `r518-sim3` | MA r518 | sim3 | 끔 | **0.37%** | **0.75 cm** | 2.1 cm | 3.2 cm | 1.9 cm / 0.71 | 2.0 cm / 0.79 | 26.0 dB | 68.7 / 0 / 2.3 / 29.1% | − | 160 s |
| `r518-plane` | MA r518 | 끔 | 켬 | 0.82% | 2.10 cm | 5.4 cm | 3.9 cm | 1.8 cm / 0.29 | 1.7 cm / 0.42 | 26.2 dB | 41.5 / 27.3 / 1.2 / 30.0% | 18 | 163 s |
| **`r518-sim3-plane`** | MA r518 | sim3 | 켬 | **0.37%** | **0.75 cm** | 2.1 cm | 3.2 cm | 1.7 cm / 0.27 | 1.8 cm / 0.41 | 25.4 dB | 42.6 / 31.0 / 1.2 / **25.2%** | 17 | 161 s |
| `da1008-base` | DA3 1008 | 끔 | 끔 | 1.61% | 3.32 cm | 6.9 cm | 5.4 cm | 1.8 cm / 0.44 | 2.2 cm / 0.69 | 23.0 dB | 60.8 / 0 / 0.7 / 38.5% | − | 259 s |
| `da1008-sim3-plane` | DA3 1008 | sim3 | 켬 | 0.62% | 1.12 cm | **1.3 cm** | **2.5 cm** | **1.4 cm / 0.24** | 1.8 cm / 0.44 | 23.4 dB | 42.9 / 26.9 / 0.4 / 29.8% | 19 | 371 s |
| `da2304-base` | DA3 2304 | 끔 | 끔 | 7.47% | 6.03 cm | 4.9 cm | 5.7 cm | 2.2 cm / 0.48 | 2.1 cm / 0.69 | 23.6 dB | 48.7 / 0 / 0.4 / 50.9% | − | 229 s |
| `da2304-sim3-plane` | DA3 2304 | sim3(전부 상한 초과 → 원래대로) | 켬 | 7.47% | 6.03 cm | 4.9 cm | 6.3 cm | 2.4 cm / 0.42 | 2.1 cm / 0.50 | 23.5 dB | 41.2 / 6.2 / 0.4 / 52.2% | 76 | 521 s |

열: 시점 간 거리는 정렬 뒤(끔이면 앞) 전체 짝. 겹 폭 = `layers` 카메라별 높이 중앙값의 최대 − 최소(평면마다, p50). RMS·기운 윗면 = `flatness`
(띠 5 cm RMS, 윗면 쪽 면 중 10° 넘게 기운 넓이 비율). 자기 시점 PSNR 은 관측 색 면(관측 + 추정 평면)만. 정렬 보정(r518, 기준 cam_A, 4회에
비용 수렴, Huber 비용 1.54e-5 → 6.11e-6): cam_B 축척 0.993·회전 1.19°·중심 8.3 cm, cam_C 0.985·1.79°·12.9 cm, cam_D 0.991·1.07°·11.7 cm.
DA3 1008: B 0.991·0.65°·10 cm, C 1.006·1.73°·20 cm, D 1.020·1.00°·17 cm. DA3 2304: 처음 풀이가 회전 4.4~8.7° 라 세 카메라 모두 CAPPED.
평면 정리(r518-sim3-plane): 깊이 픽셀 36~58% 를 평면에 맞춤, 메시 정점 이동 p50 3.4 mm·p95 1.7 cm, 평면 면 15.3만/40만. 바닥 41.4 m²(RMS 0.7 cm),
소파 섬 0.44 m(4.3 m²), 벤치·스툴 0.38~0.50 m, 탁자 0.63~0.73 m.
눈 확인 그림: `C:/Users/SSAFY/simai-local/texture-298/lobby314-compare-sofa.jpg`(cam_C 소파 섬), `lobby314-compare-tables.jpg`(cam_B 탁자),
각 폴더 `compare/geometry-cam_*.jpg`.

판단:

- **정렬(sim3)이 겹의 주원인을 줄인다**: r518 시점 간 |Δd|/d p50 0.82 → 0.37%, 점-평면 2.1 → 0.75 cm, 1% 안 비율 59 → 78%, 소파 섬 겹 폭
  5.4 → 2.1 cm. 관측 표면 65.4 → 68.7%, 미관측 32.6 → 29.1%, 자기 시점 PSNR 25.1 → 26.0 dB(텍스처 정합도 좋아졌다). 다만 정렬만으로는 윗면이
  거칠다(기운 윗면 비율 0.70 그대로, 2 cm TSDF·518 해상도 잡음).
- **평면 정리가 윗면 거칠기를 없앤다**: 기운 윗면 비율 소파 0.70 → 0.27~0.29, 탁자 0.77 → 0.41~0.42(남은 몫은 탁자 테두리·의자). 음영 그림에서
  벤치·탁자·바닥이 평평해지고 뾰족함이 사라졌다. 정렬 없이 켜면 한 카메라만 본 바닥이 3.7 cm 위 평면(1.4 m²)으로 따로 잡혀 턱이 생긴다.
- **DA3 깊이**: 1008 은 형상이 가장 매끈하다(정렬+평면 뒤 소파 겹 1.3 cm, 기운 윗면 0.24) 하지만 자기 시점 PSNR 이 2 dB 낮고(23.4) 미관측이
  더 많다(29.8%). 소파 섬에서는 스툴(0.40~0.45 m)이 벤치 평면(0.42 m)에 함께 눌렸다. 2304 는 MapAnything 깊이와 정합이 나빠(정합 오차 p50
  3~6%, 시점 간 7.5%) 정렬도 상한에서 막혔다 → 쓰지 않는다.
- **추천: r518 + `--align-views sim3 --plane-cleanup`** = `C:/Users/SSAFY/simai-local/texture-298/lobby314-r518-sim3-plane/bake-area/all/textured.glb`
  (GLTF_Y_UP, 16.3 MB, 관측 42.6% + 추정 평면 31.0%, 미관측 25.2% 로 가장 적다). 추정 형상을 넣고 싶지 않으면 `lobby314-r518-sim3`(정렬만,
  추정 평면 0, 관측 68.7%, PSNR 26.0 dB)이 차선이다. 자기 시점 PSNR 은 sim3 26.0, plane 26.2, sim3-plane 25.4 dB 다. 같은 설정 재실행 차는 0.05 dB
  (base 25.11 / 25.06)라 sim3-plane 이 sim3 보다 0.6 dB 낮은 것은 실제 차이다. 원인(평면 정점 이동 ≤ 3 cm 로 사진과 어긋남, 시점 선택 변화)은
  가르지 않았다. 형상(뾰족함·겹)과 미관측이 이 티켓의 문제라 sim3-plane 을 추천하고, 차이는 남은 위험에 적는다.

### 버린 대안 (-314)

- **깊이 배율만(`scale`)·카메라별 배율+오프셋**: 어긋남의 주성분이 자세(1~2°)라 효과가 작다(|Δd|/d p50 0.82 → 0.71%). 옵션으로만 남김.
- **매개변수 변화로만 수렴 판정**: 평면 위 미끄러짐으로 자세가 흘러간다(위).
- **카메라별 ICP 를 기준 카메라 하나에만**: 넷이 함께 겹치는 곳이 적어(카메라 쌍마다 겹이 다르다) 함께 푸는 쪽이 정해짐이 좋다.
- **TSDF 칸·절단 키우기(평균으로 겹 덮기)**: 겹이 남은 채 두꺼워지고 세부가 사라진다(-298 정한 수치).
- **면 법선 기준 메시 맞추기**: 위 3 참고(얼룩).
- **일반 RANSAC(3점, 아무 방향)**: 수평이라는 사전이 있어 1점 가설이 싸고 안정하다. 벽·기운 면은 대상이 아니다.
- **천장 포함(법선 양방향)**: DA3 1008 에서 천장·덕트 높이(2.3~2.7 m)에 평면 29개가 잡혔다. 노출 천장의 덕트·조명을 평평하게 만들면 지어낸
  형상이라 위를 향한 면만 후보로 했다.
- **평면 면 색을 같은 평면의 다른 관측으로 채우기**: 관측 없는 색(규칙 2). 미관측 면은 그대로 회색이다.

### 잔여 위험 (-314)

- 정렬은 MapAnything 자세가 틀린 몫을 깊이 겹침으로 되맞춘 것이다(1~2°, 8~13 cm). World 정렬(`wf-lobby.json`)은 정렬 전 자세로 만들었다.
  기준 카메라(cam_A)는 그대로라 World 틀은 유지되지만, 다른 카메라가 보는 곳의 World 높이는 수 cm 바뀔 수 있다(G4 미측정).
- 평면 군집 안 3 cm 이내 물체(노트북 바닥판, 쿠션 높이 차)는 평면으로 눌린다. 평면 정리만(정렬 없이) 켜면 한 카메라만 본 바닥이 어긋난
  높이(3.7 cm, 1.4 m²)에 따로 평면으로 잡혀 턱이 생겼다 → 정렬과 함께 쓴다.
- DA3 1008 은 바닥 위 3~5 cm 평면(0.86 m² 등)이 남았다(겹 거절 30% 기준 밖). 무엇인지 확인하지 않았다.
- DA3 2304 는 정렬이 상한(회전 4~9°)을 넘어 네 카메라 모두 원래대로 남았다. 입력 깊이 자체가 MapAnything 깊이와 4~6%(p50) 어긋나 이 조합은
  쓰지 않는다.
- ESTIMATED_PLANE 은 계약 enum 에 아직 없다(넘긴 일). EXP-02 처럼 0/1/2 만 읽는 쪽은 0 으로 읽어야 한다.
- 수치는 로비 한 장면·조합마다 한 번 실행(base 만 두 번, 자기 시점 PSNR 차 0.05 dB). 평면 정리를 켜면 정렬만보다 자기 시점 PSNR 이 0.6 dB
  낮다(원인 미확인). 한 카메라 빼고 굽기는 이번에 돌리지 않았다(`--loo-terms ""`).

## 버린 대안

- **OpenMVS TextureMesh**: Windows 바이너리·glTF 직접 출력이 있으나 AGPL. 기본 `close-holes` 는 형상 구멍을 닫는다.
- **Poisson 표면 복원**: 닫힌 표면을 만들어 관측하지 않은 형상을 지어낸다(규칙 2). TSDF 는 관측한 곳만 만든다.
- **카메라별 격자 메시 4개(현 MVP gridmesh)를 그대로 굽기**: 같은 표면이 네 겹이라 z-fighting·중복 면이 생기고 texrecon 이 같은 정점 세 개
  면을 지워 면 번호가 어긋난다.
- **texrecon C++ 수정으로 마스크 연결**: 상위 코드를 고치면 판 올리기가 어렵다. 데이터 비용 파일(`-D`)·라벨 파일(`-L`)로 같은 효과를 낸다.
- **texrecon 내장 구멍 채우기를 그대로**: 기준이 정점 100 개(미터·픽셀 아님)이고 성공 여부를 알려 주지 않는다. 우리 기준으로 통과한 성분만 남겨
  굽고, 실제로 채워졌는지는 출력 면으로 확인한다.
- **사진 일관성 이상치 제거(`-o gauss_*`)로 사람 거르기**: 인라이어 4개 미만이면 멈춘다(카메라 4대).
- **3a 같은 평면 텍스처 복사·3b 확산 inpainting**: 관측 근거 없는 색. 직접 근거 문헌도 없다(조사 자료 A·B).
- **출처 값에 카메라 비트를 섞기**: EXP-02 값(0/1/2)과 달라진다. 별도 지도로 낸다.
- **glTF 확장(`extensionsUsed`) 신설**: 뷰어·계약에 없는 확장. 지금은 `extras` 와 별도 PNG.
- **Open3D 0.20.0**: TSDF 추출이 비어 있다(위).
- **Docker 폴더 붙이기**: 이 노트북에서 멈춘다(위).
- **Open3D `project_images_to_albedo`(MIT)**: 텍셀마다 카메라 가중 평균이라 카메라 간 9~18 px 정합 오차에서 고스팅이 나고, 이음매 보정·구멍 통제·카메라별 출처가 없다. 정합이 1~2 px 로 좋아지면 비교 기준선으로 한 번 볼 가치는 있다.
- **Open3D `color_map_optimization`**: 정점 색이라 메시 밀도만큼만 선명하다(현 gridmesh 계열).
- **pymeshlab 사진 텍스처**: GPL-3, 여러 사진을 섞는 방식(고스팅·출처 없음).
- **nvdiffrast·PyTorch3D·Kaolin 미분 렌더 최적화**: GPU 와 자작 최적화 코드가 필요하다(nvdiffrast·Kaolin 은 NVIDIA 라이선스). 노트북 CPU 목표와 맞지 않는다.
- **texrecon pybind/Cython 바인딩**: 빌드 의존성은 그대로이고, 굽기 약 50 s 중 파일 입출력은 작다. 별도 프로세스가 C++ 충돌을 격리하고 `-D`·`-L` 파일로 필요한 통제가 된다.

## 잔여 위험

- **World 아님**: w1 world-fix 는 FAILED(AXES_AMBIGUOUS, 경계 벽 방향 하나)라 결과가 MapAnything world·모델 추정 축척이다. G4 미측정.
- **K 차이**: 형상은 MapAnything 모델 K(518 폭, fx 262.9 → 원본 환산 약 1169)로 역투영하고 텍스처는 펴진 K(fx 1161)로 투영한다
  (`run_hybrid_depth` 와 같은 선택). 약 0.7% 차이는 영상 가장자리에서 수 px 어긋남이 될 수 있다. 빼고 굽기 지표에 섞여 있다.
- **사람 영역 형상**: 사람 마스크로 뺀 곳 뒤의 바닥을 다른 카메라도 못 봤으면 형상이 없다(렌더에서 검게 빈다, 형상 구멍은 채우지 않음).
  빈 교실 사진이 없으면 남는다.
- **카메라 노출 차이**: 전역 이음매 보정만 쓴다(사전 이득 정규화 없음). 빼고 굽기 PSNR 에 노출 차가 섞인다.
- **결정성**: texrecon 기본 스레드(전부)는 실행마다 텍셀 비율이 조금 달랐다. `--num-threads 1` 이 결정적이다(시간 측정 안 함).
- **카메라 간 정합**: 빼고 굽기 12 dB 의 주 원인. 이음매가 있는 곳(두 카메라 패치 경계)에서 무늬가 어긋나 보일 수 있다.
- **아틀라스 수**: texrecon 이 작은 아틀라스를 여러 장(w1 25장) 만들어 glb 프리미티브가 26개다(그리기 호출). 뷰어에서 문제면 아틀라스 크기 조정·병합을 본다.
- **텍셀 경계**: 텍셀 중심 기준으로 칠해 아주 가는 삼각형은 지도에 안 남을 수 있다(그 텍셀은 0 = 보수적).
- **작은 구멍 기준**은 문헌·실측 검증 전 시작값이다.

## 넘긴 일

- 계약: 배경 glTF 의 출처 지도·비율을 실을 자리(`scene_output` 자산 종류 `SURFACE_SOURCE_MAP`·`ratios` 등), `surface_source` enum 의
  ESTIMATED_PLANE·GENERATED 예약, 출력 좌표가 World 가 아닐 때의 표시. `contracts/` 는 바꾸지 않았다.
- 뷰어: `textured.glb` 표시, `unobserved_neutral` 를 사각지대로 보이기, 출처 지도 최근접 표본 겹쳐 보기.
- 관측 영역 일부를 일부러 가려 2층 보간 오차를 재는 시험(작은 구멍 기준 검증).
- World 고정 성공 Run(-100 실측 정물 등)과 4차 결과(-282 해상도, -283 DA3 혼합)로 다시 굽기.
- 채택 시 Open3D·trimesh·Pillow 를 `scene` extra·잠금으로, 함수는 `ai/src/simai_ai/scene/` 로.
- `scene_output` 문서는 World 정렬이 OK 일 때만 낼 수 있어(좌표계 WORLD 고정) 이번에는 내지 않았다.
- (-314) 계약 `surface_source` enum 에 ESTIMATED_PLANE(3) 넣기, 정렬된 자세를 보정 쪽(world_frame)에 다시 넣어 World 를 다시 고정할지 결정,
  정렬 뒤 World 높이 실측(G4), 관제 앱에서 GLTF_Y_UP glb 확인(-59), 빈 교실 사진으로 다시 굽기.
