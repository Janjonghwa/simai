# 실험 이미지(HMR·MapAnything)와 Tier 0 이미지 층 분할

S15P31A101-207. 기획서 §3.12(GPU 실행 환경·저장소: 모델별 Docker 이미지, 저장 위치에는 가중치·데이터·산출물·엔진 캐시만, 최소 사양 Ampere 이후·드라이버 최대 CUDA 13.0 이상,
이미지 다이제스트 = `env_hash`), §20.5(GPU Job Queue: 모델별 환경 격리, LIVE 관제 시간에 EXP 금지, 재현 기록), §20.6(E-GATE-0 재현), §21.5(env_hash).
코드는 `deploy/{tier0,exp02,mapanything}/Dockerfile`, `deploy/docker-bake.hcl`, `deploy/scripts/{simai_bootstrap,gpu_session1,exp_image_check}.py`, `deploy/scripts/check_image.sh`, `deploy/weights.json`,
`ai/experiments/hmr/fs3db_common.py`(MoGe 로컬 파일), `ai/experiments/static_scene/run_mapanything.py`(이미지 경로·env_hash). 시험은 `ai/tests/deploy/test_exp_images.py` 와 기존 `ai/tests/deploy/*`.
S15P31A101-238 이 Detectron2 CUDA 확장을 뺐고(아래 "Detectron2 CUDA 확장 제거"), HMR 의 E-GATE-0 대조 기준을 GPU·torch 판별 기대값으로 바꿨고(`ai/experiments/hmr/compare_egate0.py`),
3차 세션 항목을 정했다. HMR 의 K 입력(보정 K 우선)은 `docs/experiments/EXP-02_design.md` "K 입력", 시험은 `ai/tests/experiments/hmr/test_hmr_k_input.py`.

## 문제

1. HMR(EXP-02, torch 2.5.1+cu124·Detectron2)·MapAnything(EXP-01, torch 2.7.1+cu126)은 볼륨 venv 방식이라 지역·GPU 가 바뀌면(2026-10-20 결정: 그때그때 남은 GPU, 볼륨 없음)
   다시 설치·컴파일해야 하고, cu124·cu126 휠에는 Blackwell(sm_100·sm_120) 커널이 없어 B200·RTX 5090·RTX PRO 6000 에서 돌지 않는다. `deploy/exp02` 는 cu124 골격(Detectron2 sm_90 만)이었다.
2. Tier 0 이미지의 `uv sync` 층이 11.5GB 하나라 Docker Hub 업로드가 단일 연결 0.47MB/s 로 약 4시간 걸렸고, 끊기면 그 층을 처음부터 다시 올렸다. 작은 패키지 하나만 바뀌어도 같은 층을 다시 올린다.
3. GPU 1차 세션에서 이미지에 `nvidia-modelopt` 가 없어 엔진 빌드가 실패했다(-213 이 잠금에 넣음). 새 잠금으로 다시 빌드하고 함께 들어온 `cupy-cuda12x`·`onnxruntime-gpu` 1.24.4(CUDA 12 계열 바이너리)가 CUDA 13 이미지에서 import·modelopt 경로를 깨지 않는지 봐야 한다.

## 정한 것

### Tier 0 층 분할

uv 공식 문서(Docker 통합 안내)의 권장은 "의존성 층(`--no-install-project`)과 프로젝트 층을 나눈다" 이고, 지금 Dockerfile 이 이미 그렇다. 남은 문제는 의존성 층 안의 크기라서,
잠금에서 큰 휠 묶음을 뽑아 묶음마다 다른 층으로 먼저 깐다.

- `layer-lists` 단계: `uv export --locked --no-dev --extra tier0 --no-emit-project`(해시 포함 requirements)에서 이름으로 묶음 목록을 뽑는다. 이 단계는 잠금이 바뀌면 다시 돌지만
  BuildKit 의 `COPY --from` 캐시 키는 **복사하는 파일 내용**이라, 묶음 목록이 같으면 그 아래 큰 층은 캐시 그대로다(같은 층 = 같은 다이제스트 = 레지스트리에 다시 올리지 않음).
- 묶음(바뀌는 빈도가 낮은 순): `cuda`(torch 가 가져오는 CUDA 13 휠 16개), `torch`(torch·triton·torchvision), `trt`(tensorrt-cu13-libs, pypi.nvidia.com), `wheels`(onnxruntime-gpu·cupy-cuda12x·polars-runtime·pyarrow·opencv 2종·scipy·av).
  각 층은 `uv pip install --no-deps --require-hashes`(잠금 해시와 다르면 실패). 나머지 작은 패키지는 `uv sync --locked --no-install-project` 층, simai_ai 코드는 그 위 층.
- 묶음 이름이 잠금에 없으면 빌드를 멈춘다(이름이 바뀐 휠이 조용히 작은 패키지 층으로 들어가 분할이 무너지는 것을 막는다).
- `uv sync` 는 앞 층에 같은 판이 깔려 있으면 다시 받지 않는다(빌드 로그·층 크기로 확인, 아래 "빌드 결과").
- 묶음 순서: CUDA 휠은 torch 판을 올릴 때 함께 바뀌고 TensorRT 는 따로 바뀐다. 어느 쪽을 아래에 둬도 한쪽이 바뀌면 위층을 다시 만든다. torch 쪽이 더 크므로(풀어서 약 5GB) 아래에 둔다.

### 실험 이미지

