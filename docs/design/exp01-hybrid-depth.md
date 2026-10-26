# EXP-01 혼합 경로: MapAnything 자세 + 원본 해상도 깊이 (조사·설계·실험 스크립트)

S15P31A101-283. 기획서 §5.3(정적 3D, 비교군 Depth Anything 3 자세 조건·MoGe-3 단안 대체, G4 10 cm, 복원 후처리 순서), §20.2(EXP-01 연구 질문·필수 산출물),
§4.1·4.3(World 좌표계, MapAnything 초기 추정), §3.12(GPU 실행 환경), AGENTS 규칙 1·2·3·7 기준이다.
코드는 `ai/experiments/static_scene/run_hybrid_depth.py`, 시험은 `ai/tests/experiments/static_scene/test_hybrid_depth.py`(합성, GPU·torch·OpenCV 없음)다.
MapAnything 실행·World 고정은 `docs/design/world-frame-from-mapanything.md`(-102), 통합 이미지·잠금은 `docs/design/unified-env.md`, 실험 이미지는 `docs/design/exp-images.md` 를 따른다.
MapAnything 입력 해상도 비교(518·1036·1554·원본)는 병행 티켓 -282 몫이라 여기서는 다루지 않는다(MapAnything 은 공식 기본 518 결과를 자세·기준 깊이로만 쓴다).
이 티켓은 GPU 없이 조사·설계·스크립트까지다. 잠금·Dockerfile·`deploy/weights.json`·명세·계약은 바꾸지 않았다(아래 "넘긴 일").

## 배경 (3차 GPU 세션 w1, 2026-10-26, H100)

`CAL102_w1_map_anything_known_k_20261026-s3`: 새 교실 4장(펴진 2304×1296, 펴진 K fx 1161, 사람 마스크 15 px 팽창), MapAnything 공식 기본(긴 변 518, 모델 입력 518×294), bf16, 데운 추론 449 ms, `peak_alloc` 6.4 GB.

| 지표 | 값 | 판단 |
|---|---|---|
| 바닥 rms(`fix`, 3 cm 안 인라이어) | 1.15 cm, 상방 사전값과 1.8° | 보정 초기값에는 충분 |
| 점 수(신뢰도 하위 10% 뺌) | 44.7만, 원본 한 점 ≈ 4.4 px(2304 / 518) | 화면 배경으로는 성김 |
| 카메라 간 왕복 재투영 중앙값 | 원본 9.1~18.2 px(모델 2.0~4.1 px) | 깊이·자세 일관성 한계 |
| 카메라 높이(바닥에서) | 모델 단위 2.0~2.1 | 설치 목표 2.6~2.8 m 와 25% 안팎 차이 → 축척 확인 필요 |
| World 고정 | FAILED `AXES_AMBIGUOUS` | 이 결과는 MapAnything world 에서만 다룬다 |

연구 질문(이 티켓): MapAnything 자세·K 를 그대로 두고 깊이만 원본 해상도에 가깝게 다시 내면 점 밀도·가장자리·카메라 간 일관성이 나아지는가, 그리고 그 깊이를 MapAnything world 에 어떻게 맞춰 한 점구름으로 합치는가.

## 조사 (2026-10-26 확인, 출처는 줄마다)

