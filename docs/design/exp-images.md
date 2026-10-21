# 실험 이미지(HMR·MapAnything)와 Tier 0 이미지 층 분할

S15P31A101-207. 기획서 §3.12(GPU 실행 환경·저장소: 모델별 Docker 이미지, 저장 위치에는 가중치·데이터·산출물·엔진 캐시만, 최소 사양 Ampere 이후·드라이버 최대 CUDA 13.0 이상,
이미지 다이제스트 = `env_hash`), §20.5(GPU Job Queue: 모델별 환경 격리, LIVE 관제 시간에 EXP 금지, 재현 기록), §20.6(E-GATE-0 재현), §21.5(env_hash).
코드는 `deploy/{tier0,exp02,mapanything}/Dockerfile`, `deploy/docker-bake.hcl`, `deploy/scripts/{simai_bootstrap,gpu_session1,exp_image_check}.py`, `deploy/scripts/check_image.sh`, `deploy/weights.json`,
`ai/experiments/hmr/fs3db_common.py`(MoGe 로컬 파일), `ai/experiments/static_scene/run_mapanything.py`(이미지 경로·env_hash). 시험은 `ai/tests/deploy/test_exp_images.py` 와 기존 `ai/tests/deploy/*`.

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
| 베이스 | CUDA 12.8.1 base(실행), 12.8.1 devel(Detectron2 빌드 단계만) | CUDA 12.8.1 base |
| 잠금 | `deploy/exp02/requirements.lock.txt`(기존 잠금을 선호해 다시 만듦: torch·CUDA 휠·triton·sympy 만 바뀜) | `deploy/mapanything/requirements.lock.txt`(새로) |
| 잠금 밖 | chumpy 0.70·Detectron2 @a1ce2f9·MoGe @74fbce0 휠(빌드 단계에서 만들어 `--no-deps`) | map-anything @3d10cf7(`--no-deps`) |
| 코드 사본 | Fast-SAM-3D-Body @d72aa36, DINOv3 허브 코드 @6876159 | map-anything @3d10cf7, DINOv2 허브 코드 @7764ea0 |
| 가중치 묶음 | `exp02` | `static` |

- **torch 판.** PyTorch 공식 휠 인덱스(`download.pytorch.org/whl/cu128`, 2026-10-21 확인)의 cu128 판은 2.7.0·2.7.1·2.8.0·2.9.0·2.9.1·2.10.0·2.11.0 이다.
  cu128 은 Blackwell(sm_100·sm_120) 커널이 들어간 첫 CUDA 판이다. 2.7.1 을 골랐다: MapAnything 은 볼륨 venv 와 **같은 torch 판**(CUDA 빌드만 다름)이라 E-GATE-0 와의 차이가 가장 작고,
  HMR 은 2.5.1 에서 Blackwell 을 지원하는 가장 가까운 판이다. 두 이미지가 같은 판이라 확인할 조합이 하나다. 더 새 판(2.8~2.11)은 E-GATE-0 대비 차이만 늘린다.
- **Detectron2 아키텍처.** `TORCH_CUDA_ARCH_LIST="8.0;8.6;8.9;9.0;10.0;12.0"`(A100, A40·A10·3090, L4·L40S·4090, H100·H200, B200, RTX 5090·PRO 6000). CUDA 의 SASS 호환 규칙(X.y 로 만든 cubin 은 같은 주 버전 X 의 X.z, z ≥ y 에서 돈다)으로
  8.7·10.3(B300)·12.1(GB10)도 덮는다. **+PTX 는 넣지 않았다**: PTX 는 이 목록 밖의 더 높은 주 버전에서만 쓸모가 있는데, 그런 GPU 에서는 torch 2.7.1 휠 자체에 커널이 없어 어차피 돌지 않는다. PTX 를 넣으면 확장 크기와 빌드 시간만 늘어난다.
- **devel 은 빌드 단계만.** CUDA 12.8 devel 이미지는 압축 약 5.2GB 다. Detectron2 확장을 빌드 단계에서 휠로 만들어 실행 이미지(base)에 옮기므로 nvcc·헤더 층을 올리지 않는다.
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

| 대상 (E-GATE-0 Run → 새 Run) | 같아야 하는 값 | 허용 |
|---|---|---|
| H0 스모크 → `H0_smoke_img_<세션>` | `result` SUCCESS, `n_people` 1, `finite` true, 적재 4구성요소 모두 OK | 정확히 같음 |
| H1 공식 14장 → `H1_official_opt_img_<세션>` | 14장 모두 SUCCESS·`finite` true, 그림별 `n_people`(1×11, 5, 4, 4) | 정확히 같음 |
| | 그림별 `process_one_image_ms`(첫 장·첫 다인원 장은 컴파일·적재가 섞여 제외)의 중앙값 | 같은 GPU 에서 ±25% (E-GATE-0 나머지 12장 중앙값 356 ms) |
| | `peak_alloc_mb`(그림별 최대) | ±10% (torch 2.5→2.7 할당기 차이 여유) |
| H6 검출 → `H6_det_yolo26x_img_<세션>` | 네 줄(imgsz 640·1280 × batch 1·4)의 `people_detected` 5 | 정확히 같음 |
| | `ms_per_frame_p50` | 같은 GPU 에서 ±20% (E-GATE-0 15.4·21.5·18.4·38.2 ms) |
| C1(map-anything) → `C1_room4_bf16_img_<세션>` | `compare_runs.py --base C1_room4_bf16` 의 `cam_dist_ratio_vs_base` | 0.99~1.01 |
| | `rot_diff_vs_base_max_deg` | ≤ 0.2° |
| | 시점별 `depth_p50_m`, `metric_scaling_factor` | 각각 ±1% |
| | 시점별 `fov_x_deg` | ±0.3° |
| C2(apache) → `C2_room4_apache_bf16_img_<세션>` | `--base C2_room4_apache_bf16` 로 같은 4개 | 같은 허용 |

허용 폭 근거: MapAnything 은 E-GATE-0 안에서 같은 환경의 bf16 ↔ fp32(C1 ↔ C3) 차이가 거리 비 0.998, 회전 0.07°, 깊이 중앙값 0.13~0.17%, 척도 0.08%, FOV 0.02° 였다.
CUDA 빌드(cu126 → cu128)와 cuDNN 판(9.5 → 9.7) 차이는 이 정밀도 차이보다 작아야 하므로 그 약 2~6배를 허용으로 잡았다. HMR 은 E-GATE-0 메시 파일이 노트북에 없어(볼륨 폐기) 메시 수치는 비교하지 못하고,
결과 구조·인원 수·유한성·지연으로 본다. 렌더 패널(`panels/`)은 사람이 E-GATE-0 MR 의 그림과 눈으로 비교한다.
MapAnything 비교는 노트북에서 한다: 결과 묶음의 `runs/exp01/*_img_<세션>` 과 E-GATE-0 Run 폴더(`ai/experiments/static_scene/runs/`)를 한 폴더에 두고 `SIMAI_ROOT=<그 폴더의 상위> uv run --project ai python ai/experiments/static_scene/compare_runs.py --base C1_room4_bf16 C1_room4_bf16_img`.

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