| | HMR `exp02-fs3db` | MapAnything `mapanything` |
|---|---|---|
| torch | 2.7.1+cu128 | 2.7.1+cu128 |
| Python | 3.11.15(볼륨 venv 와 같음) | 3.12.13(볼륨 venv 가 3.12) |
| 베이스 | CUDA 12.8.1 base(실행·빌드. -238 전에는 Detectron2 빌드 단계만 12.8.1 devel) | CUDA 12.8.1 base |
| 잠금 | `deploy/exp02/requirements.lock.txt`(기존 잠금을 선호해 다시 만듦: torch·CUDA 휠·triton·sympy 만 바뀜) | `deploy/mapanything/requirements.lock.txt`(새로) |
| 잠금 밖 | chumpy 0.70·Detectron2 @a1ce2f9(CUDA 확장 없음)·MoGe @74fbce0 휠(빌드 단계에서 만들어 `--no-deps`) | map-anything @3d10cf7(`--no-deps`) |
| 코드 사본 | Fast-SAM-3D-Body @d72aa36, DINOv3 허브 코드 @6876159 | map-anything @3d10cf7, DINOv2 허브 코드 @7764ea0 |
| 가중치 묶음 | `exp02` | `static` |

- **torch 판.** PyTorch 공식 휠 인덱스(`download.pytorch.org/whl/cu128`, 2026-10-21 확인)의 cu128 판은 2.7.0·2.7.1·2.8.0·2.9.0·2.9.1·2.10.0·2.11.0 이다.
  cu128 은 Blackwell(sm_100·sm_120) 커널이 들어간 첫 CUDA 판이다. 2.7.1 을 골랐다: MapAnything 은 볼륨 venv 와 **같은 torch 판**(CUDA 빌드만 다름)이라 E-GATE-0 와의 차이가 가장 작고,
  HMR 은 2.5.1 에서 Blackwell 을 지원하는 가장 가까운 판이다. 두 이미지가 같은 판이라 확인할 조합이 하나다. 더 새 판(2.8~2.11)은 E-GATE-0 대비 차이만 늘린다.
- **Detectron2 CUDA 확장 제거(S15P31A101-238).** -207 은 Detectron2 CUDA 확장을 CUDA 12.8 devel 빌드 단계에서 6세대(`TORCH_CUDA_ARCH_LIST="8.0;8.6;8.9;9.0;10.0;12.0"`)로
  컴파일했다(423초, devel 베이스 압축 약 5.2GB 받기). 우리 경로(YOLO26x-Pose 검출 + MoGe2 + HMR + 렌더)에서 Detectron2 를 쓰는 곳은 Fast-SAM-3D-Body @d72aa36 의
  `sam_3d_body/visualization/utils.py` 의 `from detectron2.config import LazyConfig`(순수 Python) 하나뿐이다(`tools/build_detector.py` 의 ViTDet 옵션은 쓰지 않는다).
  Detectron2 @a1ce2f9 `setup.py` 는 `CppExtension` 이 기본이고 `(torch.cuda.is_available() and CUDA_HOME) or FORCE_CUDA=1` 일 때만 `CUDAExtension` 을 만든다
  (공식 INSTALL.md 도 GPU 지원 없이 빌드하면 "Detectron2 CUDA Compiler: not available" 로 CPU 추론만 된다고 적는다).
  그래서 빌드 단계를 CUDA base(+ `build-essential`)로 바꾸고 `FORCE_CUDA=0` 으로 빌드한다(GPU 없는 빌드 → CPU 전용 C++ 확장). nvcc·devel 베이스·`TORCH_CUDA_ARCH_LIST`·`.detectron2_arch` 를 뺐다.
  - 노트북 확인(2026-10-22, WSL Ubuntu 22.04, Python 3.11, torch 2.7.1+cpu, 임시 venv 는 끝나고 지움): `FORCE_CUDA=0`·`CUDA_HOME` 없음으로 `pip wheel --no-build-isolation` 76초(8병렬),
    휠 6.3MB, `detectron2._C.has_cuda()` False, `_C*.so` 의 ldd 에 CUDA 라이브러리 없음, `collect_env` "CUDA compiler not available".
    Fast @d72aa36 에서 `parse_pose_metainfo(<mhr70.py 경로>)`(= `LazyConfig.load`) 결과가 dict 입력 결과와 키·값이 같고, `tools.vis_utils` 의 스켈레톤 그리기와
    `visualize_sample_together`(pyrender EGL, WSL mesa) 렌더가 됐다. 공식 렌더 경로는 `pose_info` dict 를 넘겨 `LazyConfig.load` 를 부르지도 않는다(import 만).
  - 노트북에서 못 본 것: 이미지 빌드(cu128 torch 가 깔린 빌드 단계, Docker 금지), GPU EGL 렌더. 3차 세션과 Lead 의 이미지 재빌드·`check_image.sh` 로 본다(아래 "3차 세션 항목").
  - 점검 바꿈: `exp_image_check.py` 의 `detectron2_arch`·`detectron2_kernel`(GPU 위 `box_iou_rotated`) → `detectron2_lazyconfig`(Fast 의 mhr70 메타데이터 70관절을 `LazyConfig.load`, 확장의 CUDA 여부는 기록만).
    `check_image.sh` 는 확장이 CPU 전용(`has_cuda()` False)인지와 같은 LazyConfig 읽기를 이미지 안에서 확인한다(빌드 성질 점검).
  - `deploy/README.md` 의 Detectron2 줄(6세대 CUDA 빌드·`EXP02_CUDA_ARCH`·devel)은 -236 담당 파일이라 고치지 않았고, -243 에서 CPU 전용 빌드(`FORCE_CUDA=0`, base 빌드 단계)에 맞췄다.
- **빌드 단계도 base.** -238 부터 devel 이미지(압축 약 5.2GB)를 받지 않는다. 빌드 단계는 휠(chumpy·Detectron2 CPU 확장·MoGe)만 만들어 실행 이미지로 옮긴다.
  실행 이미지에는 torch.compile(triton) 런처용 `gcc`·`libc6-dev`, pyrender EGL 용 glvnd 를 둔다.