확인 기준: Depth Anything 3 공식 저장소 `ByteDance-Seed/Depth-Anything-3` main `3d835ec`(2026-07-27, 태그·Release 없음, https://github.com/ByteDance-Seed/Depth-Anything-3),
논문 arXiv 2511.10647 v1(2025-11-13, https://arxiv.org/abs/2511.10647), map-anything main `3d10cf7`(2026-08-07, 우리가 쓰는 커밋과 같음), MoGe main `74fbce0`(2026-08-19).
"코드 독해"는 소스를 읽고 판단한 것이고 실행하지 않았다. 가중치는 내려받지 않았다(HF API 의 메타데이터만 읽음).

### Depth Anything 3

**모델**(README "Model Cards", HF API `huggingface.co/api/models/depth-anything/<이름>`).

| 모델 | README 매개변수 | 할 수 있는 것 | 가중치 라이선스(HF 카드 `license`) |
|---|---|---|---|
| DA3NESTED-GIANT-LARGE-1.1 | 1.40B (safetensors 6.76 GB) | 상대 깊이, 자세 추정, **자세 조건**, 3DGS, **미터 깊이**, 하늘 분할 | **cc-by-nc-4.0** (카드 "Non-commercial use only") |
| DA3-GIANT-1.1 | 1.15B | 상대 깊이, 자세 추정, 자세 조건, 3DGS | cc-by-nc-4.0 |
| DA3-LARGE-1.1 | 0.35B | 상대 깊이, 자세 추정, 자세 조건 | HF 카드 apache-2.0, GitHub README 표는 CC BY-NC 4.0 — **불일치, 정본 확인 못 함** |
| DA3-BASE / DA3-SMALL | 0.12B / 0.08B | 상대 깊이, 자세 추정, 자세 조건 | apache-2.0 |
| DA3METRIC-LARGE | 0.35B | 단안 미터 깊이(`focal × 출력 / 300`) | apache-2.0 |
| DA3MONO-LARGE | 0.35B | 단안 상대 깊이 | apache-2.0 |

- `-1.1` 은 학습 버그를 고쳐 다시 학습한 판이고 README 는 구판(NESTED·GIANT·LARGE)을 deprecated 로 둔다. README 매개변수와 HF safetensors 매개변수 수(NESTED 1.69B)는 다르다(어느 쪽이 맞는지 확인 못 함).
- 코드 라이선스 Apache-2.0(`pyproject.toml` `license`). 상업 사용이 되는 미터 깊이 모델은 단안 DA3METRIC-LARGE 뿐이고, 자세 조건 모델 중 Apache-2.0 은 BASE·SMALL(LARGE-1.1 은 위 불일치)이다.
  NESTED 는 MapAnything 기본 가중치(CC-BY-NC-4.0)와 같은 비상업 조건이다. 기존 판단("비상업: 교육 시연 가능, 공개 산출물에 표기", `world-frame-from-mapanything.md`)을 그대로 적용한다(Lead 결정 2026-10-26, 명세 수정 33). Apache 대안은 BASE·SMALL·METRIC-LARGE.

**자세 조건 추론**(`src/depth_anything_3/api.py` `DepthAnything3.inference`, docs/API.md, 코드 독해 + api.py 직접 확인).

- 서명: `inference(image, extrinsics=None, intrinsics=None, align_to_input_ext_scale=True, infer_gs=False, use_ray_pose=False, ref_view_strategy="saddle_balanced", …, process_res=504, process_res_method="upper_bound_resize", export_dir=None, export_format="mini_npz", …) -> Prediction`.
  `extrinsics` 는 (N, 4, 4) **world→camera**(OpenCV), `intrinsics` (N, 3, 3). MapAnything cam2world 는 역행렬로 넣는다. extrinsics 만 주고 intrinsics 를 빼면 `intrinsics.numpy()` 에서 실패한다(둘 다 준다).
- `Prediction`(`specs.py`): `depth` (N, H, W), `is_metric`, `conf`, `extrinsics`, `intrinsics`, `processed_images`, `scale_factor` 등.
- 내부 동작: 입력 K 는 크기 바꾸기·자르기에 맞춰 함께 옮긴다 → 입력 자세를 첫 시점 원점·카메라 거리 중앙값으로 정규화 → (T, 쿼터니언, fov) 카메라 토큰. **주점(cx, cy)은 조건에 들어가지 않는다**(`model/utils/transform.py`).
- **자세를 넣어도 모델은 자세를 다시 예측하고**, 예측 자세를 입력 자세에 Umeyama 로 맞춘다. `align_to_input_ext_scale=True`(기본)면 출력 extrinsics·intrinsics = 입력값, `depth /= scale` — 깊이 단위가 **넣은 자세의 단위**를 따른다.
  False 면 맞춘 예측 자세를 돌려준다(깊이는 그대로).
- NESTED 의 미터: README FAQ "output from DA3NESTED-GIANT-LARGE is already in meters". 내부는 any-view 깊이를 미터 가지(예측 초점/300)에 최소제곱 축척으로 맞춘 것이고, 자세를 넣으면 그 위에 위 Umeyama 정렬이 덮여 **NESTED 자체의 미터 추정은 입력 자세 단위로 바뀐다**(코드 독해).
  논문은 자세 조건 모드가 미터 깊이를 낸다고 쓰지 않았고 학습 때 자세 조건은 확률 0.2 로만 켰다(Sec. 3.4).

**해상도·성능.**

- 기본 `process_res=504`, 방식 `upper_bound_resize`·`lower_bound_resize`(각 변을 가장 가까운 14 배수로)·`upper_bound_crop`·`lower_bound_crop`(14 배수로 내려 가운데 자름, 주점 보정). 패치 14(`utils/io/input_processor.py`).
  코드상 최대 크기 제한은 없고 출력 깊이는 처리 해상도 그대로다(원본으로 되돌리지 않음, issue #121 "multiple of 14").
- 2304 처리(우리 계산): 2310×1302 → 시점당 토큰 165×93 = 15,345, 4장 약 6.1만. 학습은 504 기반(가장 긴 896×504, Sec. 3.4)이라 **2304 는 학습 분포 밖**이고 그 해상도의 품질·메모리 공식 수치는 없다(확인 못 함).
- 공식 수치는 논문 Table 8 하나: A100, 32장, 504×336, 장당 평균 Giant 37.6 FPS, Large 78.4, Base 126.5, Small 160.5. 80GB 한 번에 Giant 900~1000장, Large 1500~1600장. GB 단위 VRAM 표·해상도별 표는 없다.

**의존성과 통합 잠금(torch 2.11.0 cu130, numpy 2.3.5, vLLM 0.21.0, transformers 5.9.0).**

- `pyproject.toml`(직접 확인): `requires-python ">=3.9, <=3.13"`, `torch>=2`, `torchvision`, **`numpy<2`**, `opencv-python`, `xformers`, `open3d`, `trimesh`, `evo`, `e3nn`, `moviepy`·`moviepy==1.0.3`, `pycolmap`, `huggingface_hub`(무고정), `omegaconf`, `einops`, `safetensors`, `plyfile`, `pillow_heif`, `fastapi`, `uvicorn`, `typer>=0.9.0`, `pre-commit` 등. extras `[app]` gradio>=5, `[gs]` gsplat 커밋 고정.
- 충돌: (1) **`numpy<2` ↔ 잠금 numpy 2.3.5** 정면 충돌. map-anything 이 extra 로 까는 포크 `Nik-V9/Depth-Anything-3` 는 `307f00b`(2025-11-16, "Make requirements compatible with MapAnything")에서 numpy 고정을 풀었다(HEAD `b9db148`, 2026-01-06, 상류 2025-12-12 까지만 합침).
  (2) `opencv-python` ↔ 잠금의 headless 하나 정책(같은 `cv2` 폴더). (3) `moviepy==1.0.3` 은 `decorator<5`·`imageio<3`·`proglog<=1.0.0` 상한(numpy 상한은 없음). (4) torch·transformers 는 하한만이라 충돌 없음(transformers 는 의존성에 없다).
  vLLM 0.21 과 한 환경에 깔리는지는 확인 못 함.
- **import 경로(노트북에서 소스 AST 로 확인, 실행 안 함).** `from depth_anything_3.api import DepthAnything3` 가 모듈 수준에서 닿는 외부 패키지: torch, torchvision, numpy, PIL, addict, cv2, einops, evo(`utils/pose_align.py` `PosePath3D`), huggingface_hub, imageio, matplotlib, moviepy(`moviepy.editor`, 1.x 전용), omegaconf, plyfile, pycolmap, scipy, tqdm, trimesh.
  `utils/export/__init__.py` 가 gs·colmap 내보내기를 모두 import 해서 쓰지 않는 moviepy·pycolmap 도 필요하다. gsplat(try/except)·xformers(`SwiGLU` try/except, 없으면 PyTorch 구현, 어텐션은 SDPA)·open3d·e3nn 은 추론 import 에 필요 없다.
- xformers PyPI 최신 0.0.35(`torch>=2.10`, 2026-02-20). torch 2.11 과 바이너리 호환은 확인 못 함(필요 없으므로 깔지 않는다).

**map-anything 의 DA3 외부 모델 인터페이스**(`mapanything/models/external/da3/__init__.py` `DA3Wrapper`, 팩토리 키 `"da3"`, `ece4611` 2025-12-18 추가·`f065d13` 2026-01-18 수정).
설정 `configs/model/da3_nested.yaml`(`depth-anything/DA3NESTED-GIANT-LARGE-1.1`), 기본 `task: images_only`, 자세·K 입력은 `model/task=mvs`·`calibrated_sfm`·`posed_sfm`, README 권장 해상도 504, 설치 `pip install -e ".[depth-anything-3]"`(위 포크).
**래퍼는 `inference()` 가 아니라 forward 를 바로 부른다**(`use_ray_pose=True`, bf16): 첫 프레임 기준 상대 w2c 만 넘기고 거리 정규화·Umeyama 정렬을 하지 않으며 출력 자세는 **예측 값**이다. K 만 주고 자세를 안 주면 K 를 쓰지 않는다(코드 독해).
→ "입력 자세를 고정한 깊이"가 필요하면 래퍼가 아니라 공식 `inference()` 를 직접 부른다(이 스크립트의 선택).

### MoGe (짧게)

- MoGe-2(`Ruicheng/moge-2-vitl` 326M·`-vitl-normal` 331M·`-vitb-normal` 104M·`-vits-normal` 35M): 단안 **미터** 점 지도·깊이·정규화 K·마스크, 출력 크기 = 입력 크기.
  알려진 카메라 입력은 `infer(..., fov_x=가로 화각(도))` 하나뿐이다(전체 K·주점 없음, `moge/model/v2.py` 코드 독해). 해상도는 `resolution_level` 0~9(기본 9) → `num_tokens` [1200, 3600](권장 1200~2500): 출력은 원본 크기지만 내부 토큰 상한이 있다.
- **MoGe-3 는 공식 공개됐다**(README 뉴스 2026-08-18, `Ruicheng/moge-3-vitg` 1.25B·`moge-3-vitl` 370M, arXiv 2607.17967). FlexGEMM(Triton) 필요 — 통합 잠금에서는 triton 판 충돌로 flex-gemm 을 뺐다(`unified-env.md`).
- 라이선스: 코드 MIT(DINOv2 부분 Apache-2.0), HF 가중치 MoGe-2·MoGe-3 모두 `mit`(HF API 확인). 의존성 `numpy>=2`, `torch>=2.4` 로 잠금과 충돌 없음(MoGe @74fbce0 은 exp02 이미지에 이미 있다, MoGe-2 vitl-normal 가중치는 `weights.json` exp02 묶음).

### 조사에서 나온 명세와의 차이 (보고)

1. 명세 5.3·20.2 의 비교군 "DA3NESTED-GIANT-LARGE-1.1 자세 조건, **미터 단위**, **MapAnything 외부 모델 인터페이스로 바꿔 끼운 것**": 자세를 넣으면 깊이는 넣은 자세의 단위를 따르고(공식 `inference`), MapAnything 의 래퍼는 자세를 고정하지 않고 예측 자세를 낸다.
   즉 "외부 모델 인터페이스로 끼운 자세 조건 + 미터"는 한 경로로 동시에 성립하지 않는다. 이 티켓은 (a) 공식 `inference` + MapAnything 자세(단위 = MapAnything 단위), (b) 자세 없이 NESTED 단독(미터, 축척 근거 후보) 두 경로로 나눠 잰다. 명세 문구 정리는 Lead 판단.
2. 명세 5.3 "MoGe-3 는 단안 대체": MoGe-3 는 있지만 통합 잠금에 flex-gemm 이 없어 지금 이미지에서 못 돈다. 이 티켓의 MoGe 어댑터는 exp02 이미지에 있는 MoGe-2 로 경로를 잡았다.

## 설계

### 입력

| 입력 | 출처 | 쓰는 곳 |
|---|---|---|
| MapAnything Run 폴더 | `run_mapanything.py --mode prepared` 출력: `cameras.json`(cam2world OpenCV, MapAnything world), `raw.npz`(모델 해상도 depth_z·conf·mask·intrinsics), `metrics.json`(`prepared_sha256`) | 자세(고정), 정합 기준 깊이, 연결 확인 |
| 입력 폴더 | `world_frame.cli prepare` 출력(`simai.mapanything_inputs`): 펴진 원본 영상, 펴진 K(원본 해상도), 사람 마스크 PNG | 깊이 모델 입력, 마스크, 색 |
| World 정렬(선택) | `world_frame.cli fix` 결과(`simai.world_alignment`) | status OK 면 출력을 World 로 |

연결 확인(종료 2): MapAnything `metrics.json` 의 `prepared_sha256` ≠ 입력 폴더, 깊이 폴더의 `prepared_sha256`·`cameras_json_sha256`(자세 입력) ≠ 지금 입력, World 정렬의 `cameras_json_sha256` ≠ 이 Run, 결과 폴더가 비어 있지 않음(덮어쓰지 않음).

### 두 단계

| 단계 | 어디서 | 환경 | 출력 |
|---|---|---|---|
| `predict` | GPU 세션(da3·moge) / 노트북(fake) | numpy + 깊이 모델만(모듈 최상위 import 는 표준 라이브러리·numpy 뿐, simai_ai 없음 — 시험이 확인) | 깊이 폴더 `simai.hybrid_depth` 0.1.0: `depth.json`(어댑터·판·가중치 해시·자세 조건 여부·처리 해상도·시간·VRAM) + `<view>.depth.npz`(원본 해상도 z 깊이 float32, 유효, 신뢰도) |
| `fuse` | 노트북 CPU | `uv run --project ai --with opencv-python-headless [--with trimesh]` | `hybrid_metrics.json`(`simai.hybrid_depth_result` 0.1.0), `fused.ply`, (trimesh 있으면) `pointcloud.glb`·`gridmesh.glb` |

나눈 이유: DA3 의존성(`numpy<2`·moviepy 1.x·evo·pycolmap)을 통합 잠금에 넣지 않고 별도 가상환경에서 깊이만 내고, 정합·융합·지표는 노트북에서 같은 코드로 여러 번 돌린다(GPU 시간 최소). 결과 묶음은 깊이 폴더만 가져오면 된다(4장 약 27 MB).

**어댑터.**

- `fake`: MapAnything 깊이(518×294)를 원본 해상도로 올린다(정보는 늘지 않음). 노트북 경로 점검과 **기준선**(같은 지표를 MapAnything 만으로 낸 값)이다. `--fake-scale cam=배율` 로 정합 점검.
- `da3`: 공식 `DepthAnything3.inference` 에 네 장·펴진 K·MapAnything w2c 를 한 번에 준다. 첫(데움) 호출은 `align_to_input_ext_scale=False` 로 돌려 **예측 자세와 입력 자세의 차이**(회전 각, 중심 거리 / 카메라 간 거리 중앙값)를 기록하고, 둘째(기록) 호출을 True 로 돌려 그 깊이를 쓴다.
  출력 K 가 처리 해상도 기준이면 그대로, 원본 기준이면 크기 비로 옮겨(`k_for_depth_grid`, 어느 쪽인지 기록) 원본 해상도로 다시 표본한다. `--no-pose` 는 자세 없이(NESTED 미터 깊이 → 축척 근거 후보), `--ray-pose` 는 ray 헤드 자세.
- `moge`: MoGe-2 를 카메라마다 따로, 펴진 K 의 가로 화각을 `fov_x` 로 준다(자세 없음, 축척은 정합이 맞춘다).

**다시 표본(`resample_depth`).** 같은 카메라(중심·회전 같음)라 광선이 같으므로 z 깊이를 K_src·K_dst 격자 사이로 옮긴다(정수 = 픽셀 중심 규약, 펴진 K 의 주점 (W−1)/2 와 같은 규약).
**역깊이(1/z)를 쌍선형 보간**한다(평면에서 1/z 는 픽셀 좌표의 1차 함수라 평면이 휘지 않는다. z 를 보간하면 합성 평면에서 바닥 rms 가 커졌다). 네 이웃이 모두 유효해야 하고, 네 값의 폭이 5%(상대)를 넘으면 섞지 않고 가장 가까운 이웃 값을 쓴다(불연속 사이에 떠 있는 점을 만들지 않음).

### 깊이 → MapAnything world 정합 (자세 고정)

- 자세(cam2world)는 MapAnything 값을 그대로 쓴다. 깊이만 카메라별로 맞춘다: `d_ref ≈ s·d (+ t)`. 기준 `d_ref` 는 MapAnything 깊이(모델 해상도, `world_frame` 과 같은 규칙: MapAnything 마스크·신뢰도 하위 10%·사람 마스크 뺌, `load_mapanything_run` 재사용)이고 그 픽셀을 원본 격자로 옮겨 새 깊이를 표본한다.
- 맞춤(`fit_scale_shift`): 축척은 log(d_ref/d) 의 중앙값에서 시작해 인라이어(|d_ref − s·d − t| ≤ 5%·d_ref)로 5번 다시 맞춘다. `scale`(기본)은 인라이어 로그 비 중앙값, `scale_shift` 는 인라이어 상대 가중 최소제곱, `none` 은 맞추지 않음(DA3 자세 조건은 이미 MapAnything 단위라 축척 ≈ 1 인지 보는 대조).
- **모르면 비운다**: 표본 500 미만이거나 인라이어 30% 미만이면 그 카메라는 정합 실패로 융합에서 빼고 경고한다(축척 1 로 채우지 않는다).
- 기록: 카메라별 축척·이동·인라이어 비율·맞추기 전후 |Δd|/d, 카메라 간 축척 비(`scale_spread` = 최대/최소). DA3 자세 조건에서 축척이 카메라마다 다르면(예: 2% 넘게) 단안 편향이 남은 것이다.
- **이동(shift) 보정은 기본으로 하지 않는다**: 미터 깊이 모델 출력에 이동을 더하면 가까운 곳·먼 곳의 기하가 함께 휘어 카메라 간 불일치를 숨긴다. `scale_shift` 는 MoGe 처럼 단안 모델의 대조로만 본다.
- 축척 근거: 자세 조건 DA3 는 단위를 MapAnything 에서 물려받으므로 축척 독립 근거가 아니다. `--no-pose` NESTED·MoGe-2 의 축척 s(= MapAnything / 모델 미터)는 독립 **모델 추정** 근거로 기록만 한다(명세 4.3 우선순위에서 모델 추정은 마지막, 실측 정물·사람 키가 우선. World 축척은 `world_frame` 의 `choose_scale` 이 정한다).

### 융합

- **점 융합 + 가시성 확인 + 칸 평균**(MVP). 정합된 카메라마다 점(`--stride`, 기본 1 = 원본 모든 픽셀)을 다른 카메라에 투영해
  지지(그 카메라 깊이와 3% 안)·충돌(그 카메라가 본 표면 — 투영 위치 네 이웃의 가장 가까운 깊이 — 보다 5% 넘게 앞, 즉 그 카메라에서는 빈 공간)을 센다. 충돌 수 > 지지 수인 점은 뺀다.
  네 이웃 최소로 충돌을 보는 이유: 스치는 각도(카메라 높이 근처 천장)·모서리에서 보간 오차가 5% 를 넘어 합성 정확 장면에서도 1.2% 가 충돌로 빠졌다(네 이웃 최소로 바꾼 뒤 0%).
- 다른 카메라에 안 보이는 점(단일 시점)은 남기고 비율을 적는다(관측은 했지만 교차 확인은 못 한 점. 지어낸 점이 아니다).
- 칸(`--voxel`, 기본 1 cm)마다 점·색 평균. 1 cm 는 바닥 rms(1.2 cm)·G4(10 cm)보다 작고, 교실 바닥 약 36 m² 에서 칸당 최대 1만 점/m².
- **표면 생성**: MVP 는 카메라별 격자 메시(`--mesh-stride` 4, 네 꼭짓점 유효·깊이 폭 5% 안인 칸만, 가시성 확인 전 깊이)와 점구름 glb 다. TSDF·메시 단순화는 넘긴 일(아래 "버린 대안").

### 출력과 좌표 (AGENTS 3)

- 정합·융합은 MapAnything world 에서 한다. `--world-alignment` 가 OK 면 출력 점·메시·자세에 `mapanything_to_world`(sR | T)를 곱해 World(m, 오른손, +Z 상방, 바닥 Z=0)로, 없거나 FAILED 면 MapAnything world 그대로 두고 `output_frame.frame = MAPANYTHING_WORLD`(World·Scene 으로 쓰지 않는다)로 적는다.
- glb 정점은 출력 좌표 그대로다. Three.js (x, z, −y) 변환은 뷰어 어댑터 한 곳에서만 한다.
- 결과는 보기용 Visual Geometry 다. 바닥 평면·OBB·위험 구역(Operational Geometry)과 Tier 0 위치·판정에 넣지 않는다(명세 5.3, AGENTS 1).
- 출처: 입력 폴더의 `input_provenance` 를 그대로 싣는다(SIM·DATASET_GT 면 `is_upper_bound` true 가 따라온다). fake 결과는 어댑터 `FAKE_UPSAMPLED_MAPANYTHING` 이라 깊이 모델 성능이 아니다.

### 판정 지표 (`hybrid_metrics.json`)

| 지표 | 정의 | 기준선(fake = MapAnything 518) 대비 기대 |
|---|---|---|
| 카메라 간 깊이 일관성 | i 의 점(2만 표본)을 j 에 투영, \|z − d_j\|/d_j(0.2 안 = 같이 보임) 중앙값·p95, 1%·5% 안 비율 | 중앙값이 줄어야 한다 |
| 카메라 간 왕복 재투영(원본 px) | 5% 안인 쌍을 j 점으로 되돌려 처음 픽셀과의 거리. `world_frame` 지표를 원본 해상도에서(같은 뜻, 다른 표본 격자) | w1 9~18 px 보다 작아야 한다 |
| 정합 | 카메라별 축척·인라이어·맞춘 뒤 \|Δd\|/d, 축척 비 | DA3 자세 조건 축척 ≈ 1, 축척 비 작게 |
| 바닥 rms | 출력 점에서 `find_floor`(world_frame 과 같은 규칙, 3 cm 안 인라이어) rms, World 면 \|Z\|<5 cm rms 도 | 기준선 1.34 cm(원본 격자, `fix` 의 모델 격자 값은 1.15) 이하 |
| 점 밀도 | 융합 점 수, 바닥 면 밀도(점/m², 5 cm 칸 점유 면적), 모델 처리 해상도와 원본 px/표본 | 기준선 4.4 px/표본 → 1~2 px |
| 가장자리 선명도 | 3×3 이웃 깊이 폭 > 10% 인 불연속 주변 중 이웃 최소·최대에서 둘 다 2% 넘게 떨어진(떠 있는) 픽셀 비율 | 낮을수록 선명 |
| 가시성 | 카메라별 충돌로 뺀 비율, 지지 비율, 단일 시점 비율 | 충돌 비율 작게 |
| 성능 | predict 모델 적재·추론 ms(데움 뒤)·`peak_alloc_mb`, fuse 초 | 분석 워커 GPU 상한 안 |
| G4 | 개발 교실 줄자 실측 대비(명세 5.3, 10 cm) | 실측이 생길 때까지 `NOT_MEASURED` |

채택 기준(Lead 결정 2026-10-26, 명세 수정 33 20.2): 같은 입력에서 DA3 결과가 기준선보다 카메라 간 |Δd|/d 중앙값과 왕복 재투영이 작고, 바닥 rms 가 1.5 cm 이하, 충돌 비율 5% 이하, H100 에서 predict 가 분석 워커 예산 안이면 "배경용 깊이로 쓸 만함"으로 보고 G4 실측으로 넘어간다. 기준선보다 나쁘면 MapAnything 해상도 비교(-282) 결과와 함께 다시 본다.

### 정한 수치

| 수치 | 값 | 근거 |
|---|---|---|
| 보간 이웃 폭 | 5% | 교실 평면은 원본 1 px 사이 깊이 변화가 1% 안. 책상 가장자리·사람 경계(수십 %)는 넘는다 |
| 정합 인라이어 | 5% | 깊이 1~12 m 에서 5~60 cm. 기준선 카메라 간 \|Δd\|/d p95 가 11~16% 라 그보다 좁게, 모델 간 국소 차(수 %)는 받게 |
| 정합 최소 표본·인라이어 | 500·30% | 기준 깊이 유효 픽셀은 시점당 약 9~10만. 그 0.5% 도 안 되거나 70% 넘게 어긋나면 같은 장면 깊이로 볼 수 없다 |
| 지지 3% / 충돌 5% | — | 지지는 기준선 \|Δd\|/d 중앙값(1~2%)보다 조금 넓게, 충돌은 정합 인라이어와 같은 폭(그보다 작은 차를 빈 공간 위반으로 보지 않는다) |
| 같이 보임 20%, 왕복 5% | — | 같이 보임은 가림(대개 수십 %)과 깊이 오차를 가르는 폭, 왕복 5% 는 world_frame `depth_tol` 과 같게 |
| 칸 1 cm | — | 위 "융합" |
| 불연속 10%·떠 있음 2% | — | 책상 상판 가장자리(0.75 m 아래 바닥)는 수십 %, 평면 경사는 1 px 사이 1% 안 |
| 표본 2만/카메라, 시드 0 | — | 노트북 4대 1초 안, 같은 입력이면 같은 결과 |

## 노트북 확인

**합성 상자 교실(SIM, 상한, 제품 성능 아님)** — `test_hybrid_depth.py` 16개, 8 × 6.5 × 2.9 m 방, 모서리 카메라 3대(높이 2.6 m, 가로 85°).

| 경우 | 결과 |
|---|---|
| 모델 해상도(84×48) → 원본(336×192) 다시 표본 | 정답 깊이 대비 \|Δd\|/d 중앙값 < 0.2%(방 모서리 꺾임에서만 오차) |
| 2 m / 4 m 계단을 가로지르는 표본 | 3 m(떠 있는 점)를 만들지 않음, 무효 이웃은 섞지 않음 |
| 축척 1.07 + 이상점 25% / 축척·이동 1.05·0.2 | 1.07 ± 0.3%(인라이어 75%) / 정확히 복원 |
| 표본 100개, 서로 상관없는 깊이, 맞추지 않은 10% 차 | 정합 실패(축척을 1 로 채우지 않음) |
| 카메라 하나 깊이 × 1.05 | 그 카메라 \|Δd\|/d 중앙값 3~7%, 1% 안 비율 < 20%(정확하면 각 0.2% 미만·왕복 < 0.6 px) |
| 한 카메라 픽셀 묶음을 깊이 × 0.6(빈 공간에 뜸) | 융합이 모두 뺌. 정확한 다른 카메라는 충돌 0% |
| 계단 / 3 px 에 걸쳐 퍼진 계단 | 떠 있는 점 비율 0 / 0.3 초과 |
| fake 어댑터 끝까지(카메라 하나 × 1.08) | 정합 축척 1/1.08 ± 0.2%, 정합 뒤 카메라 간 \|Δd\|/d < 0.5%, 바닥 OK·카메라 높이 2.6 ± 2 cm |
| World 정렬 OK(단위 행렬) / 다른 Run 의 정렬 / FAILED 정렬 / 다른 입력 폴더 / 같은 결과 폴더 | WORLD·\|Z\| rms < 1.5 cm / 종료 2 / MapAnything world 그대로 / 종료 2 / 종료 2(덮어쓰지 않음) |
| predict 모듈 최상위 import | 표준 라이브러리·numpy 뿐(깊이 모델 전용 가상환경에서 돈다) |
| 깊이 모델 실패(가짜 MemoryError) | `depth.json` 에 result ERROR·오류를 남기고 종료 3, fuse 는 그 폴더를 거절(종료 2) |

**실제 교실(w1 + classroom4-prepared-20261026, fake 어댑터 = MapAnything 518 기준선, 노트북 CPU).** 결과는 scratchpad(커밋 안 함, 사람이 찍힘).
`uv run --project ai --with opencv-python-headless --with trimesh python ai/experiments/static_scene/run_hybrid_depth.py all --ma-run <w1> --prepared <입력 폴더> --adapter fake --out …`: predict 2.1 s, fuse 35~46 s, 전체 57 s.

| 지표 | cam_A | cam_B | cam_C | cam_D |
|---|---|---|---|---|
| 카메라 간 \|Δd\|/d 중앙값 / p95 | 1.33% / 12.6% | 0.96% / 11.8% | 1.04% / 11.1% | 1.90% / 15.9% |
| 1% / 5% 안 비율 | 0.42 / 0.83 | 0.51 / 0.85 | 0.49 / 0.87 | 0.33 / 0.74 |
| 왕복 재투영 원본 px 중앙값 / p95 | 11.7 / 86 | 8.7 / 77 | 15.7 / 76 | 17.9 / 107 |
| 충돌로 뺀 비율 / 지지 / 단일 시점 | 1.7% / 0.60 / 0.20 | 2.4% / 0.55 / 0.14 | 2.0% / 0.49 / 0.19 | 1.7% / 0.37 / 0.29 |
| 사람 마스크로 뺀 픽셀 | 28.0만 | 24.5만 | 8.2만 | 29.9만 |

- 왕복 재투영 중앙값 8.7~17.9 px 는 `world_frame fix` 의 w1 값(9.1~18.2 px)과 맞는다: 기준선이 같은 지표를 재현한다(정합·투영 경로 점검).
- 융합: 원본 픽셀 765만 점 → 1 cm 칸 141.6만 점(MapAnything 점구름 44.7만의 3.2배이지만 정보는 518×294 그대로, 원본 px/표본 4.45). 바닥 rms 1.34 cm(3 cm 안 인라이어 35.7만, 점유 36.5 m², 9,791 점/m²), 카메라 높이 2.00~2.09(모델 단위, `fix` 와 같음).
- 가장자리: MapAnything `mask_edges` 가 불연속을 지워 3×3 이웃이 모두 유효한 불연속 픽셀이 0 이다(기준선 가장자리 지표는 비어 있다, 잔여 위험).
- `--fake-scale cam_B=1.1 cam_D=0.93`: 정합 축척 0.90909·1.07527(= 1/1.1, 1/0.93), 일관성 지표는 배율 없을 때와 같다.
- `--world-alignment <w1 world-fix>`(FAILED `AXES_AMBIGUOUS`): MapAnything world 그대로 출력하고 그 사유를 기록.
- 출력 크기: `fused.ply` 21 MB, `pointcloud.glb` 23 MB, `gridmesh.glb`(stride 4) 19 MB, 깊이 폴더 27 MB.

## 다음 GPU 세션 큐 항목

전제: H100 80GB, LIVE 관제 시간 밖(EXP Job), 한 시점 GPU 인스턴스 하나. 통합 이미지 Pod 에서 Tier 0 를 띄우지 않은 상태로 돈다(이미지는 바꾸지 않고 컨테이너 디스크에 별도 가상환경). 결과는 깊이 폴더만 노트북으로 가져와 `fuse` 를 노트북에서 돌린다(사람이 찍힌 입력이라 커밋 안 함).
입력은 w1 과 같은 MapAnything Run(`CAL102_w1_…_20261026-s3`)과 입력 폴더(`classroom4-prepared-20261026`)를 Pod `datasets/`·`runs/` 에 올린 것이다(깊이 폴더가 `cameras_json_sha256` 으로 그 Run 을 확인한다).

| # | 항목 | 명령(요지) | 예상 |
|---|---|---|---|
| h0 | DA3 가상환경 | `uv venv /root/da3 --python 3.11` → `uv pip install --python /root/da3 torch==2.11.0 torchvision==0.26.0 numpy==2.3.5 einops huggingface_hub safetensors omegaconf addict opencv-python-headless pillow imageio matplotlib scipy tqdm trimesh plyfile "moviepy==1.0.3" pycolmap evo` → `uv pip install --python /root/da3 --no-deps "git+https://github.com/ByteDance-Seed/Depth-Anything-3@3d835ec1a5802d64a8b8b15f817a1ab54809bfe4"` → `/root/da3/bin/python -c "from depth_anything_3.api import DepthAnything3"` | 5~10 분(torch cu130 휠 내려받기). `--no-deps` 로 `numpy<2`·`opencv-python` 고정을 피한다. evo·moviepy 1.0.3 의 하위 의존성이 풀리지 않으면 포크 `Nik-V9/Depth-Anything-3@b9db148` 로 바꾼다 |
| h1 | DA3 가중치 | `hf download depth-anything/DA3NESTED-GIANT-LARGE-1.1 --revision b2359bdf72 --local-dir <root>/weights/da3nested-giant-large-1.1` 후 sha256 기록(Run 기록 `weights_hash`) | 6.76 GB, 2~5 분 |
| h2 | DA3 자세 조건 504(공식 기본) | `SIMAI_DA3_COMMIT=3d835ec /root/da3/bin/python ai/experiments/static_scene/run_hybrid_depth.py predict --ma-run <w1 Run> --prepared <입력 폴더> --adapter da3 --model <h1> --process-res 504 --out <root>/runs/exp01/HYB283_da3_pose_504_<세션>` | 적재 30~60 초 + 추론 2회, 2 분 |
| h3 | 같은 1008 | `--process-res 1008` | 2 분 |
| h4 | 같은 1512 | `--process-res 1512` | 3 분 |
| h5 | 같은 2304(원본) | `--process-res 2304`(2310×1302). OOM 이면 `depth.json` 에 result OOM 이 남고 종료 3(큐는 다음 항목으로) | 5 분 |
| h6 | 자세 없이 504 | `--no-pose --process-res 504`(NESTED 미터 깊이 → 축척 근거 후보, 자세 예측 품질) | 2 분 |
| h7 | ray 헤드 자세 | h2~h5 중 가장 좋은 해상도에 `--ray-pose` | 3 분 |
| h8 | MoGe-2 단안 | exp02 이미지(MoGe @74fbce0)에서 `… predict --adapter moge --model <root>/weights/moge/moge-2-vitl-normal/model.pt --out …/HYB283_moge2_vitl_<세션>` (exp02 Pod 를 따로 켜야 하면 생략 가능) | 2 분 |

합계 GPU 약 25~35 분(환경·가중치 포함). 각 항목은 gpu_queue Job(`deploy/scripts/exp_job.py` 로 감쌈, Run 기록 `experiment_id: EXP-01`, `jira_key: S15P31A101-283`, 가중치·입력 절대 경로)으로 넣는다.

**큐 확정(S15P31A101-291, 4차 세션).** 생성기 `ai/experiments/static_scene/session4_jobs.py`(시험 `ai/tests/deploy/test_session4_queue.py`), 실행서 `docs/design/runpod-environment.md` "4차 세션". 위 표와 다른 점:
- h8(MoGe-2)은 넣지 않는다(S15P31A101-287 에서 제외). 생성기가 `--only h8` 을 거절한다.
- h1 은 `hf download` 대신 simai-bootstrap 의 받기(`SIMAI_WEIGHTS_MANIFEST=ai/experiments/static_scene/da3_weights.json`, 묶음 `da3`)를 쓴다. 같은 리비전 고정(`b2359bdf726fb44ef62acca04d629dcf158053e7`)에 더해 sha256(HF LFS oid `8ebe871a…`, config `09adf894…`)이 맞을 때만 게시하고, 다르면 받은 파일을 버리거나(있던 파일은 지우지 않고) 종료 2 다. 이어받기·검증 스탬프도 그대로다. Run 기록 `weights_hash` 는 run_record 가 두 파일로 낸다. 이미지 목록(`deploy/weights.json`)에는 넣지 않았다(채택 전, 아래 "넘긴 일").
- h2~h7 은 시작 때 같은 확인을 다시 거쳐(스탬프라 다시 해시하지 않음) h1 이 실패했거나 파일이 바뀌었으면 predict 를 돌리지 않는다. 결과 폴더 보호·지표는 `exp_job.py --metrics depth.json`(predict 는 `metrics.json` 대신 `depth.json` 을 쓰고, OOM·ERROR 도 거기 남는다).
- h7 은 h2~h5 를 본 뒤 `--only h7 --h7-res <504·1008·1512·2304 중>` 로 Job 파일을 하나 더 만든다(그 밖의 값은 거절).
- h0 은 실패한 시도가 남긴 `/root/da3` 를 지우고 다시 만든다(이어 쓰지 않음).
세션 뒤 노트북: 깊이 폴더마다 `run_hybrid_depth.py fuse --ma-run <w1> --prepared <입력 폴더> --depth <깊이 폴더> --out <결과>`(각 약 1 분), fake 기준선과 지표 표를 비교한다.

## 버린 대안

- **map-anything `DA3Wrapper`(외부 모델 인터페이스)로 끼우기**: forward 를 바로 불러 입력 자세를 고정하지 않고 예측 자세를 내며 Umeyama 축척 맞춤도 없다. 자세를 MapAnything 값으로 고정하는 이 경로의 전제와 맞지 않는다. 래퍼 경로(자세까지 DA3 가 다시 내는 것)는 MapAnything 대 DA3 "전체 교체" 비교라 다른 질문이다.
- **DA3 를 통합 잠금에 넣기**: `numpy<2`·`opencv-python`·moviepy 1.x 를 Tier 0·vLLM 과 한 가상환경에 두게 된다. 판정 전에는 별도 가상환경(깊이만 내는 `predict`)으로 충분하다.
- **TSDF 융합(Open3D `ScalableTSDFVolume`)**: DA3 저장소도 벤치마크 평가에만 쓴다(`bench/utils.py`, ScanNet++ voxel 2 cm·trunc 15 cm·최대 깊이 5 m). Open3D 는 잠금에 없고, 교실(깊이 12 m, 스치는 각도의 천장·바닥)은 최대 깊이를 자르면 바닥이 비고 안 자르면 truncation 이 먼 쪽에서 표면을 두껍게 만든다. MVP 산출물이 "점군 → 단순 메시"(명세 5.3)라 점 융합 + 격자 메시로 먼저 지표를 보고, 메시 품질이 부족하면 TSDF·Poisson 을 넘긴 일로 본다.
- **이동(shift) 포함 정합을 기본으로**: 위 "정합". 대조로만 둔다.
- **MapAnything 깊이 대신 카메라끼리 서로 맞추기(자세·축척 동시 최적화)**: 자세를 바꾸게 되어 MapAnything 자세 = World 고정·보정 초기값이라는 연결이 깨진다. 자세는 그대로 두고 깊이만 맞춘다.
- **모델 입력 영상에서 사람 지우기**: MapAnything 과 같은 이유(모델이 보는 장면이 바뀜)로 하지 않고 결과 깊이에서 마스크 픽셀을 뺀다.
- **z 깊이 쌍선형 보간**: 평면이 휜다(합성 평면에서 확인). 역깊이를 보간한다.

## 잔여 위험

- DA3 자세 조건 깊이는 입력 자세에 Umeyama 로 맞춘 것이라, 예측 자세가 MapAnything 자세와 회전이 다르면 깊이 지도가 입력 자세에서 조금 비틀려 놓인다. 데움 호출의 자세 차이 기록(회전 각·중심 거리 비)과 카메라 간 일관성으로만 드러난다.
- 주점은 DA3·MoGe 조건에 안 들어간다. 펴진 K 주점이 가운데(cv2 `centerPrincipalPoint`)라 영향은 작을 것으로 보지만 확인은 GPU 결과로 한다.
- 2304 처리는 학습 분포 밖이라 깊이가 오히려 나빠질 수 있다(h2~h5 로 본다). 메모리·시간 공식 수치가 없다.
- DA3 출력 K 가 처리 해상도 기준이라고 코드(`_align_to_input_extrinsics_intrinsics` 가 전처리된 K 를 넣음)로 읽었지만 실행 확인 전이다. `k_for_depth_grid` 가 주점 위치로 가리고 어느 쪽이었는지 기록한다.
- MoGe 정규화 K → 픽셀 K 변환(주점 −0.5)은 utils3d 의 정규화 규약을 가정했다(확인 못 함, 정합 축척·일관성 지표에 드러난다). MoGe 출력은 원본 크기지만 내부 토큰 상한이 있어 `native_px_per_sample` 1.0 은 실제 정보 밀도를 과대평가한다.
- 기준 깊이(MapAnything 518)의 오차가 정합 축척에 들어간다. 축척은 수만 픽셀의 중앙값이라 국소 오차에는 강하지만, MapAnything 의 전체 축척 오차(카메라 높이 2.05 vs 설치 2.6~2.8)는 그대로 물려받는다(World 축척은 별도 근거로 정한다).
- 바닥 rms 는 3 cm 안 인라이어 rms 라 벽 아래 3 cm 점도 들어간다(합성 정확 장면도 0.6 cm). world_frame 과 같은 지표로 비교만 한다.
- 결과 점 수가 많다(1 cm 칸 약 140만 점, glb 약 23 MB). 뷰어에 넣을 때는 칸 크기·LOD 를 다시 정한다.
- 가장자리 지표는 3×3 이웃이 모두 유효한 곳만 본다. MapAnything 은 `mask_edges` 로 가장자리를 지워 기준선 값이 비어 있다(불연속 픽셀 0). DA3 는 신뢰도로 지우지 않아 값이 나온다.

## 넘긴 일

- 다음 GPU 세션: 위 h0~h7(생성기·실행서는 S15P31A101-291, h8 제외), 결과로 지표 표·채택 의견.
- DA3 를 쓰기로 하면: `deploy/weights.json`·`docs/data/weights.md` 에 DA3 가중치(해시·라이선스), 실행 환경(별도 이미지 단계 또는 별도 가상환경) 결정, 잠금 판단(지금은 바꾸지 않음).
- 명세 정리(-172 등): 5.3·20.2 비교군 문구(자세 조건 = 입력 자세 단위, 외부 모델 인터페이스 래퍼는 자세를 고정하지 않음), MoGe-3(flex-gemm) 대 MoGe-2.
- G4: 개발 교실 줄자 실측 점(바닥 모서리·책상 상판)으로 `hybrid_metrics.json` 에 G4 지표를 넣는 단계.
- 표면: TSDF/Poisson·메시 단순화·Scene 버전 연계(명세 5.3 복원 후처리의 뒷부분), 뷰어용 칸 크기·LOD.
- World 고정이 OK 인 MapAnything Run 이 생기면 `--world-alignment` 로 World 출력 확인(지금 w1 은 `AXES_AMBIGUOUS`).