- **허브 코드 고정.** Fast 의 `Dinov3Backbone` 과 MapAnything(uniception) 인코더는 `torch.hub.load("facebookresearch/dinov{2,3}", pretrained=False)` 로 GitHub `main` 을 그때그때 받았다(가중치는 안 받음).
  E-GATE-0(2026-10-16) 이전 마지막 커밋으로 고정해 `~/.cache/torch/hub/facebookresearch_dinov{2,3}_main` 에 넣었다(torch.hub 는 이 캐시가 있으면 받지 않는다).
- **가중치.** 이미지에 넣지 않는다. `simai-bootstrap` 이 이미지 ENV `SIMAI_WEIGHTS_SET` 의 묶음을 받아 sha256 을 확인한다(`docs/data/weights.md` "실험 이미지 가중치").
  `tier0` 명령은 ENV 와 상관없이 `tier0` 묶음만 준비한다. 묶음에 해시가 빈 항목이 있으면 **받기 전에** 멈춘다(지금 `sam-3d-body-dinov3-mhr`). MoGe2 는 Fast 코드가 HF 이름으로 부르므로
  `fs3db_common.use_local_moge()` 가 확인된 로컬 파일로 바꿔 끼운다(MoGe 의 `from_pretrained` 는 있는 경로를 그대로 읽는다).
- **진입점.** 세 이미지 모두 `tini → simai-start(SSH) → simai-bootstrap`. 실험 이미지 기본 명령은 `sleep infinity`(준비 없이 SSH 로 쓰는 개발 Pod), 준비가 필요하면 `simai-bootstrap run <명령>`·`queue`.

### 세션 큐 연결

`gpu_session1.py` 의 g·m 자리를 실제 항목으로 바꿨다. Pod 하나 = 이미지 하나이므로 항목마다 이미지를 정하고, 큐는 지금 이미지(`--image`, 기본 ENV `SIMAI_IMAGE_KIND`)의 항목만 넣는다.
실험 이미지 큐는 LIVE 보호를 켜고(`on_live=stop`), 실험 Run id 는 `<E-GATE-0 이름>_img_<세션>` 이라 E-GATE-0·다른 세션 결과를 덮어쓰지 않는다. 표·순서는 `docs/design/runpod-environment.md` "2차 세션".
실험 Job 은 `deploy/scripts/exp_job.py` 로 감싼다(리뷰 M1·M2·R1·R4): 결과 폴더에 끝난 `metrics.json` 이 있으면 실행하지 않고(종료 코드 4), 끝나면 실험 지표를 스칼라로 줄여
`$SIMAI_RUN_DIR/metrics.json` 으로 넘긴다. Job 기록에는 저장 위치의 **절대 경로**로 가중치(HMR: YOLO26x-pose·sam-3d-body ckpt·mhr·fastcfg config·MoGe2-S, MapAnything: safetensors·config),
입력(`dataset` → `input_provenance.frames.dataset_hash`), 결과(REPORT `metrics.json`, 메시·장면·렌더)를 적는다. 큐 workdir 는 저장소 사본이라 상대 경로면 기록기가 못 찾는다.
여러 파일의 `weights_hash` 는 (경로, 파일 해시) 목록의 해시라 저장 위치가 다르면(`/srv/simai` ↔ `/workspace/simai`) 값이 다르다. 파일별 해시는 `runs/bootstrap/*.json` 에 있다.
실험 이미지 venv 에는 jsonschema 가 없어 gpu_queue 가 Run 기록 검증을 조용히 건너뛰므로, 세션 실행기가 시작 전에 `ai/uv.lock` 판의 jsonschema·referencing 을 컨테이너 디스크(`--target`)에 깔고
그래도 검증기를 못 만들면 시작하지 않는다(종료 코드 5). 이미지 잠금을 바꾸지 않은 이유: 잠금이 바뀌면 실험 이미지를 다시 빌드·업로드해야 하고, 검증은 큐 쪽 도구라 실험 환경(E-GATE-0 대조 대상)에 넣지 않는 편이 깨끗하다.
-212 의 c3 재측정은 c 항목(`session1_live.py all`)에 들어 있어 2차 세션 Tier 0 Pod 에서 함께 돈다.

## E-GATE-0 대조 기준 (torch 판이 바뀌므로)

같은 입력·같은 프리셋으로 새 이미지에서 다시 돌려 아래를 모두 만족하면 "새 이미지가 E-GATE-0 환경을 대신한다" 로 본다. 하나라도 어긋나면 그 Run 을 HOLD 로 두고 원인을 본다(환경 차이인지 입력 차이인지).
지연 비교는 **같은 GPU 모델**(E-GATE-0: HMR H100 80GB HBM3, MapAnything A40)일 때만 판정에 쓰고, 다른 GPU 면 기록만 한다(§20.5 "특정 GPU 수치는 실측 없이 확정하지 않는다").
HMR 은 S15P31A101-238 부터 지연 기대값을 **(GPU 모델, torch 판)** 별로 둔다(아래 "HMR 지연 기대값"). 구조(SUCCESS 수·그림별 n_people·finite, H6 people_detected)는 GPU·판과 상관없이 정확히 같아야 한다.
HMR 판정은 노트북에서 `python ai/experiments/hmr/compare_egate0.py --base <E-GATE-0 Run>/metrics.json --new <새 Run>/metrics.json`(종료 0 = PASS·PASS_STRUCTURE_ONLY, 1 = HOLD).

| 대상 (E-GATE-0 Run → 새 Run) | 같아야 하는 값 | 허용 |
|---|---|---|
| H0 스모크 → `H0_smoke_img_<세션>` | `result` SUCCESS, `n_people` 1, `finite` true, 적재 4구성요소 모두 OK(기준 Run 의 구성요소가 하나씩 있고 OK. 빠짐·이름 바뀜·중복은 HOLD) | 정확히 같음 |
| H1 공식 14장 → `H1_official_opt_img_<세션>` | 14장 모두 SUCCESS·`finite` true, 그림별 `n_people`(1×11, 5, 4, 4) | 정확히 같음 |
| | warm 그림(같은 크기·같은 인원 수가 앞에서 나온 그림, 14장 중 7장)의 `process_one_image_ms` 중앙값 | 같은 (GPU, torch 판) 기대값 ±25%, 표에 없으면 기록만 (-238 전: 첫 장·첫 다인원만 뺀 12장 중앙값을 E-GATE-0 356 ms 와) |
| | `peak_alloc_mb`(그림별 최대) | E-GATE-0 ±10% (torch 2.5→2.7 할당기 차이 여유). 값이 없으면 HOLD |
| H6 검출 → `H6_det_yolo26x_img_<세션>` | 네 줄(imgsz 640·1280 × batch 1·4)의 `people_detected` 5 | 정확히 같음 |
| | 줄별 `ms.p50` | 같은 (GPU, torch 판) 기대값 ±20%, 표에 없으면 기록만 |
| C1(map-anything) → `C1_room4_bf16_img_<세션>` | `compare_runs.py --base C1_room4_bf16` 의 `cam_dist_ratio_vs_base` | 0.99~1.01 |
| | `rot_diff_vs_base_max_deg` | ≤ 0.2° |
| | 시점별 `depth_p50_m`, `metric_scaling_factor` | 각각 ±1% |
| | 시점별 `fov_x_deg` | ±0.3° |
| C2(apache) → `C2_room4_apache_bf16_img_<세션>` | `--base C2_room4_apache_bf16` 로 같은 4개 | 같은 허용 |

허용 폭 근거: MapAnything 은 E-GATE-0 안에서 같은 환경의 bf16 ↔ fp32(C1 ↔ C3) 차이가 거리 비 0.998, 회전 0.07°, 깊이 중앙값 0.13~0.17%, 척도 0.08%, FOV 0.02° 였다.
CUDA 빌드(cu126 → cu128)와 cuDNN 판(9.5 → 9.7) 차이는 이 정밀도 차이보다 작아야 하므로 그 약 2~6배를 허용으로 잡았다. HMR 은 E-GATE-0 메시 파일이 노트북에 없어(볼륨 폐기) 메시 수치는 비교하지 못하고,
결과 구조·인원 수·유한성·지연으로 본다. 렌더 패널(`panels/`)은 사람이 E-GATE-0 MR 의 그림과 눈으로 비교한다.
MapAnything 비교는 노트북에서 한다: 결과 묶음의 `runs/exp01/*_img_<세션>` 과 E-GATE-0 Run 폴더(`ai/experiments/static_scene/runs/`)를 한 폴더에 두고 `SIMAI_ROOT=<그 폴더의 상위> uv run --project ai python ai/experiments/static_scene/compare_runs.py --base C1_room4_bf16 C1_room4_bf16_img`.

### 2차 세션 HMR HOLD 원인과 새 기준 (S15P31A101-238)

2차 세션(2026-10-22, AP-JP-1, 이미지 `exp02-fs3db` `sha256:da77b601…`)의 H0·H1·H6 은 구조가 E-GATE-0 와 같았다(14/14 SUCCESS, finite, n_people 1×11·5·4·4, 투영 차이 0.0 px, H6 네 줄 5명, peak_alloc 최대 4,827.0 vs 4,828.1 MB).
지연만 빨라 위 ±25%·±20% 밖이라 HOLD 였다.

| | E-GATE-0 (2026-10-16) | 2차 세션 (2026-10-22) | 비 |
|---|---|---|---|
| GPU | H100 80GB HBM3, 700 W, P0, 1980 MHz | 같음 | |
| 소프트웨어 | 볼륨 micromamba venv, torch 2.5.1+cu124, ultralytics 8.4.178 | 이미지, torch 2.7.1+cu128, ultralytics 8.4.182, 드라이버 595.91.07 | |
| H1 12장 중앙값(옛 기준) | 355.7 ms | 232.1 ms | 0.65 |
| H1 warm 7장 중앙값(새 기준) | 330.84 ms | 219.73 ms | 0.66 |
| H1 warm HMR 단계 중앙값 | 249.47 ms | 164.69 ms | 0.66 |
| H6 640×1·640×4·1280×1·1280×4 p50 | 15.38·21.47·18.40·38.15 ms | 8.61·12.11·10.26·30.45 ms | 0.56~0.80 |
| 다인원 첫 실행(5명·4명) | 2,708·2,904 ms | 2,608·2,660 ms | 0.92~0.96 |
| H0 첫 실행(적재 포함) | 20,157 ms | 4,064 ms | |

- **같은 GPU 모델이다.** 이름·전력 한도·클럭이 같다. E-GATE-0 Run 에는 드라이버·CPU·cuDNN 기록이 없어 호스트가 같은지는 확인할 수 없다.
- **유력한 설명은 소프트웨어 판(+ 호스트일 수 있음). 통제 실험으로 가른 인과는 아니다**(E-GATE-0 에 CPU·드라이버·cuDNN 기록이 없어 기여도를 나눌 수 없다). torch 2.5.1+cu124 → 2.7.1+cu128 은 CUDA 런타임·cuBLAS·cuDNN 휠과 SDPA 커널이 함께 바뀐 판이다. 검출(YOLO)·HMR·FOV 세 단계가 모두
  비슷한 비율(0.56~0.8)로 빨라졌고 GPU 밖 CPU 렌더(`render_ms` 726 → 648 ms, 첫 장 5,347 → 1,180 ms)도 빨라져 호스트 CPU 차이도 섞였을 수 있다. 결과가 빨라진 쪽이고 구조·메모리는 같으므로 품질 회귀가 아니다.
- **측정 방식.** run_image 는 그림마다 한 번씩만 돌고(워밍업 없음) 처음 보는 입력 모양에서 커널 선택이 섞인다(검출 200~370 ms 튐: 610x840·1280x720·512x512 첫 장).
  옛 기준은 첫 장·첫 다인원만 빼서 4명 첫 실행(t3_multi_f200, 2.6~2.9 s)이 12장에 남았다. 새 기준은 "같은 크기·같은 인원 수가 앞에서 나온 그림"만 본다(`compare_egate0.warm_rows`).
  2차 세션의 "다인원 첫 실행 2.66 s" 는 E-GATE-0 에도 같은 크기로 있었다(첫 실행 비용, 회귀 아님).
- **이후 기록.** `fs3db_common.env_summary` 가 cuDNN 판·드라이버·CPU 모델·이 프로세스가 쓸 수 있는 CPU 수(`sched_getaffinity` 기준, cgroup quota 는 읽지 않음)·이미지 다이제스트(`SIMAI_IMAGE_DIGEST`)를 남긴다. 다음 비교부터 호스트 차이를 가를 수 있다.

**HMR 지연 기대값** (`compare_egate0.EXPECTED`. 조합마다 측정 Run 하나에서 나온 값이고, 같은 조합의 Run 이 쌓이면 근거와 함께 고친다):

| GPU | torch 판(환경) | H1 warm p50 | H6 p50 640×1·640×4·1280×1·1280×4 | 근거 |
|---|---|---|---|---|
| H100 80GB HBM3 | 2.5.1+cu124 (볼륨 venv) | 330.84 ms | 15.38·21.47·18.40·38.15 ms | E-GATE-0 H1_official_opt·H6_det_yolo26x |
| H100 80GB HBM3 | 2.7.1+cu128 (exp02-fs3db 이미지) | 219.73 ms | 8.61·12.11·10.26·30.45 ms | 2차 세션 `*_img_20261022-s2-exp02` |
| 그 밖의 GPU·판 | | 기록만 | 기록만 | §20.5 |

- 허용 폭(±25%·±20%)은 그대로다. 한 Run 안의 warm 7장 값이 140~463 ms 로 흩어져 있어 더 좁히면 같은 환경에서도 HOLD 가 날 수 있다.
- 키를 이미지 다이제스트가 아니라 torch 판으로 둔 이유: -238 처럼 지연과 무관한 층(Detectron2 확장)만 바뀌어도 다이제스트가 바뀐다. torch·CUDA 판이 바뀌면 측정으로 새 줄을 더한다.
- 2차 세션 결과를 이 기준으로 다시 보면 H0 PASS_STRUCTURE_ONLY(warm 없음), H1·H6 PASS 다(기대값의 근거가 그 Run 이라 자기 확인일 뿐이고, 판정 의미는 3차 세션부터다).

### 3차 세션 항목 (S15P31A101-238, exp02 Pod)

Lead 가 이 MR 의 Dockerfile 로 이미지를 다시 빌드한 뒤 같은 `simai-bootstrap queue` 로 돈다. 세션 큐(`deploy/scripts/gpu_session1.py`)의 g4 항목은 -243 이 넣었다(아래 "g4 큐 연결").

| 키 | 확인 | 명령(저장 위치 `$R` = `/srv/simai` 등) | 기대 |
|---|---|---|---|
| (빌드 뒤) | 무확장 이미지 내용 | `bash deploy/scripts/check_image.sh <이미지>` (Lead, 노트북) | "import·코드 사본 (exp02: … cpu-only …)" PASS, 빌드 로그에 devel 베이스·nvcc 없음 |
| g0 | 무확장 이미지 GPU 점검 | `python deploy/scripts/exp_image_check.py` | `detectron2_lazyconfig` ok(CUDA 확장이 든 옛 이미지면 실패·종료 1), `pyrender_egl` ok, 종료 0 |
| g1·g2·g3 | E-GATE-0 재현(항목 그대로) | 기존 항목. 판정은 `compare_egate0.py --base <E-GATE-0 Run>` | 구조 PASS. 같은 torch 판이라 지연은 H100·2.7.1+cu128 기대값 ±25%·±20%. 렌더 패널은 사람이 2차 세션 것과 비교(확장 제거가 렌더를 바꾸지 않음) |
| g4 | 보정 K 입력 경로 | `simai-exp02 python ai/experiments/hmr/run_image.py --run-id H7_calib_k_img_<세션> --images $R/datasets/classroom-stills-20261021 --mode official --preset opt --calibration $R/datasets/classroom-k/intrinsics-20261021-022940z.json` | 20장 모두 행의 `k_input.source` CALIBRATION(G1_AUTO·PROVISIONAL)·`undistorted` true·`image_space` UNDISTORTED_ALPHA1, 최상위 `k_input.count` {CALIBRATION: 20}, 적재 목록에 `fov_moge2` 없음, `k_focal_diff_px`(모델 초점 − `K_used` fx) < 0.5, `K_used` 주점이 그림 중심 (W/2, H/2) 에서 1 px 안, `reproj_err_px` ≤ 0.75(sam-3d-body 는 주점 대신 그림 중심을 쓰고 펴기는 주점을 (W−1)/2·(H−1)/2 에 둬서 √0.5 ≈ 0.7071 이 남는다. 3차 세션 u2 20장 모두 0.7071, S15P31A101-279), finite. 사람 없는 그림(NO_PERSON)은 초점 비교 없이 통과. 사람 수는 기록만(정답 없음. 노트북 yolo11m-pose 로 원본에서 93명) |

- g4 입력: 노트북 `ai/experiments/recordings/classroom-stills-20261021/`(cam_A~D × 5장, 2304×1296, 커밋하지 않음)과 교실 보정 기록
  `ai/experiments/recordings/intrinsics/intrinsics-20261021-022940z.json`(AnyCalib, 4대 OK, 직선성 UNVERIFIED)을 Pod 로 올린다. 카메라는 파일 이름 접두(`cam_A_01` → `cam_A`)로 고른다.
  수업 중 우연히 찍힌 사람이 있을 수 있어 내부 확인용이며 결과 렌더를 공개·시연에 쓰지 않는다. Run 기록 `input_provenance.frames` 는 FILE, `dataset_id` classroom-stills-20261021.
- g4 Job 기록: `weights` 에서 MoGe2 를 빼고 보정 기록 파일을 `dataset` 에 함께 적는다(보정 K 가 입력이므로 해시가 Run 기록에 남는다). `input_provenance.calibration` 은
  {source G1_AUTO, calibration_version `intrinsics-20261021-022940z.json@sha256:<파일 해시>`}(metrics 의 `k_input.input_provenance` 와 같은 값), `is_upper_bound` false. g4 는 적재 구성이 달라(MoGe 없음) `compare_egate0.py` 대조 대상이 아니다. 예상 3분(2차 세션 g3 실측 58.3초에서 추정, `docs/design/runpod-environment.md` "예상 시간 근거"), 제한 30분.
  펴기(`undistort_ms`, CPU)는 그림당 수십 ms 로 `process_one_image_ms` 밖이다.
- **g4 큐 연결 (S15P31A101-243).** 항목 `g4_exp02_h7_calib_k`(Jira -238, Run id `H7_calib_k_img_<세션>`). `input_provenance.calibration` 은 큐를 만들 때(Pod 에 입력이 올라간 뒤
  `simai-bootstrap queue`) 보정 기록 파일을 읽어 위 규칙으로 채우고, 파일이 없거나 형식(`simai.intrinsics_estimate`)이 다르면 비운다(지어내지 않는다). 그러면 run_image 도 MoGe 로 물러서므로 판정이 실패한다.
  판정은 `exp_job.py --check ai/experiments/hmr/check_calib_k.py`(위 표의 기대 + metrics 의 `k_input.input_provenance` 가 Job 기록과 같은지)이고, 어긋나면 Job 이 FAILED(종료 1)이며
  Run 기록 지표에 `check.passed` 가 남는다. 입력은 `gpu_session1.py prepare` 가 노트북 recordings 에서 `--out`(저장소 밖이어야 한다. 안이면 사진을 넣지 않는다)에 `g4-inputs.tar` 로 묶고,
  출력하는 scp·tar 명령으로 Pod 의 `$R/datasets/` 에 푼다. 노트북에서 실제 보정 기록으로 계산한 Job 기록 값은 fs3db_common `k_summary` 의 값과 같았다(20장 모두 CALIBRATION).
- 선택: 같은 사진을 `--calibration` 없이 한 번 더 돌리면(MoGe 추정) 두 K 의 HMR 결과 차이를 본다. MoGe 계열 추정(70~73°)과 보정(약 85°)이 어긋난다는 -192 관찰을 HMR 쪽에서 확인하는 용도다.

## 빌드 결과 (노트북, 2026-10-21)

노트북(Docker Desktop 29.6.1, 엔진 VM 메모리 2GB·CPU 22, 내려받기 약 3MB/s). 크기 "압축" 은 `docker image inspect .Size`(containerd 이미지 저장소의 내용 크기 = 레지스트리에 올라가는 압축 크기, dev da68f00 Tier 0 이미지에서 7.48GB 로 기존 "약 7.4GB" 와 맞음), "풀면" 은 `docker images` 크기다.
다이제스트는 2026-10-21 push 뒤 `docker buildx imagetools inspect` 로 얻은 값이다. Docker Hub 무료 플랜은 비공개 저장소가 하나라 세 이미지를 비공개 저장소 `docker.io/jonghwa1492/tier0` 에 태그로 함께 올렸다(실험 이미지 둘은 CUDA·torch 층을 공유해 한 번만 올라갔다). 세 이미지는 커밋 8e1675e 트리에서 빌드했고(OCI revision 라벨 8e1675e), 리뷰 수정 커밋 3926a87 과 이미지에 들어가는 파일이 같아 태그를 3926a8772649 로 붙였다. 이미지 안 `deploy/weights.json` 은 mhr 해시를 채우기 전 판이다(docs/data/weights.md).

| 이미지(로컬 태그) | 압축 / 풀면 | 큰 층(풀린 크기) | 빌드 시간 | `check_image.sh` | 다이제스트 |
|---|---|---|---|---|---|
| `simai/tier0:207-b` | 7.92GB / 21GB | trt 4.65GB, cuda 3.35GB, torch 2.21GB, wheels 1.63GB, 나머지(uv sync, 77개) 591MB | 처음 5,396초(받기가 대부분: cuda 층 3,342초·trt 층 1,360초), 캐시 뒤 38초 | 모두 통과(16) | `jonghwa1492/tier0:3926a8772649` `sha256:7fe5811585ffea1c69cd027b2fe0fe2e8b329435cb13eb81032826f3f48b6163` |
| `simai/exp02-fs3db:207-a` | 4.72GB / 13.9GB | cuda 4.09GB, torch 2.58GB, 잠금 나머지 1.34GB, apt(EGL·gcc·git) 546MB, CUDA base 204MB | Detectron2 6세대 컴파일 423초(MAX_JOBS=2), 빌드 단계 잠금 설치 2,051초(받기). 받기 실패·Docker 종료로 4번 다시 돌렸다(아래) | 모두 통과(12) | `jonghwa1492/tier0:exp02-fs3db-3926a8772649` `sha256:da77b601689973861cd5caf049fa67be36e0c11763987061c67df814bc2a6e28` |
| `simai/mapanything:207-a` | 4.63GB / 13.6GB | cuda 4.09GB, torch 2.58GB, 잠금 나머지 1.30GB, 허브·코드 등 453MB, CUDA base 204MB | 806초(HMR 과 같은 CUDA 휠은 uv 캐시) | 모두 통과(11) | `jonghwa1492/tier0:mapanything-3926a8772649` `sha256:f171420cfaed7182185f0ccbf977a8e46492915d21790da0d1dedbec3129d55f` |

**층 분할 효과(Tier 0, 실제 재빌드 후 층 diffID 비교).**

| 바꾼 것 | 다시 만든 층(풀린 크기 합) | 전에는 |
|---|---|---|
| `ai/src` 한 줄(주석) | src·services·contracts COPY, 프로젝트 `uv sync`, 진입점 스크립트들 ≈ 8.5MB (빌드 7초) | 같음(≈ 8MB): 코드 층은 이미 나뉘어 있었다 |
| 작은 패키지 하나(`tqdm` 4.70.1 → 4.67.1 로 잠금만 바꿈) | 잠금 COPY + 나머지 의존성 층 590MB + 위 코드 층 ≈ 600MB (빌드 172초, 받기 없음) | 11.5GB 한 층 전체 |
| 큰 묶음의 휠 하나(예: TensorRT) | 그 묶음 층부터 위 전부(trt 이면 4.65 + 1.63 + 0.59GB) | 11.5GB |

- 큰 층 넷(cuda·torch·trt·wheels)은 서로 다른 층이라 레지스트리에 병렬로 올라가고, 끊겨도 끝난 층은 다시 올리지 않는다. 가장 큰 층은 TensorRT libs 휠 하나(풀어서 4.65GB)라 휠 단위로는 더 나눌 수 없다.
- 반례로 본 것: 처음 빌드(207-a)와 그 뒤 빌드(207-b)는 trt 층부터 diffID 가 달랐다. 두 빌드 사이에 HMR 빌드가 같은 BuildKit 캐시를 크게 쓰면서 207-a 의 trt 이후 캐시 기록이 없어졌고(빌드 로그 `DONE` 다시 실행), 다시 만든 층은 파일 시각이 달라 내용이 같아도 다이제스트가 다르다.
  즉 층 재사용은 **같은 빌드 기계의 BuildKit 캐시가 남아 있을 때만**이다. 다른 기계·캐시 정리 뒤에는 모든 층을 다시 올린다(잔여 위험).
- Tier 0 크기 19.6 → 21GB(압축 7.48 → 7.92GB)의 원인(site-packages 비교, da68f00 → 207): onnxruntime-gpu +404MB, cupy(cupy·cupy_backends·cupyx) +293MB, onnx 31 → 80MB(+49), pulp +36MB, nvidia-modelopt +17MB, lief +12MB 등 합 +907MB. 모두 -213 의 `nvidia-modelopt[onnx]` 가 끌어온 것이다.
  - `onnxruntime-gpu` 1.24.4 의 CUDA·TensorRT 실행 공급자(`libonnxruntime_providers_cuda.so`)는 `libcudart.so.12`·`libcublas.so.12`·`libcufft.so.11`·`libcurand.so.10` 을 찾는데 CUDA 13 이미지에는 없다(ldd "not found"). 그래서 이 이미지에서 ORT 는 CPU 로만 돈다. `import onnxruntime` 과 공급자 목록은 정상이다.
  - `cupy-cuda12x` 는 import 는 되고 `modelopt.onnx.autocast` 를 import 할 때 함께 import 된다. GPU 에서 쓰면 CUDA 12 런타임이 없어 실패할 것이다(노트북 컨테이너는 GPU 를 못 써서 확인 못 함). cupy 를 직접 쓰는 곳은 modelopt 의 INT4 양자화(`modelopt/onnx/quantization/int4.py`)뿐이고 FP16 AutoCast 경로가 아니다.
  - 줄일 여지(이 MR 에서는 바꾸지 않음, 잠금 변경이라 -213 판단 필요): ORT 를 CPU 판(`onnxruntime`)으로 바꾸면 약 0.35GB, cupy 를 빼면 약 0.3GB. 둘 다 modelopt 의 의존성 선언이라 `override-dependencies` 가 필요하고, 엔진 빌드가 1차 세션과 같은지 Pod 에서 다시 봐야 한다.
    → S15P31A101-246 에서 반영(override 로 둘 다 빼고 CPU `onnxruntime`, 근거·AutoCast CPU 재판정은 `docs/design/unified-env.md` "2단계").
- 노트북 GPU 확인(`--gpus all`)과 `engine_cache ensure` 는 하지 못했다: Docker Desktop 의 GPU 연결이 `nvidia-container-cli: load library failed: libnvidia-ml.so.1` 로 컨테이너를 띄우지 못한다(호스트 드라이버·WSL GPU 설정 문제로 보이며 이 티켓 범위 밖). 그래서 GPU 쪽은 2차 세션 b·g0·m0 로 넘긴다.
- CUDA 커널 확인(GPU 없이, devel 이미지의 `cuobjdump --list-elf/--list-ptx`): Detectron2 `_C` 는 sm_80·86·89·90·100·120 SASS 각 6개, PTX 없음. torch 2.7.1+cu128 `libtorch_cuda.so` 는 sm_75·80·86·90·100·120 SASS + PTX sm_120.
- HMR 이미지를 만들며 고친 것(모두 `check_image.sh` 가 찾았다): (1) 잠금의 MoGe 의존성 두 개가 git 주소라 실행 단계에 git 이 필요했다(첫 빌드 실패). (2) Fast-SAM-3D-Body 저장소에 mhr2smpl 실험 가중치 `.pth` 3개(256KB 초과)가 커밋돼 있어 이미지에 들어갔다 → 빌드 단계에서 지우고 지운 목록을 로그에 남긴다(우리 Run 은 mhr2smpl 을 쓰지 않는다). (3) pyrender import 가 pyglet → X11 `libXrender` 를 열다 실패 → `libxrender1·libxext6·libsm6` 추가. (4) chumpy 0.70 은 Python 3.11 에서 import 때 `inspect.getargspec` 으로 깨진다(볼륨 venv 도 3.11 이라 같다, smplx 가 SMPL pkl 을 읽을 때만 쓴다) → 점검은 설치 판만 본다.
- HMR 빌드 벽시계 시간은 1시간을 넘었다(첫 빌드의 받기 2,051초 + 실패·Docker 종료로 다시 돌린 시간). 받기를 뺀 빌드 단계(컴파일 423초, 층 설치·내보내기 수 분)는 짧다. 다시 빌드할 때는 BuildKit uv 캐시가 남아 있으면 수 분이다.

## 버린 대안

- **Tier 0 의존성을 한 층으로 두고 레지스트리 업로드 병렬화만 바꾸기**: 한 층은 병렬로 나뉘지 않고, 끊기면 처음부터 다시 올린다. 작은 패키지 변경도 11.5GB 를 다시 올린다.
- **uv `--no-install-package` 로 큰 휠을 빼고 두 번 sync**: "작은 것만 먼저" 는 되지만 "큰 것만 먼저" 는 빠질 패키지(90개 남짓)를 모두 나열해야 한다. 그리고 sync 단계는 `uv.lock` 에 묶여 잠금이 바뀌면 큰 층도 다시 만든다.
  잠금에서 묶음 목록(해시 포함)만 뽑아 `COPY --from` 으로 넘기면 목록이 같을 때 큰 층이 캐시에 남는다.
- **큰 휠 목록을 저장소에 따로 커밋**: 잠금과 어긋날 수 있다. 빌드 안에서 잠금에서 뽑고 이름이 없으면 멈추는 쪽이 한 정본(잠금)을 지킨다.
- **필요 없는 CUDA 라이브러리 지우기**: torch·TensorRT·onnxruntime 의 dlopen 경로를 ldd·import 로 다 확인하기 전에는 지우지 않았다(근거 없음, 엔진 빌드 경로가 깨지면 Pod 에서야 드러난다).
- **실험 이미지를 devel 베이스 그대로**: 이미지가 압축 5GB 넘게 커지고 0.47MB/s 업로드에서 약 3시간이 더 든다.
- **torch 2.8 이상**: Blackwell 은 2.7 부터 되므로 E-GATE-0 와의 차이만 늘린다.
- **MoGe2 를 HF 캐시 구조(`models--*/snapshots/<rev>`)로 미리 채우고 오프라인 모드**: 부트스트랩에 HF 캐시 규칙을 들여야 한다. 실험 코드에서 로컬 경로를 넘기는 쪽이 작다.
- **`mhr_model.pt` 해시를 첫 다운로드 값으로 받아들이기**: "모르면 비운다" 에 어긋난다. 비워 두고 받기 전에 멈춘다.

## 잔여 위험

- (-238) HMR 지연 기대값은 조합마다 Run 하나에서 나왔다. 호스트 CPU 가 다른 Pod 가 걸리면 같은 GPU·판이어도 ±25% 를 넘을 수 있다(그때는 env 의 cpu·driver 를 보고 새 근거로 줄을 더한다).
- (-238) Detectron2 무확장 빌드는 노트북 WSL(torch CPU 판)에서만 확인했다. cu128 torch 가 깔린 빌드 단계에서도 GPU 가 없으니 CPU 확장이 되는 것이 setup.py 조건이지만, 실제 이미지 빌드(Lead)와 `check_image.sh` 로 확인한다.

- `sam-3d-body-dinov3-mhr` 해시가 비어 있어 HMR 이미지는 해시를 채우기 전에는 가중치 준비에서 멈춘다(승인 계정 토큰으로 `docs/data/weights.md` 명령).
- 노트북 Docker 에서 GPU 를 쓰지 못해 세 이미지 모두 GPU 실행은 하지 않았다(import·CUDA 커널 목록은 GPU 없이 확인). Blackwell(sm_100·sm_120)에서 실제로 도는지는 2차 세션 g0·m0 를 그 GPU 에서 돌려야 확인된다.
- MapAnything 잠금은 새로 풀었다(볼륨 venv 잠금 사본이 노트북에 없음). huggingface-hub 2.x 등 E-GATE-0 와 다른 판이 있을 수 있어 m1·m2 대조로 확인한다.
- MoGe 의 flex-gemm 을 뺀 상태(볼륨 환경과 같음)라 `uv pip check` 는 실패한다. import 점검으로 대신한다.
- 실험 이미지끼리·Tier 0 와는 Python·CUDA 가 달라 층을 나누지 않는다(CUDA base 층만 같다).
- 층 재사용은 같은 빌드 기계의 BuildKit 캐시가 남아 있을 때만이다. 다른 기계에서 빌드하거나 캐시를 지우면 내용이 같아도 층 다이제스트가 달라져 전부 다시 올린다
  (재현 가능한 층 시각 `SOURCE_DATE_EPOCH`·`rewrite-timestamp` 는 docker 드라이버에서 확인하지 않아 넣지 않았다).
  이 노트북에서 캐시가 계속 지워진 원인은 Docker Desktop 빌더 GC 정책이다(`docker buildx inspect`: cache mount 2.76GiB, 빌드 캐시 약 20GiB). 큰 빌드를 하나 더 하면
  앞 이미지의 큰 층 기록과 uv 캐시가 지워져 다시 받고 다시 만든다(커밋 뒤 최종 태그 빌드도 torch 층부터 다시 받았다). 효과를 얻으려면 Docker Engine 설정의
  `builder.gc.defaultKeepStorage` 를 100GB 이상으로 올려야 한다(사용자 설정이라 바꾸지 않았다).
- Tier 0 이미지의 onnxruntime-gpu·cupy 는 CUDA 12 런타임을 찾아 이 이미지에서 GPU 로는 돌지 않는다(위 "빌드 결과"). FP16 엔진 빌드는 1차 세션 Pod 에서 같은 잠금 조합으로 통과했으므로(-213) 2차 세션 b 로 다시 본다.
- 노트북 Docker 에서 GPU 를 쓰지 못해 세 이미지 모두 GPU 실행은 확인하지 않았다(2차 세션 b·g0·m0).
