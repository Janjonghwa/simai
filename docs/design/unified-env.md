# 한 가상환경(Tier 0 + vLLM + HMR, torch 2.11.0) 잠금과 사전 판정

S15P31A101-235 1단계. 기획서 §3.2(Tier 0 와 분석 워커는 GPU 하나에서 **별도 프로세스**, 분석 워커가 죽어도 Tier 0 는 계속), §3.2 GPU 메모리 예산표,
§3.8(VLM Cosmos3-Nano Reasoner, vLLM 0.19.1 이상 `vllm-cosmos3`), §3.12(GPU 실행 환경, 드라이버 CUDA 13.0 이상), §20.5(GPU Job Queue).
코드는 `ai/pyproject.toml`(extras `tier0`·`hmr`·`vlm`·`worker`, `[tool.uv]`, import-linter `tier0-no-worker-stack`), `ai/uv.lock`, `deploy/weights.json`(묶음 `vlm`).
시험은 `ai/tests/architecture/test_unified_env.py`, `ai/tests/architecture/test_import_rules.py`(Tier 0 금지 규칙). 사전 판정 스크립트는 저장소에 넣지 않았다(아래 "사전 판정", 2단계에서 이미지 점검으로 옮긴다).

1단계는 잠금·사전 판정·3차 세션 큐 초안까지다. 통합 Dockerfile·`simai-bootstrap` 감시 프로세스는 2단계(S15P31A101-246, 아래 "2단계: 통합 이미지와 simai-bootstrap 감시 프로세스")다.
MapAnything 을 같은 가상환경에 넣고 장면 준비 모드를 둔 것은 3단계(S15P31A101-250, 아래 "3단계")다.

## 결정

- **가상환경 하나, torch 2.11.0.** vLLM 0.21.0 이 `torch==2.11.0`·`torchvision==0.26.0`·`flashinfer-python==0.6.8.post1` 을 고정한다(PyPI requires_dist, 2026-10-22).
  Tier 0 는 torch 를 직접 고정하지 않아 ultralytics 가 끌어온 최신 2.14.1 이었으므로 Tier 0 를 2.11.0 으로 내린다. HMR(지금 `deploy/exp02` 의 torch 2.7.1+cu128 별도 잠금)도 같은 잠금으로 옮긴다.
- **torch 휠 출처는 그대로 PyPI.** PyPI 의 torch 2.11.0 Linux 휠은 CUDA 13.0 빌드다(requires_dist `cuda-toolkit[...]==13.0.2`, `nvidia-cudnn-cu13==9.19.0.56`). Windows 는 같은 판의 CPU 휠이다(`2.11.0+cpu`).
  색인을 따로 두지 않아 지금 index 설정(`tensorrt-cu13-libs` 만 pypi.nvidia.com)이 그대로다.
- **프로세스는 그대로 셋**: Tier 0 / vLLM 서버 / 분석 워커(§3.2 장애 격리). 가상환경이 하나라 Tier 0 코드가 워커 스택을 실수로 import 해도 설치 오류로 드러나지 않으므로 import-linter 로 막는다(아래).
- 근거: Argon 자문 2회(안 A 환경 2개 vs 안 B 환경 1개). 같은 cu130 인데 torch 를 두 벌(2.14.1·2.11.0) 두면 CUDA 13 + torch 휠 약 8GB 가 이미지에 두 번 들어가 노트북 빌드 캐시(약 20GiB)와 업로드 시간을 넘긴다.
  Tier 0 의 추론 본체는 TensorRT 엔진(torch 와 별개 휠)과 CPU(numpy·OpenCV·SciPy)라 torch 하향의 런타임 위험은 작고, 실제 위험(ONNX 내보내기·ModelOpt FP16 변환, CUDA 휠과 TensorRT libs 의 심볼)은 GPU 없이 미리 볼 수 있다(아래 "사전 판정").

## 판 고정 표

| 패키지 | pyproject 범위 | 잠금 판 | dev(9df8fd9) 잠금 | 근거 |
|---|---|---|---|---|
| torch | `==2.11.0` (tier0·hmr) | 2.11.0 (Linux cu130 / Windows cpu) | 2.14.1 | vLLM 0.21.0 고정 |
| torchvision | `==0.26.0` (tier0·hmr) | 0.26.0 | 0.29.1 | vLLM 0.21.0 고정 |
| triton | (torch 가 고정) | 3.6.0 | 3.8.0 | torch 2.11.0 requires_dist |
| ultralytics | `>=8.4.178,<8.5` | 8.4.178 | 8.4.178 | TrackTrack·추적기 ID 카운터(8.4.178), 8.5 판 변화 차단 |
| nvidia-modelopt[onnx] | `>=0.47,<0.48` (Linux) | 0.47.0 | 0.47.0 | ultralytics TensorRT 11 FP16 AutoCast 경로 |
| onnx | `>=1.21,<1.22` (Linux) | 1.21.0 | 1.21.0 | modelopt 0.47 이 onnx<1.22 |
| onnxruntime (CPU) | `>=1.24,<1.25` (tier0, Linux) | 1.24.4 | onnxruntime-gpu 1.24.4 | -246: CUDA 12 빌드 onnxruntime-gpu·cupy-cuda12x 를 override `never` 로 빼고 CPU 판(아래 "2단계") |
| tensorrt-cu13(-libs·-bindings) | `>=11.4,<12` (Linux) | 11.4.0.106 | 11.4.0.106 | 그대로 |
| vllm | `==0.21.0` (Linux) | 0.21.0 | — | 모델 카드·vllm-cosmos3 README 의 판 |
| flashinfer-python | `==0.6.8.post1` (Linux) | 0.6.8.post1 | — | vLLM 0.21.0 고정과 같은 값(명시) |
| vllm-cosmos3·transformers-cosmos3 | git `cf5d68c` (Linux) | 0.1.0 | — | cosmos-framework "Release 2026-09-23"(2주 넘은 커밋). 두 폴더는 06-01·09-17 뒤 안 바뀜. PyPI 에 없음 |
| transformers | constraint `>=5.5.3,<5.10` (Linux) | 5.9.0 | — | 제약 없으면 5.20.2. vllm-cosmos3 자체 잠금 5.9.0, vLLM 0.21.0 CI 5.5.3 |
| openai·anthropic·mcp | constraint `<3`·`<1`·`<2` (Linux) | 2.54.0·0.125.0·1.30.0 | — | 제약 없으면 3.36.1·2.1.0·2.3.0(메이저가 vLLM 0.21 뒤에 바뀜). 플러그인 잠금 2.38·0.105·1.27 |
| tokenspeed-triton | constraint `==3.7.10.post20260505` (Linux) | 같음 | — | vLLM 0.21.0(05-15) 이 나올 때 있던 유일한 판, 플러그인 잠금과 같음. 제약 없으면 10-09 에 올라온 3.9.10 |
| numpy | (Linux 에서 numba 0.65 상한) | 2.3.5 (Linux) / 2.4.6 (Windows) | 2.4.6 | vLLM 의 numba==0.65.0 |
| opencv-python-headless | `>=4.9` (io·calibration·detection·hmr) | 5.0.0.93 | 5.0.0.93 | 하나로 통일 |
| opencv-python | override `sys_platform == 'never'` | 설치 안 됨 | 5.0.0.93 | ultralytics 가 선언, 같은 `cv2` 폴더를 덮어씀 |
| fastapi·starlette·uvicorn·pydantic | risk_api·dev 범위 그대로 | 0.142.4·1.7.0·0.54.0·2.14.1 | 같음 | vLLM(`fastapi[standard]>=0.115`, `pydantic>=2.12`)과 겹쳐 `conflicts` 불필요 |
| protobuf | — | 7.36.2 | 7.36.2 | onnx(Tier 0)와 vLLM 이 함께 씀. vLLM 은 6.30~6.33.4 만 제외 |
| networkx | `==3.2.1` (hmr, Linux) | 3.2.1 (Linux) / 3.6.1 (Windows) | 3.6.1 | exp02 requirements.in 고정을 옮김 |
| setuptools | `<81` (hmr, Linux) | 80.10.2 (Linux) / 81.0.0 (Windows) | 84.0.0 | exp02 고정(pkg_resources), torch 2.11 은 `<82` |

Tier 0 만 깔 때(`uv sync --extra tier0`) 바뀌는 것(dev 잠금 대비): torch·torchvision·triton, CUDA 휠(cudnn 9.24.0.43 → 9.19.0.56, cublas 13.1.1.3 → 13.1.0.3, nvjitlink 13.4.92 → 13.0.88,
nccl 2.30.7 → 2.28.9, cusparselt 0.8.1 → 0.8.0, cuda-toolkit 13.0.3.0 → 13.0.2), numpy 2.4.6 → 2.3.5, networkx 3.6.1 → 3.2.1, omegaconf 2.4.0 → 2.3.1, setuptools 84 → 80.10.2, antlr4 추가, opencv-python 제거.
잠금이 하나라 Tier 0 단독 설치도 워커와 같은 판을 쓴다(그래야 한 가상환경에서 두 묶음이 같은 판이다). `onnxruntime-gpu`·`cupy-cuda12x`(CUDA 12, modelopt 의존성)는 그대로 남겼다(아래 "2단계").

## 잠금 결과 (충돌과 해결)

`uv lock` 은 첫 시도에 풀렸다(289 패키지, `conflicts` 선언 없음). 충돌 후보로 본 것과 결과:

- **fastapi·starlette·pydantic(risk_api·dev vs vLLM)**: vLLM 0.21.0 은 하한만 둔다. risk_api 범위(0.142.x·1.7.x)를 그대로 쓴다. 자문 4-5 의 `[tool.uv] conflicts` 분리는 필요 없었다.
- **OpenCV 두 벌**: ultralytics 가 `opencv-python` 을, 우리 extras·vLLM 이 `opencv-python-headless` 를 선언한다. `override-dependencies = ["opencv-python; sys_platform == 'never'"]` 로
  ultralytics 의 선언을 어느 플랫폼에서도 맞지 않게 바꿨다(잠금에 marker `never` 로만 남고 설치되지 않음, `uv sync --dry-run` 에서 제거로 확인). `detection` extra 에 headless 를 넣어 cv2 가 빠지지 않게 했다.
- **websockets**: HMR 의 `==14.1` 을 `>=14` 로 풀었다(Tier 0·dev 가 이미 `>=14`, 잠금 17.2). HMR 쪽은 `live_server.py` 의 `websockets.sync.server` 만 쓴다.
- **최신 판 끌려옴(이번에 묶은 것)**: 제약 없이 풀면 vLLM 쪽이 vLLM 0.21 보다 늦게 나온 메이저(openai 3, anthropic 2, mcp 2, huggingface-hub 2, transformers 5.20)와
  올린 지 2주가 안 된 판(tokenspeed-triton 3.9.10, 10-09)을 가져왔다. vLLM 은 이들의 상한을 두지 않아 import 단계에서 깨질 수 있으므로 `constraint-dependencies`(Linux 만)로
  vllm-cosmos3 자체 잠금(`packages/vllm-cosmos3/uv.lock`: vLLM 0.22.0, transformers 5.9.0, openai 2.38, anthropic 0.105, mcp 1.27, tokenspeed-triton 3.7.10.post20260505)과
  같은 메이저·마이너로 묶었다. huggingface-hub(1.33.0)·tokenizers(0.22.2)는 transformers 5.9 를 따라 내려왔다. 이 제약은 VLM 쪽만 쓰는 패키지라 Tier 0 판은 바뀌지 않는다.
- **git 의존성 4개**(vllm-cosmos3·transformers-cosmos3·utils3d-moge·pipeline)는 `[tool.uv.sources]` 에 커밋을 고정했다. vllm-cosmos3 은 자기 `tool.uv.sources` 에서 transformers-cosmos3 를
  상대 경로로 찾는데, 의존성의 uv sources 는 쓰이지 않으므로 우리 sources 에 같은 커밋의 subdirectory 로 적었다. Tier 0 만 깔 때(`--extra tier0`)는 git 이 필요 없다(git 없는 Tier 0 이미지 컨테이너에서 `--dry-run` 확인).
- "같은 venv 위에 워커 잠금 하나 더"(자문 4-5 최후 대안)로 가지 않았다.

**Linux 전용 marker.** `vlm` 은 전부, `hmr` 은 torch·torchvision 밖 전부 `sys_platform == 'linux'` 다(Windows 노트북 개발 환경에서 `uv lock`·`uv sync` 가 vLLM·flashinfer·xtcocotools 를 건드리지 않음).
torch·torchvision 은 marker 없이 같은 판(Windows CPU 휠)이다. `[tool.uv] constraint-dependencies` 도 모두 Linux 전용이다. `test_unified_env.py` 가 marker 빠짐(extras·constraint)을 잡는다.

**HMR 패키지 정리**(`deploy/exp02/requirements.in` → `hmr` extra). 추론에 안 쓰는 것을 뺐다. 근거는 Fast-SAM-3D-Body @d72aa36 전체 `.py` 의 import(아래 셋은 import 0건)와 Detectron2 @a1ce2f9 의 쓰임:
`tensorboard`(Detectron2 `utils/events` 의 기록기에서만, 우리는 `LazyConfig.load` 만), `black`(Detectron2 `LazyConfig.save` 의 서식 정리에서만), `pytest`(시험 도구), `seaborn`·`hydra-submitit-launcher`(import 0건, SLURM 실행기),
`pip`(chumpy `--no-build-isolation` 빌드 단계에서만 필요, 실행 환경에는 필요 없음). `opencv-python` 은 headless 로 합쳤다. 나머지는 그대로 옮겼다(덜 확실한 `ffmpeg`·`cython`·`appdirs` 등은 근거 없이 빼지 않았다).
`deploy/exp02/requirements.in`·`requirements.lock.txt` 는 2단계 전까지 exp02 이미지를 위해 그대로 둔다.

## 설치 순서 (2단계 Dockerfile 이 따를 규칙)

-246: 통합 이미지는 잠금 밖 휠 셋을 넣지 않기로 해(아래 "2단계") 이 순서는 exp02 처럼 세 휠을 쓰는 환경과 나중의 잠금 밖 휠(nvdiffrast 등)에 적용한다.

1. `uv sync --locked --no-dev --extra tier0 --extra worker`(묶음별 큰 휠 층은 지금 Tier 0 Dockerfile 방식으로 먼저).
2. **그 뒤에** 잠금 밖 휠 3개를 `--no-deps` 로: chumpy 0.70, detectron2 @a1ce2f9(`FORCE_CUDA=0`, CPU 전용 확장), moge @74fbce0. 빌드는 따로 둔 빌드 단계(같은 잠금 + `pip` + `build-essential`·`git`)에서 휠로 만들어 옮긴다.
3. 휠을 깐 뒤에는 `uv sync` 를 다시 돌리지 않는다. `uv sync`(exact)는 잠금에 없는 패키지를 지우므로 2 의 휠이 사라진다. 잠금을 바꾸면 1 → 2 를 다시 한다.
4. `uv pip check` 는 MoGe 의 flex-gemm·gradio 를 빼서 실패한다(지금 exp02 와 같음). import 점검으로 본다.

## Tier 0 경로 import 금지

import-linter 계약 `tier0-no-worker-stack`: `services.tier0` 와 그 import 대상 패키지(`simai_ai.io·detection·tracking·geometry·contact·calibration·risk·config·eventlog·recovery·gpu·providers`)가
`vllm`·`vllm_cosmos3`·`transformers`·`transformers_cosmos3`·`flashinfer`·`detectron2`·`moge`·`pyrender`·`smplx`·`pytorch_lightning`·`chumpy`·`utils3d`(MoGe 의존성)와
우리 워커 패키지 `simai_ai.vlm`·`simai_ai.hmr`·`simai_ai.scene`·`services.worker` 를 직접·간접 import 하지 못한다(지금 Tier 0 경로에서 이들을 import 하는 곳은 없다).
워커 쪽(`simai_ai.vlm`·`hmr`·`scene`) 안에서 워커 스택을 쓰는 것은 막지 않는다. `services/tier0` 가 새 `simai_ai` 하위 패키지를 쓰기 시작하면 source 목록에 넣어야 하고, 빠지면 시험(`test_tier0_contract_covers_every_package_tier0_imports`, `import simai_ai.x`·`from simai_ai import x`·괄호 여러 줄 형태를 모두 읽음)이 실패한다.
torch 는 Tier 0 도 쓰므로 막지 않는다. 프로세스별 `TRITON_CACHE_DIR`·`TORCHINDUCTOR_CACHE_DIR` 분리(자문 4-4)는 실행 스크립트 일이라 2단계다.

## 사전 판정 (노트북 Docker, Linux CPU 컨테이너, GPU 없음)

노트북 Docker Desktop 29.6.1(엔진 VM 메모리 2GB → 진행 중 사용자가 10GB 로 올림, CPU 22). 바인드 마운트가 이 노트북에서 멈춰(alpine 도 60초 무응답) 파일은 `docker cp` 로 주고받았다.
기준 환경은 dev 의 Tier 0 이미지 `jonghwa1492/tier0:3926a8772649`(torch 2.14.1+cu130, 2차 세션이 쓴 판)이고, 새 환경은 같은 이미지 컨테이너의 가상환경을 이 잠금으로 바꾼 것이다
(`uv sync --locked --no-dev --extra tier0 --extra worker --no-install-project` → 잠금 밖 휠 3개). 두 컨테이너 모두 GPU 없음. 스크립트·결과 JSON 은 커밋하지 않았다(Owner scratchpad `s235/`).

**환경 만들기.** `uv sync` 15분(끊긴 첫 시도 뒤 남은 15개 받기 14분 45초, 받기 약 3MB/s, uv 캐시 7.7GB), 174개 설치, 가상환경 15GB. 잠금 밖 휠 빌드 57초(`MAX_JOBS=4`, `FORCE_CUDA=0`, detectron2 휠 6.8MB).
Tier 0 이미지에는 git 이 없지만 `--extra tier0` 만 깔 때는 git 의존성을 건드리지 않는다(`--dry-run`, 바뀜은 위 "판 고정 표" 아래 목록과 같음).

**발견 1 (cv2 사라짐).** 두 OpenCV 휠이 함께 깔린 **기존** 가상환경을 이 잠금으로 sync 하면 `opencv-python` 제거가 두 휠이 같이 쓰는 `cv2/` 폴더를 지워 `import cv2` 가 실패한다
(headless 는 같은 판이라 다시 깔지 않아 dist-info 만 남음). `--reinstall-package opencv-python-headless` 를 붙여 sync 하면 복구된다. 새로 짓는 이미지는 해당 없고,
3차 세션 u0 처럼 기존 이미지의 가상환경을 바꿀 때와 개발자 PC 의 기존 `--extra tier0` 가상환경을 갱신할 때 필요하다.
**발견 2 (설치 순서 실증).** 위 재 sync 가 `--no-deps` 로 깐 chumpy·detectron2·moge 를 지웠다(exact sync). "설치 순서" 3 의 규칙 그대로다.

| 판정 | torch 2.14.1 (기준) | torch 2.11.0 (새 잠금) |
|---|---|---|
| import Tier 0 (`torch`·`tensorrt`·`ultralytics`·`onnx`·`modelopt`·`modelopt.onnx.autocast`·`cv2`·`services.tier0.service`·`simai_ai.detection.engine_cache`) | (2차 세션에서 동작) | OK. `torch.version.cuda` 13.0, cv2 5.0.0 `GUI: NONE`, `opencv-python` 없음, numpy 2.3.5 |
| import vLLM (`vllm` 0.21.0, `flashinfer`, `vllm.entrypoints.openai.api_server`) | — | OK. transformers 5.9.0, openai 2.54.0, fastapi 0.142.4, starlette 1.7.0, protobuf 7.36.2. `vllm/_C.abi3.so` 는 `libcudart.so.13` 링크 |
| vllm-cosmos3 플러그인 | — | `load_general_plugins()` 뒤 `Cosmos3ReasonerForConditionalGeneration` 등록, 모델 클래스 적재 OK. HF config(고정 커밋 `config.json`)를 `AutoConfig` 가 `Cosmos3OmniConfig`(`cosmos3_omni`)로 읽음 |
| tokenspeed-triton | — | 모듈 이름 `tokenspeed_triton` 이라 torch 의 `triton` 3.6.0 을 덮지 않음 |
| import HMR (`detectron2`·`detectron2._C`·`moge`·`MoGeModel`·`pyrender`(EGL)·`pytorch_lightning`·`smplx`·`timm`, Fast @d72aa36 `sam_3d_body`·`tools.build_detector`·`build_fov_estimator`·`vis_utils`) | — | OK. `detectron2._C.has_cuda()` False, `LazyConfig.load(mhr70.py)` 관절 70 |
| `.so` RTLD_NOW 열기(`tensorrt_libs`·`torch/lib`·`nvidia/*`) | 78개, 설명 안 되는 실패 0 | 76개, 설명 안 되는 실패 0 |
| 그중 TensorRT libs 19개 | 모두 OK, ldd -r 미정의 0 | 모두 OK, ldd -r 미정의 0 |
| 실패로 나온 것(두 판 같음) | 외부 라이브러리 없음 9개(`libcuda.so.1` = 드라이버, MPI·PMIx·OpenSHMEM·UCX·libfabric·RDMA = nvshmem·cufile 선택 전송), ldd 단독 거짓 양성 2개(`libtorch_python` 파이썬 C API, `libtorch_nvshmem` 먼저 올라간 c10_cuda 심볼) | 같음 |
| YOLO26x-pose ONNX 내보내기(CPU, `dynamic`·`nms=False`·`simplify=False`, 640·batch 1) | 4.4초, opset 18, 노드 1,238, 230.6MB | 4.9초, 같음 |
| ModelOpt 0.47 FP16 AutoCast(ultralytics `modelopt_quantize_onnx(quantize=16)` 그대로) + `onnx.checker`(full_check) | 18.2초, 최대 RSS 5,794MB, 노드 1,249, FP16 initializer 350개 | 19.4초, 5,522MB, 같음 |
| 입출력 | `images` [batch,3,height,width] FLOAT → `output0` [batch,Concatoutput0_dim_1,anchors] FLOAT (keep_io_types) | 같음 |
| 두 ONNX 그래프 비교 | | FP32·FP16 모두 노드 op 순서 동일, initializer shape·dtype 동일, Cast(→FP16 15, →FP32 11) 수 동일 |

- 가중치: `yolo26x-pose.pt` 는 노트북에 없어(`docs/data/weights.md` 의 노트북 위치 없음) 받지 않고 같은 구조의 `yolo26x-pose.yaml`(무작위 가중치, 매개변수 62,914,350개)로 만들었다. 그래프 구조 판정용이다.
- 메모리: 엔진 VM 2GB 에서는 ONNX 내보내기(최대 RSS 1.6GB)까지 되고 AutoCast 의 ORT 참조 실행에서 OOM(종료 137, 640·320, 같은·새 프로세스 모두)이었다. 10GB 로 올린 뒤 640·batch 1 이 통과했다.
  운영 크기(1280·batch 4) AutoCast 는 활성값이 약 16배라 노트북에서 돌리지 않았다(3차 세션 b 가 GPU 로 한다).
- 두 판 모두 AutoCast 경고가 같다: `Range` 1개 opset 18 FP16 미지원으로 FP32 유지, 공유 상수 복제, FP16 최솟값보다 작은 initializer 값 6.0e-08 대체(무작위 가중치 탓일 수 있음).

**시험(Linux, 통합 가상환경).** 같은 컨테이너에 `ai/`·`contracts/`·`deploy/` 를 넣고(`pytest`·`pytest-timeout`·dev 시험 도구는 컨테이너에만 임시 설치) 전체 `pytest`(`--timeout=240 --timeout-method=thread`)를 돌렸다.
아래 "기존 결함" 의 시험 하나가 멈춰 그것만 `--deselect` 했다. 결과 4,935 passed, 35 skipped, 1 deselected, 37 failed, 48 errors(1,123초), 다른 멈춤 없음.
실패·오류 85건은 모두 임시 실행 틀 탓이었다(프로젝트 미설치라 `simai-ai` 메타데이터 없음, 복사본이 git 저장소가 아님, `docs/` 없음). 틀을 보완(편집 설치·`git init`·`docs/` 복사)하고 그 10개 파일만 다시 돌려
231 passed, 2 failed: `test_scripts_copied_into_images_are_lf`(루트 `.gitattributes` 를 복사하지 않음, 틀 탓), `test_live_input.py::test_partial_port_start_failure_closes_already_opened_listeners`
(dev Tier 0 이미지(torch 2.14.1)에서도 같은 실패, Linux 의 포트 bind 동작 차이로 보이며 이 잠금과 무관). Tier 0·HMR 관련 시험(검출·추적·기하·보정·services.tier0·experiments/hmr)은 torch 2.11.0 에서 통과했다.
Windows 노트북에서는 tier0 extra 를 깐 별도 가상환경(torch 2.11.0+cpu, ultralytics 8.4.178)으로 ultralytics·torch 를 쓰는 시험(tracking·detection·calibration/intrinsics/test_learned·services/tier0/test_tier0_entry) 543 passed, 3 skipped(geocalib·anycalib·YOLO 가중치 없음).

## Cosmos3-Nano 가중치 (`deploy/weights.json` 묶음 `vlm`, 초안)

HF `nvidia/Cosmos3-Nano` 커밋 `e59a53c25979a090fa8706c9acc0c254a6e89b92`(2026-09-16 수정, HF API `gated=false`, OpenMDW-1.1). 저장소 파일 합 34.99GB.
루트 `model.safetensors.index.json` 의 `weight_map` 은 `transformer/` 샤드 7개(814개 텐서, 30.35GB)와 `vision_encoder/model.safetensors`(351개, 1.15GB)만 가리킨다(`total_size` 31.50GB).
`vllm_cosmos3.Cosmos3ReasonerForConditionalGeneration` 은 vLLM Qwen3-VL 을 이어받아 `lm_head.`·`model.language_model.`·`model.visual.` 로 바뀌는 이해 탑 가중치만 싣고 나머지(생성 탑)는 버린다.
`vae/`(1.41GB)·`sound_tokenizer/`(1.99GB)는 index 에 없어 vLLM safetensors 로더의 index 필터에서 빠진다 → 목록에 넣지 않았다(3.4GB 절약). 3차 GPU 세션에서 기동했으나 이 목록만으로 떴는지는 기록에 없다. 4차 세션 v4(`vlm_int.py`, `SIMAI_WEIGHTS_SET=vlm`)에서 확인한다.

목록에 넣은 18개(모두 `sets: ["vlm"]`, `weights/cosmos3-nano/<HF 경로>`): 샤드 7개 + vision_encoder(해시 = HF API LFS oid), 작은 파일 10개(config·index·generation·preprocessor·video_preprocessor·chat_template·tokenizer·tokenizer_config·vocab·merges,
고정 커밋에서 받아 sha256). `checkpoint.json`(`{}`)·`model_index.json`(diffusers 파이프라인용)·`text_tokenizer/`·`transformer/config.json` 등은 Reasoner 서빙에 쓰지 않아 뺐다. 빠진 파일 때문에 서버가 안 뜨면 v1 결과로 새 항목을 더한다.
지금 이 묶음을 쓰는 이미지는 없다(2단계 통합 이미지가 `SIMAI_WEIGHTS_SET` 에 더한다). 출처 기록은 `docs/data/weights.md` "Cosmos3-Nano".

## 3차 GPU 세션 큐 (통합 이미지 기준, S15P31A101-246 에서 갱신)

통합 이미지(`unified`, 위 "2단계")의 Pod 하나(H100 80GB 권장, 드라이버 CUDA 13.0 이상, 공개 TCP 포트, 컨테이너 디스크 150GB 이상)에서 아래 순서로 돈다. LIVE 관제 시간이 아닐 때만(AGENTS.md).
1단계 초안의 u0(Tier 0 이미지 Pod 의 가상환경 바꾸기)은 이미지가 대신해 없앴다. 큐는 `simai-bootstrap queue`(이미지 ENV `SIMAI_IMAGE_KIND=unified`: tier0 항목 a·b·t·c·d + u1·u2), v1~v3 는 큐 표에 '자리만'으로 남는 사람 항목이다.
Pod 는 시작 명령 `sleep infinity` 로 띄운다(감시 프로세스가 Tier 0 를 띄우면 b·c·d 의 시험용 Tier 0 와 겹친다).

| 키 | 확인 | 명령·방법 | 기대 / 기록 |
|---|---|---|---|
| a | Pod 점검 | 큐(기존) | 기존 기준 |
| u1 | 통합 이미지 GPU 점검 | 큐: `exp_image_check.py`(kind unified): CUDA·sm 커널·fp16/bf16 행렬곱, HMR 경로 import(잠금 밖 휠 없음·mhr70 70), pyrender EGL 렌더(NVIDIA EGL), vLLM + vllm-cosmos3 Reasoner 등록 | 모두 ok. 1단계의 u1(`torch.cuda.is_available()`·cudnn 91900·`vllm collect-env`)은 이 항목 결과와 `vllm collect-env` 출력 기록으로 |
| b | 엔진 빌드(torch 2.11, CPU onnxruntime) | 큐(기존 `session1_engine.sh`) | 통과. 빌드 시간·입출력 shape 기록. 엔진 캐시 키에 torch 판이 들어가 2차 세션 캐시(torch 2.14.1)는 `conflict` 로 옆에 옮기고 자동으로 다시 만든다(지우지 않아도 된다). onnxruntime-gpu·cupy 를 뺀 뒤 AutoCast 가 같은지(경고·노드 수) |
| c | LIVE 입력(-236·-237) | 큐(기존 `session1_live.py all`) | 기존 기준(c5a 10명 p95 ≤ 50 ms) |
| d | GPU 우선 보호(-60) | 큐(기존 `session1_gpu_priority.py`) | 기존 P0~P7 기준 |
| u2 | 통합 이미지 HMR 보정 K 경로(렌더 없음) | 큐: `SIMAI_WEIGHTS_SET=exp02 simai-bootstrap queue`(sam-3d-body·MHR·YOLO·MoGe 파일, MoGe 는 helper 확인용으로만 받고 쓰지 않음) → `simai-exp02 run_image.py --mode official --calibration … --no-render`, `check_calib_k.py` 판정(g4 와 같은 교실 사진 20장 + 보정 기록) | 판정 통과(K 출처 CALIBRATION·펴짐, MoGe 미적재, 초점 차 < 0.5 px). 지연·VRAM 은 torch 2.11 기록(g1~g3 기대값 표에 없는 조합) |
| u3·u4 | 통합 환경 MapAnything C1·C2(S15P31A101-250, 장면 준비 모드) | 큐: `simai_bootstrap.py scene` → `run_mapanything.py`(m1·m2 와 같은 room4 4장·가중치·인자, run id `*_uni_<세션>`). u3 이 `static` 가중치 두 벌을 처음 받는다 | 75(LIVE·GPU 사용 중) 없이 SUCCESS. 노트북에서 `compare_envs.py --base <m1·m2 Run> --new <u3·u4 Run>` 가 PASS(아래 "3단계" 합격 기준). 지연·`peak_alloc_mb`·`gpu_used_peak_mb` 는 기록(같은 GPU 모델일 때 m1·m2 와 비교) |
| v1 | VLM 단독 적재·VRAM(감시 프로세스) | `SIMAI_WEIGHTS_SET=vlm simai-bootstrap check`(31.5GB 받기, 다음부터 스탬프) → 시험용 Tier 0 없이 `SIMAI_VLLM_CMD` 그대로 `vllm serve …`(감시자와 같은 인자: README "감시 프로세스" 3) 또는 v3 의 감시 모드로 | "Application startup complete", 적재 시간(감시자 시한 1800초 안인지), 가중치 메모리·KV 캐시(GiB), `nvidia-smi` 최대, vae·sound_tokenizer 없이 뜨는지, EngineCore pid 의 pgid 가 vLLM 리더와 같은지(`ps -o pid,pgid,cmd`) |
| v2 | G7 지연 모양 | v1 서버에 OpenAI 호환 `/v1/chat/completions` 20회(사람 없는 그림 + 무늬 영상 3~5초 + 상태 텍스트), 동시 1·4 | 요청별 첫 토큰·전체 p50·p95. G7 p95 ≤ 5초(§15)와 비교만 |
| v3 | Tier 0 + VLM 동시(감시 프로세스, G0·CPU 경합) | 기본 진입점(`simai-bootstrap tier0 --config …`, `SIMAI_SERVICES=tier0,vllm,worker`)을 c4 와 같은 LIVE 하네스 입력으로 띄우고 v2 요청을 동시 4로 10분. `tier0_priority_probe.py watch`, `pidstat` | 기동 순서 로그(`[tier0]` → Tier 0 준비됨 → 워커 가중치 present-stamp → `[vllm]` 준비됨 → `[worker]`), Tier 0 `capture_to_risk`·`frame_age_ms_p95` 의 c4 대비 악화, `gpu_priority` 통과, Tier 0 메모리 부족 0. `kill -9 <vLLM pid>` 1회 → 그룹 정리·백오프 재기동·Tier 0 지연 영향 기록, 끝에 `docker stop`(RunPod Stop) 순서 로그 |
| g0·g4 | HMR 렌더·E-GATE-0(exp02 이미지) | exp02 이미지 Pod 따로(기존) | 기존 기준. 렌더 패널·MoGe 예비 경로는 이 이미지에만 있다 |
| w1·w2 (+m1·m2) | MapAnything 알려진 K(-102), u3·u4 비교 기준 | `mapanything` 이미지 Pod 따로. 같은 세션에서 m1·m2 도 돌려 u3·u4 와 같은 GPU 모델 결과를 만든다(가능하면 같은 GPU 종류의 Pod) | `docs/design/world-frame-from-mapanything.md` 그대로. m1·m2 는 E-GATE-0 대조 기준(`docs/design/exp-images.md`) |

- 순서 근거: Tier 0 가 먼저(b·c·d 가 통과해야 통합 이미지가 Tier 0 를 해치지 않음을 안다), 그다음 HMR(u2, 가중치 작음), VLM 단독(v1·v2), 마지막에 동시(v3).
- 결과로 §3.2 예산표(VLM 0.55, 분석 워커 0.70)를 고칠 근거(v1 VRAM)와 감시자의 기본값(vLLM 시한·스레드 제한·nice, v3 CPU 경합)을 얻는다.
- 비용: 이미지 받기(아래 "빌드 결과" 압축 크기) + `vlm` 가중치 31.5GB + `exp02` 가중치 약 5GB 가 가장 길다. 볼륨이 없으므로 결과 묶음만 가져오고 Pod 는 바로 Stop 한다.

## 버린 대안

- **환경 2개(안 A: Tier 0 torch 2.14.1 + 워커 torch 2.11.0)**: 같은 cu130 인데 CUDA·torch 휠 약 8GB 를 두 벌 넣는다. 노트북 빌드 캐시와 업로드를 넘긴다. Tier 0 를 검증된 판에 두는 이점은 사전 판정과 3차 세션 b·c·d 재실행으로 대신한다.
- **같은 venv 위 워커 잠금 하나 더(자문 4-5)**: 잠금이 하나로 풀려 필요 없다. 두 잠금은 같은 판을 사람이 맞춰야 한다.
- **`[tool.uv] conflicts` 로 risk_api·worker 분리**: 범위가 겹쳐 필요 없다. 선언하면 한 venv 에 두 묶음을 함께 못 깐다(dev 시험이 fastapi 를 쓴다).
- **VLM 서빙 다른 경로**: HF Transformers 직접(transformers>=5.11·torchvision 0.25·Python 3.13 요구, Tier 0 와 판이 더 멀어지고 배치·KV 캐시 서빙이 없음),
  TensorRT-LLM(rc 컨테이너만, 별도 이미지), NIM(별도 컨테이너·라이선스 조건). SGLang 은 Generator 탑만 지원. vLLM + vllm-cosmos3 가 모델 카드의 기본 경로이고 명세 §3.8 과 같다.
- **vLLM 0.19.1+cu128**: CUDA 12.8 드라이버용. 우리 최소 사양은 드라이버 CUDA 13.0 이상이고 Tier 0 가 cu13 이라 cu128 을 섞으면 다시 두 벌이 된다.
- **잠금 전체에 `exclude-newer`(2주) 적용**: 이미 검증된 Tier 0 판까지 함께 움직인다. VLM 쪽 문제 패키지만 constraint 로 묶었다.

## 잔여 위험

- Tier 0 의 torch 2.11.0·CUDA 휠 하향(cudnn 9.19, nvjitlink 13.0.88)으로 TensorRT 엔진 빌드·추론이 달라지는지는 GPU 에서만 확정된다(3차 세션 b·c·d). 사전 판정은 CPU 로 내보내기·변환·심볼만 본다.
- 사전 판정의 YOLO 는 같은 구조의 무작위 가중치(`yolo26x-pose.yaml`)다. 실제 가중치는 노트북에 없어 받지 않았다. AutoCast 의 FP32 유지 노드 선택은 활성값 크기에 따라 실제 가중치에서 다를 수 있다(엔진 빌드 b 가 실제 가중치로 본다).
- AutoCast 는 운영 입력(1280, batch 4)이 아니라 640·batch 1 로 돌렸다(그래프는 동적 축이라 같은 구조, 노트북 메모리 10GB 에서 640×1 이 5.8GB). 운영 크기 변환은 b 에서 GPU 가 한다.
- 엔진 캐시 키(`engine_cache`: GPU 세대·TensorRT·가중치 해시·입력 설정·ultralytics 판)에 torch 판이 없다. 남는 디스크에 torch 2.14.1 로 만든 엔진이 있으면 그대로 쓴다.
  ONNX 그래프가 두 판에서 같으므로(위 비교) 엔진도 같을 것으로 보지만, 3차 세션 b 는 캐시를 비우고 빌드한다. 키에 torch 판을 넣을지는 2단계에서 정한다(이번에는 코드 구조를 바꾸지 않음).
- vLLM·플러그인은 import·모델 클래스 등록·설정 읽기까지만 봤다. 가중치 적재·추론·VRAM 은 3차 GPU 세션 통합 시험(S15P31A101-278)에서 쟀다(아래 "3차 GPU 세션 결과").
- constraint 로 묶은 transformers 5.9 등은 플러그인 자체 잠금(vLLM 0.22)과 같은 메이저·마이너이고 vLLM 0.21 과의 조합 그대로를 공식이 시험한 것은 아니다.
- opencv 5.0(Tier 0 가 검증한 판)은 vLLM CI(4.13)보다 새 메이저다. vLLM 이 cv2 를 쓰는 곳은 영상 프레임 읽기다(v2 의 영상 입력 요청으로 본다).
- **해결(S15P31A101-245). 기존 결함(이 티켓이 만든 것 아님, 사전 판정 중 발견, -237 코드).** Linux 휠 numpy 의 OpenBLAS 에서 `np.linalg.svd` 가 inf·NaN 이 든 행렬에서 돌아오지 않는다.
  - 재현 조건: numpy 2.4.6(OpenBLAS 0.3.31.188.0, dev Tier 0 이미지 torch 2.14.1)과 numpy 2.3.5(OpenBLAS 0.3.30, 이 잠금) 둘 다. 최소 재현 `A = np.ones((3, 4, 4)); A[1, 0, 0] = np.inf; np.linalg.svd(A)` 가 60초 넘게 끝나지 않음(NaN 만 넣은 경우는 따로 재지 않았다. 멈춘 시험 행에는 inf·NaN 이 함께 있다). Windows(노트북)에서는 시험이 통과한다.
  - 멈춘 시험: `tests/geometry/test_geometry_speedup_regression.py::test_degenerate_rays_match_previous` 하나(전체 실행에서 다른 멈춤 없음). 입력은 `_baseline_rows(cam1, cam2)`(rig `wide`, 25행 × 2시점 × 2)의 특수 행 — `[0,0,0]=inf`, `[1,1,1]=-inf`, `[2,0,1]=NaN`, `[3,1]=[3,0]`(같은 정규화 좌표).
  - py-spy 위치: 시험 안의 이전 구현 `legacy_batch_triangulation.py:647 _dlt_batch` 와 **제품 코드** `svd (numpy/linalg/_linalg.py:1862)` ← `_dlt_batch (simai_ai/geometry/triangulation.py:659)` ← `_solve_batch (:636)` ← `triangulate_normalized_many (:429)`(같은 행을 제품 함수에 직접 넣어 120초 넘게 멈춤).
  - 영향: Tier 0 판정 루프가 non-finite 정규화 좌표를 이 함수에 넘기면 멈출 수 있다. 이 MR 에서는 고치지 않았다(제품 코드 범위 밖).
  - 해결(-245): SVD 계열 호출 앞에서 유한하지 않은 계수를 걸러 해 없음으로 둔다. 이 시험은 Linux 컨테이너에서 통과하고, 새 반례와 함께 새 프로세스 60초 제한으로 돈다. 전수 조사·음성 대조는 `docs/design/tier0-geometry-budget.md` "S15P31A101-245" 절.

## 2단계: 통합 이미지와 simai-bootstrap 감시 프로세스 (S15P31A101-246)

명세 §3.2(프로세스 분리·GPU 예산·워커 보호), §3.6, §3.12(GPU 실행 환경·시작 순서), §5.4(VLM). 코드는 `deploy/tier0/Dockerfile`(타깃 `tier0`·`unified`), `deploy/docker-bake.hcl`(`unified`),
`deploy/scripts/simai_bootstrap.py`(감시 프로세스·받기 재시도·이어받기·검증 스탬프), `deploy/scripts/check_image.sh`·`exp_image_check.py`(통합 이미지 점검), `deploy/scripts/gpu_session1.py`(이미지 `unified`, u1·u2·v1~v3),
`ai/src/simai_ai/detection/engine_cache.py`(키에 torch 판), `ai/experiments/hmr/run_image.py`(잠금 밖 휠이 없을 때의 명확한 오류), `ai/pyproject.toml`·`ai/uv.lock`(onnxruntime-gpu·cupy 제외).
시험은 `ai/tests/deploy/test_simai_supervisor.py`(가짜 프로세스 + POSIX 실제 프로세스, `fake_service.py`), `test_exp_images.py`·`test_gpu_session1.py`·`test_simai_bootstrap.py`, `ai/tests/detection/test_engine_cache.py`, `ai/tests/architecture/test_unified_env.py`.

### 결정

- **같은 Dockerfile 의 두 타깃(`tier0`·`unified`).** 두 이미지가 CUDA 13 휠·torch·TensorRT libs·큰 일반 휠·Tier 0 잠금 층(`tier0-deps` 단계)을 같은 명령·같은 내용으로 공유한다.
  레지스트리에는 공통 층이 한 번만 올라가고(같은 다이제스트), `tier0` 타깃은 이전 이미지와 같은 내용이라 Tier 0 단독 사이트·지금 세션 스크립트가 그대로 돈다.
  코드 층(`ai/src`)은 각 타깃 맨 위라 코드 한 줄을 바꿔도 워커 층(vLLM 등)을 다시 만들지 않는다. 가상환경을 통째 `COPY --from` 하지 않는다(자문 1: 8~11GB 단일 층).
- **층 구성(unified).** `tier0-deps`(cuda·torch·trt·wheels·Tier 0 나머지) → `unified-deps`: apt(EGL·X11·gcc·git) → `worker`(잠금에서 뽑은 워커 쪽 큰 휠 8개: flashinfer-cubin·vllm·tokenspeed-triton·nvidia-cutlass-dsl-libs-base·llvmlite·tilelang·xgrammar·z3-solver)
  → `uv sync --extra tier0 --extra worker`(나머지) → Fast-SAM-3D-Body·DINOv3 코드 사본 → 코드(정확 sync). 층 크기는 아래 "빌드 결과".
- **잠금 밖 휠(chumpy·Detectron2·MoGe)은 통합 이미지에 넣지 않는다**(Lead 범위 조정, 사용자 질문 반영 2026-10-23). 근거: exp02 이미지에서 `sys.meta_path` 로 세 모듈의 import 를 막고
  시연 HMR 경로(보정 K → `sam_3d_body.load_sam_3d_body`·`SAM3DBodyEstimator`·DINOv3 백본·`tools.build_detector`(yolo_pose)·`sam_3d_body.metadata.mhr70`)를 올렸더니 모두 import 됐다.
  막혀 실패한 곳은 렌더 패널 `tools.vis_utils`(→ `sam_3d_body/visualization/utils.py` 의 `detectron2.config.LazyConfig`)와 K 없는 그림의 FOV 예비 `tools.build_fov_estimator.load_moge` 뿐이다.
  chumpy 는 Fast 저장소의 `mhr2smpl/`·`mocap/` 에서만 쓴다. 관절 정보(mhr70)는 Detectron2 LazyConfig 대신 같은 파일을 파이썬 모듈로 읽는다(`exp_image_check.hmr_chain`, `check_image.sh`, 원본은 고치지 않음, `live_server.py` 도 이미 이렇게 읽는다).
  `run_image.py` 는 Detectron2 가 없으면 `--no-render` 없이 시작하지 않고(종료 2), MoGe 가 없으면 K 없는 그림이 하나라도 있을 때 모델을 올리기 전에 그 그림 이름과 함께 종료 2 다(조용히 기본 K 로 돌지 않는다).
  exp02 실험 이미지(E-GATE-0 재현·렌더 패널)는 그대로 둔다. `ai/uv.lock` 은 세 휠과 상관없다(원래 잠금 밖). 이로써 `--inexact` sync 도 필요 없다(빌드 단계 build-essential·pip 도 없음).
- **pyrender·EGL 은 남긴다.** `sam_3d_body` 가 import 때 pyrender 를 연다(위 확인에서 `pyrender` 적재). 텍스처 굽기(-89, 아래 "다음 자리")도 EGL 을 쓸 수 있어 `NVIDIA_DRIVER_CAPABILITIES=compute,utility,video,graphics` 와 EGL·X11 런타임 패키지를 둔다.
- **onnxruntime-gpu·cupy-cuda12x 를 뺐다**(두 타깃). `override-dependencies` 로 `sys_platform == 'never'`, `tier0` extra 에 CPU `onnxruntime>=1.24,<1.25`. 근거: 두 휠은 CUDA 12 빌드라 cu13 이미지에서
  GPU 공급자를 열지 못해 ORT 는 이미 CPU 로만 돌았다(`docs/design/exp-images.md` 빌드 결과). ModelOpt 0.47 의 `cupy` import 는 INT4 양자화(`onnx/quantization/int4.py`)의 try/except 안뿐이고,
  FP16 AutoCast 의 참조 실행 기본 공급자는 `["cpu"]`(`onnx/autocast/convert.py`)다. ultralytics 의 ONNX 내보내기는 `simplify` 또는 INT8 일 때만 onnxruntime 을 요구한다(우리 엔진 빌드는 `simplify=False`, FP16).
  잠금은 두 패키지만 빠졌다(287 패키지). 엔진 빌드가 같은지는 3차 세션 b 가 GPU 에서 본다(어차피 torch 2.11 로 다시 본다). 노트북 판정은 아래 "빌드 결과".
- **엔진 캐시 쓰기 직렬화.** 같은 엔진 경로의 확인·옆으로 옮기기·빌드·게시를 `cache_root/.locks/` 의 잠금 파일로 직렬화했다(리뷰 권장: 같은 저장 위치를 쓰는 두 Tier 0). 기다린 쪽은 먼저 만든 엔진을 다시 확인해 쓴다(빌드 한 번).
- **엔진 캐시 키에 torch·ModelOpt 판.** `BuildTarget.torch_version`·`modelopt_version`(detect 는 `torch.__version__`·설치된 `nvidia-modelopt`, `plan` 은 `--torch`·`--modelopt` 또는 설치판)을 메타데이터 키에 넣었다. 경로는 그대로라 이 키가 없던 엔진(2차 세션 torch 2.14.1)이나
  다른 torch 로 만든 엔진은 `conflict` 로 옆(`*.conflict-<ms>`)에 옮기고(지우지 않음) 다시 만든다. 3차 세션 b 의 "기존 캐시를 지우고 빌드" 를 사람이 하지 않아도 된다.
- **감시 프로세스는 `simai-bootstrap` 안(표준 라이브러리만).** PID 1 은 지금처럼 tini. `SIMAI_SERVICES` 가 없거나 `tier0` 이면 지금과 같은 `exec simai-tier0`(감시 없음, `tier0` 이미지 기본).
  `tier0,vllm,worker` 면 프로세스마다 `start_new_session`(= 새 프로세스 그룹)으로 띄우고 그룹에 신호를 보낸다. 기동·종료·재기동 규칙은 `deploy/README.md` "감시 프로세스".
  - 기동: Tier 0 가중치만 확인 → Tier 0 → 표시 파일 준비(`wait_tier0_ready.readiness` 를 그대로 불러 같은 판정 + 이번에 띄운 Tier 0 가 쓴 것인지: `gpu_priority.written_at` ≥ 띄우기 직전 시각, `gpu_priority.pid` = 띄운 프로세스(진입점이 exec 해 같은 pid). 이전 실행이 비정상 종료로 남긴 running 표시(mtime 새것)를 준비로 보지 않는다(리뷰 -246). 표시 파일은 지우지 않는다(Tier 0 의 previous_unclean 판정). 형식은 그대로(-60 의 기존 필드). 진입점이 TensorRT 엔진을 준비한 뒤에 서비스가 시작하고 GPU 선확보 뒤 `reserve.state=OK` 를 쓰므로, 엔진 빌드 중에 vLLM 이 메모리를 잡지 않는다) → 워커 가중치(스레드, 그동안 Tier 0 감시는 계속) → vLLM(`/health` 200) → 워커.
  - 재기동: 죽은 vLLM·워커의 그룹을 SIGKILL 로 정리(vLLM EngineCore 같은 손자가 GPU 메모리를 쥔 채 tini 아래 고아로 남지 않게, 자문 5) → 지수 백오프(5·10·20… 최대 300초) → **Tier 0 가 준비 상태일 때만** 다시 띄움.
    60초 안에 끝나거나 vLLM 이 시한(1800초) 안에 `/health` 를 못 넘기면 '빠른 실패', 5번 이어지면 포기(오래 돌다 끝나면 0 부터 다시 센다)(설정 오류·처리기 없음으로 무한히 돌지 않게). Tier 0 는 감시자가 재기동하지 않는다(명세 3.2 저널·체크포인트 불일치 때의 시작 거부를 덮지 않게).
  - 종료: Tier 0 가 끝나면 워커 → vLLM 을 내리고 같은 종료 코드. SIGTERM 은 워커 → vLLM → Tier 0(각 15초 뒤 그룹 SIGKILL).
  - 격리: 자식 환경에 HF 토큰 없음(지금 규칙 그대로, 워커 가중치는 감시자 프로세스 안 스레드가 받는다), vLLM·워커 스레드 제한(OMP·MKL·OpenBLAS·NumExpr = 1)·nice 10·CPU 고정(`SIMAI_SIDECAR_CPUS` auto: vCPU 8개 이상이면 뒤쪽 절반, Tier 0 는 묶지 않음),
    프로세스별 `TRITON_CACHE_DIR`·`TORCHINDUCTOR_CACHE_DIR`·`CUDA_CACHE_PATH`(`/tmp/simai-cache/<이름>`, 컨테이너 디스크: 남길 산출물이 아니다),
    vLLM 은 `127.0.0.1` 만·`HF_HUB_OFFLINE=1`, 워커 토큰 파일은 `simai-wait-tier0 --env-file` 로 워커에만. GPU VRAM 48GB급 미만이면 Tier 0 단독(명세 3.12 "48GB 미만에는 분석 부하를 올리지 않는다").
  - 설정 자리: `SIMAI_WORKER_ARGS`(워커 설정 파일 `--config`, 받을 작업 유형 VLM·`hmr_refine` 의 처리기 조립은 -227 등). vLLM 인자는 예산표 이름(`SIMAI_WORKER_VLLM_GPU_MEMORY_UTILIZATION`·`…_MAX_NUM_SEQS`)을 그대로 읽는다.
- **가중치 받기.** 대형 파일은 같은 이름의 `.part` 에 받는다. 항목마다 지우지 않는 잠금 파일(`weights/.locks/<경로>.lock`, flock·Windows msvcrt)을 쥐고 기존 파일 재확인·받기·해시·게시(rename)·스탬프까지 한 임계 구역으로 한다(Codex 리뷰 R1: `.part` 를 닫을 때 풀리던 잠금 틈에 다른 받기가 `.part` 를 바꿔 미검증 파일이 게시·스탬프되던 경쟁). 기다린 쪽은 먼저 끝난 쪽의 파일을 다시 확인한다. 잠금 파일을 지우거나 이름을 바꾸지 않아 inode 가 갈라지지 않는다. 끊김·429·5xx 는 지수 백오프로 5번, HTTP Range 로 이어받는다. Range 를 무시한 200·다른 Content-Range·416 은
  처음부터(이어붙여 깨진 파일을 만들지 않는다), 해시가 다르면 임시 파일을 지운다. 401·403·404 는 다시 하지 않는다. 다 실패하면 받은 바이트는 남겨 다음 시작이 잇는다.
  검증 스탬프는 tier0 묶음에 없는 항목만(Tier 0 가중치는 0.1GB 라 매번 해시) `weights/.verified/<이름>.json` 에 경로·sha256·크기·mtime_ns·inode·ctime_ns 를 둔다. 해시 전과 뒤의 stat 이 다르면 남기지 않는다. mtime 을 되돌린 복사·교체는 inode·ctime 이 달라 다시 해시한다. 모델 폴더(`weights/cosmos3-nano/`) 밖이라 vLLM 이 읽는 폴더에 섞이지 않고, Tier 0 의 `state/` 밖이다.
- **사이트 조건·실행 절차**(Pod형 RunPod 템플릿, VM형 `docker run --gpus all --stop-timeout 60`)는 `deploy/README.md` "통합 이미지".

### 버린 대안

- **새 Dockerfile(`deploy/unified/Dockerfile`)**: Tier 0 층 명령을 두 파일에 복사해 두어야 하고, 한쪽만 고치면 층이 갈라져 레지스트리 공유가 깨진다.
- **통합 이미지로 `tier0` 이미지를 대체**: Tier 0 단독 사이트(24GB급 GPU)에 워커 층 수 GB 를 받게 하고, 2차 세션까지 검증된 이미지 경로가 바뀐다. 타깃 둘로 두고 큰 층은 공유한다.
- **supervisord·s6-overlay**: 패키지가 늘고, Tier 0 와 워커의 비대칭 규칙(워커 장애는 재기동·Tier 0 는 건드리지 않음·Tier 0 종료가 곧 컨테이너 종료·Tier 0 준비 상태일 때만 재기동·워커 가중치 2단계)을 설정만으로 표현하기 어렵다.
- **Compose 다중 컨테이너**: Pod(RunPod)는 컨테이너 하나다. VM 도 같은 단일 컨테이너로 둔다(자문 1·7).
- **vLLM 을 `simai-wait-tier0 -- vllm serve` 로 감싸기**: 재기동 조건은 같지만 감시자가 `/health`·백오프·포기를 따로 알아야 해 판정이 두 곳에 생긴다. 판정 함수만 불러 쓴다.
- **잠금 밖 세 휠을 통합 이미지에도 넣기**: 시연 경로가 쓰지 않고(위 확인), 빌드 단계에 build-essential·pip·Detectron2 C++ 컴파일, 실행 이미지에 `--inexact` sync 가 필요해진다.
- **스탬프를 모델 파일 옆(`*.verified`)·Tier 0 `state/` 에**: 모델 폴더에 남의 파일이 생기고, Tier 0 상태 폴더는 Tier 0 단독 작성자다.
- **CUDA MPS·스트림 우선순위로 Tier 0 보호**: 자문 3(MPS 는 한 프로세스 장애가 Tier 0 까지 죽일 수 있고, 스트림 우선순위는 프로세스 사이에 듣지 않는다).

### 다음 자리: 외형 텍스처 굽기(-89, 사용자 결정 2026-10-23 EXP-02 시연 범위)

다시점 UV 투영(명세 20.6 2단계: 가시성 z-buffer 래스터화)은 이번에 패키지를 넣지 않는다. 넣을 때의 자리:
- 후보는 nvdiffrast(CUDA 래스터라이저 `RasterizeCudaContext`, GL 없이도 됨) 우선, PyTorch3D 는 torch 2.11 + cu130 휠이 없어 소스 빌드(수십 분·CUDA devel)라 뒤.
- nvdiffrast 는 PyPI 에 소스만 있고 import 때 `torch.utils.cpp_extension` 으로 CUDA 확장을 JIT 컴파일한다. 실행 이미지에는 nvcc·CUDA 헤더가 없으므로(베이스가 `cuda:13.0.3-base`)
  (가) `nvidia/cuda:13.0.x-devel`(다이제스트 고정) 빌드 단계에서 `TORCH_CUDA_ARCH_LIST="8.0;8.6;8.9;9.0;10.0;12.0"` 로 휠을 만들어 옮기거나, (나) 잠금에 `nvidia-cuda-nvcc`·헤더 휠을 넣고 JIT 캐시를
  `$SIMAI_ROOT/cache/worker/torch_extensions` 에 둔다. (가)가 GPU 세대 목록을 빌드에 묶는 대신 기동 시간이 일정해 우선이다.
- 층 위치: `unified-deps` 의 워커 sync 뒤, HMR 코드 사본 앞(잠금 밖 `--no-deps` 휠이라 잠금 설치 뒤, 코드 층보다 아래라 코드 변경에 다시 만들지 않는다). 코드 층의 sync 는 그때 `--inexact` 로 바꾼다(정확 sync 가 잠금 밖 휠을 지운다, 위 "설치 순서" 3).
- GL 백엔드(`RasterizeGLContext`)를 쓰면 `graphics` 기능·EGL(지금 들어 있음)이 필요하다. 점검은 `exp_image_check.py` 에 래스터화 1회(64×64)를 더한다.

### 빌드 결과 (노트북, 2026-10-23)

Docker Desktop 29.6.1(메모리 10GB), `TAG=s246 docker buildx bake unified tier0`. 로컬 태그 `simai/unified:s246`(풀면 24.3GB, ID `b00a4caeed9b`), `simai/tier0:s246`(18.7GB, ID `ac5041610fb1`).
푸시하지 않아 압축 크기·다이제스트는 Lead 푸시 때 정해진다. 처음 빌드의 대부분은 받기(TensorRT libs 3.8GB·torch·CUDA 휠, uv 캐시 마운트), 코드만 바뀐 재빌드는 위 두 층(약 6MB + 스크립트)만 다시 만든다.

| 층(풀린 크기) | GB | 공유 |
|---|---|---|
| cuda(CUDA 13 휠 16개, torch 2.11 이 요구하는 판) | 2.84 | tier0·unified |
| torch·triton·torchvision | 1.92 | tier0·unified |
| TensorRT libs | 4.65 | tier0·unified |
| 큰 일반 휠(polars·pyarrow·opencv-headless·scipy·av) | 0.78 | tier0·unified |
| Tier 0 나머지(uv sync) | 0.65 | tier0·unified |
| apt(EGL·X11·Mesa·gcc·git) | 0.54 | unified |
| 워커 큰 휠 8개(vllm·flashinfer-cubin…) | 2.89 | unified |
| 워커 나머지(uv sync, git 의존성 포함) | 0.82 | unified |
| Fast-SAM-3D-Body·DINOv3 코드 | 0.05 | unified |
| simai_ai·services·계약 | 0.01 | 각자 |

- `tier0` 이 21GB(2차 세션 이미지 `3926a8772649`) → 18.7GB 로 준 것은 onnxruntime-gpu·cupy 제외(약 0.7GB)와 torch 2.11 의 CUDA 휠 판 차이다. 큰 층 하나는 4.65GB(TensorRT libs, 이전과 같음)가 가장 크다.
- apt 층의 Mesa(`libegl-mesa0` 등)는 `libegl1` 의존성으로 같이 들어온다. GPU 가 있으면 glvnd 가 NVIDIA 공급자(`10_nvidia.json`)를 먼저 고른다(3차 세션 u1 의 EGL 렌더로 확인).
- `check_image.sh`: unified 19항목·tier0 16항목 모두 통과(비밀·가중치 파일 없음, import(vLLM 0.21.0·vllm-cosmos3 0.1.0 Reasoner 등록·transformers 5.9.0·TensorRT 11.4.0.106·onnxruntime 1.24.4, 잠금 밖 휠·onnxruntime-gpu·cupy·opencv-python 없음, HMR 경로·mhr70 70),
  진입점 오류 코드, 엔진 경로, Mesa EGL 렌더, 가짜 프로세스로 감시 순서(기동 tier0 → vllm → worker, `docker stop -t 60` 에 worker → vllm → tier0, 자식 토큰 없음, 종료 코드 0), SSH). 첫 실행에서 vLLM 의존성 compressed-tensors 의
  `hadamards.safetensors`(패키지 안 하다마드 행렬 표, 1.4MB)를 가중치로 잡아 그 경로만 점검에서 뺐다.
- 통합 이미지 안에서 `run_image.py` 가드: 렌더 요청은 종료 2(`--no-render` 안내), K 없는 그림은 종료 2(그림 이름).
- AutoCast CPU 재판정(통합 이미지, CPU onnxruntime 1.24.4·cupy 없음, 1단계 "사전 판정" 과 같은 스크립트·`yolo26x-pose.yaml` 무작위 가중치·640×1): ONNX 내보내기 5.2초(노드 1,238, opset 18), AutoCast 20.3초·최대 RSS 5,142MB,
  FP16 노드 1,249·Cast 26(→FP16 15, →FP32 11)·`onnx.checker` full 통과·입출력 같음 → 1단계(onnxruntime-gpu·cupy 있음) 결과와 같다.

### 잔여 위험

- 감시자·vLLM 기동·재기동은 가짜 프로세스로만 봤다. 실제 vLLM 0.21 + Cosmos3-Nano 의 적재 시간(1800초 시한 안인지)·`/health` 의미·EngineCore 가 같은 프로세스 그룹에 남는지(새 세션을 만들면 그룹 SIGKILL 이 닿지 않는다)는 3차 GPU 세션에서 실측했다: `/health` 까지 70.1 s(시한 1800 s 안), vLLM 리더만 `kill -9` 하면 EngineCore·resource_tracker 가 같은 그룹의 고아로 남아 그룹 SIGKILL 이 필요하다(아래 "3차 GPU 세션 결과" 측정 3·5). 감시자의 그룹 정리는 4차 세션 v4 가 감시 프로세스 코드로 다시 본다.
- nice·CPU 고정은 리더 프로세스를 띄운 직후에 걸어 그 뒤에 만드는 손자가 물려받는다. vLLM 이 그 사이(수 ms)에 손자를 만들면 그 손자는 nice 0·고정 없음이다. auto 의 '뒤쪽 절반' 은 초기값이다(v3 의 CPU 경합 측정으로 고친다). Tier 0 는 묶지 않아 sidecar 쪽 코어도 쓸 수 있다.
- vLLM 이 살아 있지만 멈춘 경우(`/health` 실패 지속)는 처음 뜰 때만 본다. 뜬 뒤의 멈춤·Tier 0 지연 초과 때 워커 그룹 SIGSTOP/SIGKILL(자문 3-5 2단계 서킷 브레이커)은 넣지 않았다(1단계 "새 작업 멈춤"은 -60 접수 문).
- 스탬프는 같은 inode 에서 크기·mtime 을 지키며 제자리에서 바꾼 내용(ctime 은 바뀐다)까지는 잡지만, 파일 시스템이 ctime 을 지키지 않거나 악의적인 변조는 보장하지 않는다(파일 시스템을 믿는다). 강제 재확인은 `weights/.verified/` 삭제. Tier 0 가중치는 스탬프를 쓰지 않는다.
- 표시 파일 소유 확인은 `simai-tier0` 이 Tier 0 서비스로 exec 해 pid 가 같다는 데 기댄다. `SIMAI_TIER0_CMD` 를 exec 하지 않는 감싸개로 바꾸면 워커 쪽이 뜨지 않는다(안전한 쪽). REPLAY 실행은 표시 파일이 `state/replay/<실행>/` 이라 워커 쪽을 띄우지 않는다.
- 로그 파일(`$SIMAI_ROOT/logs/<이름>.log`)은 회전하지 않는다. 시연 하루 분량은 작지만 vLLM 요청 로그가 많으면 커진다.
- RunPod Stop 이 SIGTERM 뒤 기다리는 시간을 확인하지 못했다(합 45초를 넘으면 Tier 0 가 SIGKILL 로 끝난다. 저널은 기록마다 fsync).
- `services.worker` 는 아직 처리기가 없어 바로 종료 1 로 끝나 감시자가 5번 뒤 포기한다(처리기 조립 -227 등 뒤에 정상).
- 통합 이미지의 HMR 은 import 까지만 노트북에서 봤다. 가중치 적재·추론은 3차 세션 u2(보정 K 경로, 렌더 없음). `hmr_refine` 작업이 LIVE 중에는 -60 접수 문 기본값(`SIMAI_WORKER_LIVE_BLOCKED_TYPES`)으로 막혀 있다: 시연 범위로 올린 결정과 맞추는 일은 -227·명세(§3.2).
- 48GB GPU 에서 VLM 0.55(26.4GB)에 Cosmos3-Nano BF16(31.5GB)이 들어가지 않을 수 있다(자문 7, 명세 결함 후보 -172). 감시자는 48GB급 이상에서만 워커 쪽을 띄우고 값은 예산표 이름 그대로 넘긴다.

## 3단계: MapAnything 을 같은 가상환경에, 장면 준비 모드 (S15P31A101-250)

명세 §3.2(프로세스·GPU 예산: Tier 0 가 GPU 를 먼저 쓴다), §3.12(GPU 실행 환경), §4.1·4.3(World 좌표계, MapAnything 초기 추정), §5.x 정적 배경(EXP-01).
코드는 `ai/pyproject.toml`(`scene` 묶음, import-linter `tier0-no-worker-stack`)·`ai/uv.lock`, `deploy/tier0/Dockerfile`(`ma-code` 단계, `unified`), `deploy/scripts/simai_bootstrap.py`(`scene`),
`deploy/scripts/check_image.sh`·`exp_image_check.py`, `deploy/scripts/gpu_session1.py`(u3·u4), `ai/experiments/static_scene/compare_envs.py`.
시험은 `ai/tests/deploy/test_simai_scene.py`, `ai/tests/experiments/static_scene/test_compare_envs.py`, `ai/tests/architecture/test_import_rules.py`(MapAnything 4개), `ai/tests/deploy/test_exp_images.py`·`test_gpu_session1.py`(u3·u4).

### 결정 (사용자 2026-10-23)

- **같은 가상환경(torch 2.11.0+cu130, Python 3.11, 잠금 하나).** 별도 venv·torch 2.7.1 한 벌을 더하지 않는다(CUDA·torch 휠이 이미지에 두 벌, 위 1단계 "결정" 과 같은 근거).
  map-anything @3d10cf7 은 `requires-python >=3.10`, uniception 0.1.7 도 `>=3.10` 이라 3.11 에 그대로 깔린다.
- **잠금: 기존 `scene` 묶음을 넓혔다**(새 묶음을 만들지 않음). `scene` 은 이미 `worker` 에 들어 있어 통합 이미지의 `uv sync --extra tier0 --extra worker` 가 그대로 깐다.
  `torch>=2.4` → `==2.11.0`(다른 묶음과 같은 판, 잠금 판은 원래 2.11.0). torch 밖은 모두 Linux 전용 marker(Windows 노트북 개발 환경은 바뀌지 않음).
  map-anything 자체는 잠금 밖(이미지가 커밋으로 받아 휠로 만들고 `--no-deps`), 의존성은 그 pyproject 의 dependencies 에서 추론 경로가 import 하는 것만 옮겼다.
- **뺀 의존성과 근거.** map-anything @3d10cf7 전체 `.py` 와 uniception 0.1.7 휠을 AST 로 따라가 추론 경로(`mapanything.models`·`utils.geometry`·`utils.image`·`utils.viz`, 그 아래 uniception 인코더, `run_mapanything.py`)의 import 를 모았다(287개 모듈).
  `tensorboard`·`natsort` 는 추론 경로에서 쓰지 않는다(tensorboard 는 학습 코드 `train/` 에서만, natsort 는 실행 import 를 찾지 못함), `orjson`·`python-box` 는 학습 데이터셋(`utils/wai`)에서만, `pillow-heif` 는 `utils/image.py` 의 `try` import(HEIF 입력 선택)라 뺐다.
  **`rerun-sdk` 는 남겼다**: `mapanything.utils.viz`(→ `predictions_to_glb`, `run_mapanything.py` 가 씀)가 모듈 수준에서 `import rerun` 하고 uniception 도 선언한다(map-anything 고정 `~=0.24.1` 그대로).
  `matplotlib` 도 `utils/hf_utils/viz.py`·`run_mapanything.py` 의 `colorize` 가 쓴다. `scipy` 는 `utils/hf_utils/viz.py` 가 import 하는데 map-anything 이 선언하지 않아 적었다.
  `opencv-python-headless==4.10.0.84` 고정은 Tier 0 판(`>=4.9`, 잠금 5.0.0.93)으로 합쳤다(아래 잔여 위험).
- **잠금 변화**: 287 → 298 패키지, **추가만 11개**(uniception 0.1.7, rerun-sdk 0.24.1, jaxtyping 0.3.11, wadler-lindig 0.1.7, scikit-learn 1.9.1, threadpoolctl 3.7.0, minio 7.2.20,
  argon2-cffi 25.1.0, argon2-cffi-bindings 26.1.0, pycryptodome 4.1.0, narwhals 2.27.1). 기존 패키지의 판·marker 는 하나도 바뀌지 않았다(잠금 전후 (이름, 판) 집합 비교.
  torch·torchvision·triton·tensorrt·ultralytics·numpy·opencv·scipy·onnx·modelopt·onnxruntime·pyarrow·av·vllm·transformers·huggingface-hub 같음).
  minio·scikit-learn·argon2·pycryptodome 은 uniception 이 선언한 의존성이라(추론 경로는 import 하지 않음) 잠금 안 패키지로 깔린다(뺄 방법은 override 뿐인데 uniception 판 변화에 약해 두었다).
- **Dockerfile(unified 만).** `ma-code` 단계가 map-anything·DINOv2 허브 코드를 커밋으로 받고 휠(`uv build --wheel`)을 만든다(사본의 `build/`·egg-info 는 지움).
  `unified` 는 소스 사본(`/opt/simai/mapanything/map-anything`, `SIMAI_MAPANYTHING_REPO`)과 허브 코드(`/root/.cache/torch/hub/facebookresearch_dinov2_main`)를 코드 층 아래에 두고,
  코드 층의 정확 sync **뒤에** 휠을 `--no-deps` 로 깐다(정확 sync 는 잠금에 없는 패키지를 지운다, 1단계 "설치 순서" 3). 휠은 단계에서 bind 로 읽어 층에 남기지 않는다.
  커밋은 `deploy/mapanything` 과 같고(전역 ARG `MA_COMMIT`·`DINOV2_COMMIT`) `SIMAI_MAPANYTHING_REPO`·`SIMAI_MAPANYTHING_COMMIT` 도 같은 이름이라 `run_mapanything.py`·`session3_jobs.py` 를 고치지 않았다.
  `tier0` 타깃은 바꾸지 않았다(설치 패키지 105개가 dev 이미지 `simai/tier0:62bab24f4dff` 의 `uv pip freeze` 와 같음).
- **장면 준비 모드 `simai-bootstrap scene [--] <명령...>`**(기존 `run`·`queue` 와 같은 하위 명령 꼴). 순서: LIVE 확인 → `static` 가중치(`SIMAI_WEIGHTS_SET` 과 상관없음) → LIVE·GPU 재확인 → 그 명령을 새 프로세스 그룹 자식으로 실행하며 Tier 0 시작을 감시(종료 코드 그대로, 끝나면 그룹을 정리해 VRAM 반환, Tier 0 가 시작하면 장면 작업을 끝내고 75). 처음에는 exec 였으나 실행 중 선점을 하려고 자식으로 바꿨다(Codex 리뷰 R1).
  거부는 종료 코드 75(sysexits `EX_TEMPFAIL`: 나중에 다시 하면 되는 상태. 기존 2·3·64·127 과 겹치지 않게).
  - **"LIVE 가 떠 있으면 거부"를 골랐다.** "같이 지정하면 거부"만으로는 구분할 수 없다: 통합 이미지가 `SIMAI_SERVICES=tier0,vllm,worker` 를 기본 ENV 로 가져 지정 여부가 LIVE 여부를 말하지 않는다. 구분 방법 두 가지:
    (1) Tier 0 표시 파일(`state/tier0.live`, `SIMAI_TIER0_MARKER`)이 30초 안에 갱신됨. Tier 0 는 5초마다 쓰고, 30초는 세션 큐 `live_guard` 의 `marker_max_age_s` 와 같은 값이다(비정상 종료로 남은 오래된 표시는 무시). 확인 실패·미래 시각은 모름 → LIVE.
    (2) 명령 직전 `nvidia-smi memory.used` > 1024 MiB 또는 못 읽음. 표시 파일로 안 보이는 사용을 잡는다: Tier 0 진입점의 TensorRT 엔진 빌드(약 550초, 이때는 표시 파일이 아직 없다), REPLAY, 다른 컨테이너.
    1024 MiB 근거: 빈 GPU 는 0~수 MiB, 다른 프로세스의 빈 CUDA 문맥은 약 0.5 GiB, Tier 0(엔진 + 선확보)·vLLM 은 수 GiB 이상.
  - 받기 전과 명령 직전에 두 번 본다(static 가중치 두 벌을 받는 수 분 동안 Tier 0 가 떴을 수 있다). 받기 전에도 GPU 사용 중 메모리를 본다(엔진 빌드·다른 컨테이너 중에 수 GB 를 받지 않게, 리뷰 -250). 받기 전에 못 읽는 것은 준비의 GPU 단계(없으면 코드 3)에 맡기고, 명령 직전에 못 읽으면 거부한다. 거부하면 부트스트랩 기록에 `stage: scene`·`exit: 75` 를 추가한다(준비 기록 exit 0 만 보고 성공으로 읽지 않게).
  - "같이 지정"도 막는다: `SIMAI_SERVICES` 에 `scene` 을 섞으면(`tier0,scene` 등) 받기 전에 64 와 `simai-bootstrap scene` 안내.
  - HF 토큰은 지금 규칙 그대로(받기에만, 명령 환경·출력·기록에 없음).
  - **Tier 0 와 상호 배제(Codex 리뷰 R1, Tier 0 우선).** 두 번 읽는 확인만으로는 확인과 실행 사이(최종 GPU 조회 최대 60초 동안, exec 직전, 실행 중)의 Tier 0 기동을 막지 못했다. 공유 flock 두 개로 바꿨다: `tier0` 명령은 가중치 준비 전에 `locks/tier0.lock` 공유 잠금을 쥐고 fd 를 상속해 exec(simai-tier0 → services.tier0)를 지나 끝날 때까지 쥔다. 장면 모드는 `locks/scene.lock` 배타 잠금을 쥐고(두 장면 작업도 동시에 못 돈다), 확인 끝에 tier0.lock 을 배타로 잠깐 잡아 보아 Tier 0 가 있는지 보고, 명령을 새 프로세스 그룹 자식으로 돌리며 0.5초마다 같은 확인(+표시 파일)을 한다. Tier 0 가 보이면 그룹 SIGTERM → 10초 → SIGKILL 하고 75(기록). Tier 0 시작 경로는 exec 전에 scene.lock 이 풀리기를 최대 20초(감시 주기 + 시한 + 여유) 기다려 장면 작업이 VRAM 을 돌려준 뒤 진행하고, 시한이 지나도 그대로 시작한다. 감시 부모가 멈추거나 죽어도 장면 작업이 남지 않게(Codex 재리뷰 R1): 장면 모드는 자식 그룹을 `locks/scene.pgid`(pgid·시작 표지, 원자적 쓰기)에 남기고, Linux 에서는 자식에 `PR_SET_PDEATHSIG`(SIGKILL)를 건다. Tier 0 시작 경로는 잠금이 풀렸거나(부모 소멸) 시한이 지나도(부모 정지) 그 그룹이 살아 있으면 직접 SIGTERM → 10초 → SIGKILL 로 회수하고 사라진 것을 확인한 뒤 진행한다(잠금 해제·시한을 VRAM 반환으로 보지 않는다). 시작 표지가 다르면(pid 재사용) 건드리지 않고, 회수하지 못하면 기록(`stage: tier0-reap-scene`)하고 진행한다(Tier 0 우선). **의도된 정책(Codex 3차 재리뷰, Lead 결정): Tier 0 가용성이 장면 작업보다 우선이다.** 회수하지 못하거나(신호 거부·시한 초과) 건드리지 않은 경우(아래) Tier 0 는 막지 않고 큰 경고 로그와 부트스트랩 기록(`stage: tier0-reap-scene`, `exit: 75`)을 남긴 뒤 시작한다. Tier 0 를 막는 쪽(관제 불가)이 장면 작업과 GPU 를 잠깐 나누는 쪽보다 위험하다고 본다. **모르면 건드리지 않는다**: 기록의 PID namespace(`/proc/self/ns/pid`)가 Tier 0 쪽과 다르거나 모르면, 또는 시작 표지가 기록·지금 중 하나라도 없으면 신호를 보내지 않고 기록을 남긴다. KILL 전에 시작 표지를 다시 본다. **같은 컨테이너 전제**: 상호 배제·회수는 통합 이미지의 같은 컨테이너(같은 PID namespace) 안에서 장면 모드를 돌리는 것을 전제로 한다(`docker exec` 로 같은 컨테이너, Pod 는 SSH 로 같은 컨테이너). 다른 컨테이너의 장면 작업은 잠금으로 보이지만 신호는 보내지 않는다(그 컨테이너가 맡는다). 자식은 시작 handshake(POSIX: 표준 입력에서 1바이트를 받은 뒤에만 exec)로 그룹 기록이 공개된 뒤에만 장면 명령을 시작하고, 기록을 못 쓰면 시작시키지 않고 75. `/proc` 를 읽을 권한이 없는 것(EACCES·EPERM)은 살아 있음(모름), 항목이 없음(ENOENT·ESRCH)만 끝남으로 본다. Tier 0 는 최악 약 40초(잠금 20초 + TERM 10초 + 확인 10초) 늦게 시작할 수 있다. 리더가 먼저 끝나고 손자만 남은 그룹은 리더 시작 표지를 읽을 수 없어 신호를 보내지 않는다(남의 그룹일 수 있다: 남은 파일은 원래 그룹이 이어졌다는 증거가 아니다). 이 경우 손자가 GPU 를 쥐고 있을 수 있어 경고한다. flock 을 고른 근거: 커널이 프로세스 종료 때 풀어 남은 표시 파일·pid 재사용 문제가 없고, 같은 호스트의 다른 컨테이너(같은 저장 위치를 붙인 경우)·PID 네임스페이스와 상관없이 보인다. Tier 0 쪽이 잠금을 열지 못해도 Tier 0 는 막지 않고, 장면 쪽은 확인 못 함 → 거부.
- **import-linter**: `tier0-no-worker-stack` 의 금지 목록에 `mapanything`·`uniception`·`rerun` 을 더했다(같은 가상환경이라 설치돼 있어도 Tier 0 경로에서 막는다). `simai_ai.scene` 은 원래 금지라 그것을 거친 간접 import 도 막힌다.

### 동등성 비교 (torch 2.7.1 이미지 ↔ 통합 환경)

같은 입력(EXP-01 room4 4장)·같은 가중치·같은 인자로 m1·m2(`mapanything` 이미지)와 u3·u4(통합 이미지, 장면 준비 모드)를 돌리고 노트북에서
`uv run --project ai python ai/experiments/static_scene/compare_envs.py --base <m Run 폴더> --new <u Run 폴더> [-o <결과.json>]` 로 판정한다(종료 0 PASS, 1 FAIL, 2 비교 불가). 입력은 각 Run 의 `metrics.json`·`cameras.json`·`raw.npz` 다.
비교 불가: SUCCESS 가 아님, `input_hash`·`weights_hash`·그림 목록·`prepared_sha256`·인자(mode·known_from·amp·amp_dtype·minibatch·conf_percentile·use_poses·pose_scale)·시점 목록·모델 입력 크기가 다름, 손상·불완전 Run(파일을 못 읽음·손상 zip(BadZipFile)·잘린 파일, 수가 아닌 raw 배열(문자열·객체), `args` 없음, 시점의 카메라 항목·raw 배열 없음, 배열 모양 불일치 — 트레이스백이 아니라 종료 2). `known_from` 은 run_mapanything.py 가 해시를 남기지 않아 경로로 본다.
잴 수 없는 값은 FAIL 이다(모르면 통과로 두지 않는다): 시점별 값 중 하나라도 None·NaN·Inf 이면 그 항목 전체가 잴 수 없음(내장 max/min 은 NaN 을 비교에서 버려 뒤 시점 NaN 이 통과하던 결함, 리뷰 -250), 비유한 회전 행렬(clip 이 +inf 를 1 로 바꿔 0도로 보이던 결함, Codex 리뷰 R2), 공통 유효 픽셀이 없는 시점, 척도 계수가 양의 유한 실수가 아님(bool·0·음수·없음), 기준 카메라 두 대가 한 점(거리 ≤ 1e-9, 작은 수로 나누지 않는다). 결과 JSON 의 잴 수 없는 값은 null(NaN 을 쓰지 않음).

| 값 | 합격 | 근거 |
|---|---|---|
| 카메라 쌍 거리 비(new/base) 최소·최대 | 0.99~1.01 | E-GATE-0 대조 기준(`docs/design/exp-images.md`)과 같은 값. 같은 환경 bf16↔fp32 차이 0.998 |
| 카메라별 회전 차이 최대 | ≤ 0.2° | 같은 표(bf16↔fp32 0.07°) |
| 가로 FOV 차이 최대 | ≤ 0.3° | 같은 표(0.02°) |
| `metric_scaling_factor` 상대 차이(모든 시점, 최대) | ≤ 1% | 같은 표(0.08%). 시점마다 기록되므로 첫 시점만 보지 않는다 |
| 시점별 유효 깊이 중앙값 상대 차이 | ≤ 1% | 같은 표(0.13~0.17%) |
| 같은 픽셀 \|Δd\|/d 중앙값(시점별 최대) | ≤ 1% | 위 깊이 중앙값과 같은 예산을 픽셀 대응으로 |
| 같은 픽셀의 world 점 거리 / 기준 깊이, 중앙값(전 시점) | ≤ 1% | 점 = 깊이 × 광선이라 깊이와 같은 예산. 깊이·내부 파라미터·자세 차이를 한 번에 본다(점구름 판정) |
| 기록만: 픽셀 \|Δd\|/d p95, 유효 마스크 IoU 최솟값, 점구름 대칭 최근접 거리(중앙값·p95 / 기준 점구름 p1~p99 대각선), 카메라 중심 차이 최대 | — | 근거 값이 없다(E-GATE-0 는 중앙값만 쟀다). 3차 세션 결과로 정한다 |

- torch 2.7.1+cu128 → 2.11.0+cu130 은 CUDA·cuDNN·SDPA 커널이 함께 바뀐다. 허용 폭은 cu126 → cu128 이미지 교체 때 정한 E-GATE-0 대조 기준을 그대로 쓴다(새 수치를 지어내지 않음). 하나라도 어긋나면 FAIL(HOLD)로 두고 원인을 본다.
- 점구름 대칭 최근접 거리는 면을 따라 미끄러진 차이(깊이 척도·전체 이동)를 잘 못 잡는다(합성 시험에서 깊이 2% 차이를 판정하지 못했다). 그래서 판정은 같은 픽셀 대응이 하고, 이 값은 유효 영역이 달라졌을 때의 모양 차이 기록으로 둔다.
- 비교의 GPU: 같은 GPU 모델 결과끼리 비교하는 것이 깨끗하다. 3차 세션에서 mapanything Pod(m1·m2)와 통합 Pod(u3·u4)를 같은 GPU 종류로 띄운다(결과 `env.same_gpu_model`). 다르면 판정은 하되 그 사실을 MR 에 적는다.
- 리뷰 반례(-250): 뒤 시점 FOV·깊이 중앙값 NaN·자세 NaN FAIL, 뒤 시점 척도 없음·3%·bool·음수 FAIL, known_from 다름 비교 불가, raw 키 없음·args 없음·배열 크기 다름·자세 모양 틀림 종료 2, 기준 카메라 한 점 FAIL·JSON null(모두 수정 전 코드에서 실패 확인).
- 시험(합성 Run, `test_compare_envs.py`): 같음·0.2% 잡음 PASS, 깊이 0.9% PASS·2% FAIL, 카메라 하나 0.5° 회전·FOV·척도 2%·전체 이동 0.3 m FAIL(각각 해당 항목), 유효 픽셀 없음·척도 없음 FAIL, 입력·가중치·인자·시점·결과 다름 비교 불가, 결과 파일 덮어쓰기 거부.

### 버린 대안

- **별도 venv·torch 2.7.1 한 벌 추가**: 사용자 결정으로 하지 않는다.
- **새 extra(`mapanything`)를 만들어 `worker` 에 넣기**: `scene` 이 이미 정적 장면 묶음이고 `worker` 에 들어 있다. 묶음 이름만 늘고 하는 일은 같다.
- **map-anything 을 설치하지 않고 `SIMAI_MAPANYTHING_REPO` 의 sys.path 만으로**: `run_mapanything.py` 는 되지만 다른 호출부(나중의 워커 장면 처리기)마다 경로를 넣어야 하고 `importlib.metadata` 판 확인이 안 된다.
- **휠을 코드 층 아래에 깔고 코드 층 sync 를 `--inexact` 로**: 2단계가 정확 sync 로 정했다(잠금 밖 찌꺼기가 남지 않게). 휠 층(약 7MB)이 코드 층 위에 있어 코드 변경마다 몇 초 다시 만드는 쪽을 골랐다.
- **`SIMAI_SERVICES=scene`**: `SIMAI_SERVICES` 는 감시자가 계속 띄우고 재기동하는 서비스 목록이다. 끝나는 일회성 작업을 섞으면 감시·재기동 규칙이 맞지 않는다.
- **장면 작업 중에 Tier 0 기동을 막기(장면이 이김)**: Tier 0 가 자원을 먼저 쓴다(AGENTS.md). 막으면 관제가 장면 작업에 밀린다. 대신 장면 작업이 선점돼 멈춘다(위 상호 배제).
- **시작 의도 표시 파일(pid 기록)**: 비정상 종료 때 남은 파일·pid 재사용·다른 PID 네임스페이스에서 판단이 틀린다. flock 은 커널이 정리한다.
- **`rerun-sdk` 빼기**(`predictions_to_glb` 를 `utils/hf_utils/viz.py` 판으로 바꾸기): 두 함수가 달라 GLB 산출물이 2.7.1 이미지와 달라지고, uniception 이 어차피 rerun-sdk 를 끌어온다.

### 빌드 결과 (노트북, 2026-10-23)

Docker Desktop 29.6.1(메모리 8GB), `TAG=s250 docker buildx bake -f deploy/docker-bake.hcl unified tier0`(Codex 3차 재리뷰 반영 최종 코드, 두 이미지 모두 다시 빌드). 로컬 태그 `simai/unified:s250`(풀면 24.8GB, ID `82607b65fa14`, check_image 24항목), `simai/tier0:s250`(18.7GB, ID `016d2f9c0b0e`, check_image 16항목). 검증 기준: 두 이미지 안의 `simai-bootstrap` = 저장소 `deploy/scripts/simai_bootstrap.py`(git blob `040b807ffb4d`), `deploy/tier0/Dockerfile` blob `922db9f6f612`(문서는 이미지에 들어가지 않는다). 같은 unified 이미지 컨테이너 안에서 `ai/tests/deploy/test_simai_scene.py` 를 Linux 로 돌려 53 passed(POSIX 전용 handshake·SIGSTOP 회수·PDEATHSIG·/proc 권한 시험 포함).
푸시하지 않아 압축 크기·다이제스트는 Lead 푸시 때 정해진다. 비교 기준은 dev(-246 머지) 로컬 이미지 `simai/unified:62bab24f4dff`(24.3GB)·`simai/tier0:62bab24f4dff`.

| 층(풀린 크기) | 2단계 | 이번 | 비고 |
|---|---|---|---|
| 워커 나머지(uv sync, git 의존성 포함) | 0.82GB | 1.18GB | +0.36GB: rerun-sdk 0.24.1(휠 90.7MB) 등 잠금 추가 11개 |
| map-anything 소스 사본 | — | 14MB | `/opt/simai/mapanything/map-anything`(build/·egg-info 지움) |
| DINOv2 허브 코드 | — | 4.8MB | `/root/.cache/torch/hub/facebookresearch_dinov2_main` |
| map-anything 휠 설치(코드 층 위) | — | 6.8MB | 코드가 바뀌면 다시 만든다(몇 초) |
| 그 밖의 층 | 같음 | 같음 | Tier 0 층·apt·워커 큰 휠 8개·HMR 사본·코드 |

- 늘어난 크기 약 0.39GB(풀린 크기). `tier0` 이미지는 설치 패키지가 같다(`uv pip freeze` 105개 동일). 다만 잠금이 바뀌어 Tier 0 나머지 sync 층(약 0.65GB)부터 위가 다시 만들어졌다(아래 잔여 위험).
- 통합 이미지 `uv pip freeze` 는 dev 이미지보다 12개 많다(잠금 추가 11개 + `mapanything` 1.1.4). 빠진 것 없음.
- `check_image.sh`: unified 24항목(2단계 19 + MapAnything 5: 추론 경로 import·판·사본, DINOv2 허브 사본으로 망 없이 골격 생성(`--network none`, vits14 22,056,576 매개변수), 장면 모드 명령 없음 64·LIVE 표시 파일 75·static 받기 실패 2·가짜 GPU 로 명령 종료 코드 7 그대로·자식 토큰 없음·사용 중 2048 MiB 면 75), tier0 16항목 모두 통과.
- 음성 대조(이미지): DINOv2 사본을 지운 컨테이너에서 망 없이 `torch.hub.load` 가 `RuntimeError`(캐시 없음). 첫 빌드 이미지(사본에 build/·egg-info 남음)에서 새 점검이 그 두 이름으로 실패 → `ma-code` 에서 지우고 다시 빌드해 통과.

### 잔여 위험

- 통합 환경 MapAnything 의 실제 추론(가중치 적재·bf16 SDPA·결과 동등성)은 GPU 에서만 본다(3차 세션 u3·u4 + `compare_envs.py`). 노트북은 import·허브 사본·장면 모드만 봤다. FAIL 이면 2.7.1 이미지를 계속 쓴다.
- map-anything 이 고정한 `opencv-python-headless==4.10.0.84` 대신 5.0.0.93 이다(Tier 0 판). 쓰는 곳은 그림 읽기·크기 바꾸기(`utils/image.py`·`cropping.py`)라 위험은 작지만 u3·u4 비교가 본다.
- 픽셀 p95·마스크 IoU·점구름 거리는 근거가 없어 기록만 한다. 첫 결과로 기준을 정하는 일은 이 비교 뒤(-172 명세 반영과 함께).
- GPU 사용 중 메모리 상한 1024 MiB 는 초기값이다. MIG·vGPU 처럼 `memory.used` 의 뜻이 다른 사이트에서는 거부가 잦을 수 있다(모름 → 거부, 안전한 쪽).
- 상호 배제는 같은 저장 위치(`$SIMAI_ROOT/locks/`)를 쓰는 프로세스끼리만 보인다. 다른 저장 위치를 쓰는 장면 작업은 표시 파일·잠금으로 못 보고 GPU 사용 중 메모리로만 거른다(한 시점 GPU 인스턴스 하나·같은 Pod 운영이라 실제로는 같은 위치). Tier 0 는 장면 작업이 멈추기를 기다리고 남은 그룹을 회수하므로 최악 약 40초(잠금 20초 + TERM 10초 + 확인 10초) 늦게 시작할 수 있다. 회수하지 못하거나 신원을 확인하지 못한 장면 작업은 Tier 0 와 GPU 를 나눌 수 있다(의도된 정책, 경고·기록 75 로 사람이 확인). 실제 Linux 프로세스 그룹 선점은 check_image(컨테이너)와 POSIX 시험으로 본다.
- 잠금이 바뀌면 `tier0-deps` 의 Tier 0 나머지 sync 층(0.65GB)과 그 위가 다시 만들어진다(`COPY ai/pyproject.toml ai/uv.lock` 가 그 층 바로 앞, 2단계 설계 그대로). 이번에도 `tier0` 이미지의 의존 패키지 판은 같지만 그 층 다이제스트가 바뀌고, bootstrap(시작 잠금·장면 대기·회수)은 바뀌어 다시 올려야 한다.

### 넘긴 일

- Tier 0 나머지 층을 `uv export --extra tier0` 목록(내용 체크섬) 설치로 바꿔 워커 쪽만 바뀐 잠금이 `tier0` 층을 다시 만들지 않게 하기(2단계 설계 변경, Lead 판단).
- u3·u4 PASS 뒤: -102 w1·w2 를 통합 이미지로(`session3_jobs.py --py /opt/simai/ai/.venv/bin/python`, 명령을 `simai_bootstrap.py scene` 으로 감쌈), `deploy/mapanything` 정리, 기록만 지표의 기준 정하기.
- 워커 장면 처리기(`simai_ai.scene` 의 MapAnything 작업 유형)는 이번 범위가 아니다(장면 준비는 사람이 `scene` 명령으로).

## 3차 GPU 세션 결과(2026-10-26, S15P31A101-278)

RunPod H100 80GB HBM3, 통합 이미지 `sha256:145b767262846c490686401880fd82df5d457f4d842f7a9d275195e95095d197`, vLLM 0.21.0. 명세 §3.2(GPU 예산·워커 보호), §3.12, §5.4.
감시 프로세스 `Supervisor._vllm_argv` 와 같은 인자·환경(스레드 1, nice 10, `VLLM_USE_DEEP_GEMM=0`, `HF_HUB_OFFLINE=1`)으로 vLLM 을 띄웠다.
원본은 노트북 `ai/experiments/runs/gpu-session1/20261026-s3/v-int/summary.json`(실패 로그 `v-int-fail1/vllm.log`·`v-int-fail2/vllm.log`, 커밋 안 함).
코드는 `deploy/scripts/simai_bootstrap.py`(vLLM 기본값·CPU 묶기, cgroup 할당량 읽기를 `session1_live.py` 에서 옮겨 c0 도 같은 함수를 쓴다), 시험은 `ai/tests/deploy/test_simai_supervisor.py`.

### 측정

1. 실패 1: FlashInfer top-k/top-p 샘플러가 메모리 측정(`determine_available_memory`) 중 JIT 컴파일 → `/usr/local/cuda/bin/nvcc: not found`(runtime 이미지) → Engine core 초기화 실패.
   `VLLM_USE_FLASHINFER_SAMPLER=0` 이면 "FlashInfer top-p/top-k sampling disabled … using PyTorch-native sampler" 로 넘어간다. 어텐션은 FLASH_ATTN 백엔드(FlashInfer 아님).
2. 실패 2: `--max-model-len` 없음 → 모델 기본 262,144 토큰, 한 요청에 KV 36.0 GiB 필요 > 0.55 에서 남는 KV 23.99 GiB → ValueError.
3. 둘 다 넣고 성공: 기동 → `/health` 70.1 s(시한 1800 s 안), 가중치 16.79 GiB(3.6 s), KV 23.99 GiB = 174,688 토큰, 32,768 기준 최대 동시 5.33x. GPU 사용 최대 약 54.2 GB(Tier 0 선확보 8 GB 포함).
   이미지 1280 px + 질문, 답 최대 256 토큰: 동시 1 전체 p95 2.04 s, 동시 4 2.10 s, 10분 지속 동시 4(1,088회) 2.12 s, 오류 0(G7 p95 ≤ 5 s 통과). 영상(mp4 data URL) 입력도 된다.
4. Tier 0 영향(c4 와 같은 LIVE 하네스 입력, 단독 → VLM 동시 4 지속): `frame_age` p95 60 → 76 ms, `pipeline_lag` p95 148 → 154 ms, reserve OK 유지, ready 4/4, `decode_failed`·`detector_errors` 0,
   Tier 0 CPU 142% → 192%, loadavg 최대 85.
5. `kill -9`(vLLM 리더만): 10 s 뒤 EngineCore·resource_tracker 가 같은 pgid 의 고아로 남아 GPU 53.7 GB 유지 → 그룹 SIGKILL 뒤 9.8 GB. 감시 프로세스의 그룹 정리(2단계 "재기동") 설계가 필요함을 실증했다.
6. CPU: Pod 는 `sched_getaffinity` 224개, cgroup 상한 23.8 vCPU(`/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_quota_us`, cgroup v1). `SIMAI_SIDECAR_CPUS=auto` 가 224 기준 뒤쪽 112-223 에 묶어
   실제로 제한 효과가 없었다(상한은 컨테이너 전체가 나눠 쓰므로 vLLM 이 112개에 퍼질 수 있다).

### 결정

- **v1·v2·v3 를 통합 시험 하나로 합쳤다**(Lead 결정). 위 "3차 GPU 세션 큐" 표의 v1(단독 적재·VRAM)·v2(G7 지연)·v3(Tier 0 + VLM 동시·kill -9)를 감시 프로세스와 같은 인자로 띄운 한 번의 실행(v-int)에서 단계별로 쟀다.
- **`--max-model-len` 기본 32768**(`SIMAI_WORKER_VLLM_MAX_MODEL_LEN`. `SIMAI_VLLM_EXTRA_ARGS` 에 `--max-model-len`(`=`·밑줄 표기 포함)이 있으면 그 값만 쓰고 넣지 않는다). 근거: 0.55 의 KV 174,688 토큰을
  32,768 로 나누면 5.33 이라 `--max-num-seqs` 4 를 다 받고, 이미지 1280 px + 질문 + 답 256 토큰 요청에 충분하다. 모델 기본(262,144)은 기동이 안 된다.
- **`VLLM_USE_FLASHINFER_SAMPLER=0` 기본**(`setdefault`, 컨테이너 환경에 주면 그 값). runtime 이미지에 nvcc(devel 층, 수 GB)를 넣는 것보다 작고, 샘플러만 PyTorch 로 바뀐다(어텐션은 원래 FLASH_ATTN).
- **auto CPU 묶기는 cgroup 할당량을 본다.** 쓸 수 있는 vCPU 수 n = min(affinity 개수, cgroup 할당량의 올림)(v2 `cpu.max`·v1 `cpu.cfs_quota_us/cpu.cfs_period_us`, 잎·조상 중 가장 작은 값.
  못 읽거나 제한 없음이면 affinity 개수). n ≥ 8 이면 affinity 목록의 뒤쪽 n//2 개를 vLLM·워커에 묶고, 8 미만이면 묶지 않는다(기존 규칙). 위 Pod 는 24 → 뒤쪽 12개(212-223).
  할당량이 없으면 짝수 개수에서 기존과 같다(홀수면 Tier 0 쪽이 하나 더). 계산 결과(affinity 개수·할당량과 파일·묶은 범위)는 감시 프로세스 시작 로그 한 줄에 남는다. Tier 0 는 여전히 묶지 않고 신호·제한을 주지 않는다.
  cgroup 읽기는 c0(`session1_live.py`)이 쓰던 함수를 `simai_bootstrap.py` 로 옮겨 둘이 같은 판정을 쓴다(이미지에는 bootstrap 만 복사되므로 의존 방향을 live → bootstrap 으로).
  2단계 "격리"·잔여 위험의 'vCPU 8개 이상이면 뒤쪽 절반' 은 이 규칙으로 바뀌었다.
- **명세 §3.2 예산(VLM 0.55)은 고치지 않는다.** 가중치 16.8 GiB + KV 24 GiB 가 0.55(H100 약 44 GB) 안에 들어가고, G7·Tier 0 보호 기준을 넘지 않았다. 48GB급 GPU 의 0.55 는 위 "2단계" 잔여 위험(-172) 그대로다.

### 남은 일·잔여 위험

- VLM 답 반복: 같은 문장을 14회 반복한 답이 1건 있었다. 반복 억제(`repetition_penalty`·`frequency_penalty`)·형식 강제(JSON 스키마·guided decoding)는 워커 쪽 요청 인자로 한다(감시 프로세스의 서버 인자 아님).
- 지연 측정은 같은 그림을 되풀이해 보냈으므로 prefix 캐시 덕에 첫 토큰·전체 지연이 낙관적일 수 있다. 실제 카메라 그림(매번 다름)으로 다시 잰다.
- 새 CPU 묶기(뒤쪽 12개)는 이번 측정에 쓰지 않았다(측정은 112-223 묶음). vLLM CPU 중앙값이 약 119%(1.2 코어)라 12개면 넉넉하다고 보지만, loadavg 85 처럼 할당량 23.8 을 넘는 실행 대기가 Tier 0 에 주는 영향은 다음 GPU 세션에서 새 묶음으로 다시 본다.
  → 4차 세션 큐 v4(`deploy/scripts/vlm_int.py`, S15P31A101-291)가 이 v-int 를 감시 프로세스 코드가 만드는 인자·환경·CPU 묶기 그대로 다시 잰다(요청마다 질문 앞 표식으로 prefix 캐시 영향을 줄임). 실행서 `docs/design/runpod-environment.md` "4차 세션".
- 할당량은 소수 둘째 자리 반올림 뒤 올림이라 7.999 같은 값은 8 로 본다(경계 한 코어 차이). 실행 중 cgroup 변경은 보지 않는다(시작 때 한 번).

## 2단계로 넘긴 일 (1단계 때 적은 것)

S15P31A101-246 이 위 "2단계" 로 처리한 것: 통합 Dockerfile·opencv-python 빼기·EGL·`NVIDIA_DRIVER_CAPABILITIES` 합집합·감시 프로세스·프로세스별 캐시·onnxruntime-gpu·cupy 정리.
남은 것: 지연 초과 지속 때 워커 그룹 강제 중단(서킷 브레이커 2단계), exp02 이미지를 통합 잠금으로 옮길지, 예산표 §3.2 정리(-172).


- 통합 Dockerfile(이 잠금 하나로 Tier 0 + 워커 + vLLM, 잠금 밖 휠 설치 순서, `deploy/tier0/Dockerfile` 묶음 목록에서 `opencv-python` 빼기, EGL·X11 라이브러리, `NVIDIA_DRIVER_CAPABILITIES` 합집합 compute·utility·video·graphics, 이미지 전역 `VIRTUAL_ENV` 정리).
- `simai-bootstrap` 감시 프로세스(프로세스 그룹 분리, Tier 0 먼저·워커 가중치(`vlm` 35GB 중 31.5GB)는 뒤 단계, 종료 순서 워커 → vLLM → Tier 0, 지연 초과 지속 시 워커 그룹 SIGKILL, 스레드 제한·nice, 프로세스별 TRITON·Inductor 캐시).
- `exp02` 이미지를 통합 잠금으로 옮길지(E-GATE-0 대조 기준 torch 판 줄 `compare_egate0.EXPECTED` 에 2.11.0 추가 필요), `deploy/exp02/requirements.in` 정리.
- Tier 0 의 CUDA 12 계열 미사용 짐(`onnxruntime-gpu`·`cupy-cuda12x`, 약 0.65GB, modelopt[onnx] 의존성) 정리: override 가 필요하고 엔진 빌드를 GPU 에서 다시 봐야 한다.
- 명세 §3.2 예산표의 VLM 0.55(48GB 에서 26.4GB)는 v1 의 Reasoner VRAM 실측 뒤 §3.2·§3.12 정리(-172, 명세 결함 후보).


## 4단계: 로비 LIVE·아바타·워커·vLLM 단일 Supervisor 통합 및 GPU 상한·기동 전 점검 교정 (S15P31A101-382)

9차 세션에서 수동 스크립트(`live-s9.sh`, `avatar-s9.sh`, `worker-s9.sh`, `lobby_live_s9.py`, `chain-s9.sh`, `static-s9.sh`)로 프로세스를 쪼개 띄우면서 드러난 네 가지 문제를 교정했다.

1. **`Supervisor` GPU 메모리 상한 상시 적용 (`deploy/scripts/simai_bootstrap.py`)**
   - 이전에는 `"avatar" in services` 일 때만 `SIMAI_WORKER_TORCH_MEMORY_FRACTION` 을 계산해, 기본 `tier0,vllm,worker` 구성에서 `worker` 가 기본값 `0.70` 으로 뜨면서 `0.20 + 0.55 + 0.70 = 1.45 > 0.95` 로 예산을 넘겼다.
   - 이제 `compute_gpu_fractions` 가 `avatar` 포함 여부와 무관하게 `budget.worker_fraction - vllm_fraction - avatar_fraction`(`tier0,vllm,worker` 는 `0.15`, `tier0,vllm,worker,avatar` 는 `0.05`)을 계산·검증해 `SIMAI_WORKER_TORCH_MEMORY_FRACTION` 으로 주입하며, 합이 예산을 넘으면 기동 전에 종료 코드 64 로 거부한다.
2. **`services.worker` 메모리 상한 호출 순서 교정 (`ai/services/worker/__main__.py`)**
   - `build_handlers`(내부 `open_pose` 가 `YOLO26x-pose` 가중치를 GPU 에 적재) 호출보다 **먼저** `WorkerGpuSettings.from_env` 검증과 `cap_process_memory(torch_fraction)` 를 수행하도록 순서를 바로잡았다.
3. **로비 LIVE 통합 기동 (`simai-bootstrap lobby`) 및 종료 코드 전파**
   - `endpoint.json`(TLS SPKI sha256), `tier0-live.json`(`--no-clean-background` 시 `"clean_background": {"enabled": false}` 포함), `pod.jsonl` 기록, 토큰 파일 격리(`--tier0-token-file` 은 `tier0` 환경에만, `--worker-token-file` 은 `0600` `worker.env` 를 거쳐 `simai-wait-tier0 --env-file` 로 `worker`·`avatar` 에만 전달)와 `Supervisor`(`tier0,vllm,worker,avatar`)를 한 명령으로 묶었다.
   - `lobby_live_s9.py` 마지막 줄이 `return 0` 으로 고정돼 Tier 0 비정상 종료 코드를 삼키던 버그를 없애고 `Supervisor.run()` 이 `exit_code(rc)` 를 그대로 반환한다.
4. **기동 전 일괄 점검 (`simai-bootstrap preflight` / `check --preflight`)**
   - `tier0,avatar,static,vlm` 다중 가중치 묶음, GPU 예산 합산, `texrecon` 실행 파일 및 `open3d`·`ultralytics` 모듈을 한 번에 점검하고 하나라도 실패하면 비-0 종료 코드(2·3·64)를 반환한다.

## 5단계: VLM FP8 온라인 양자화 기본화 및 GPU 예산 0.30·워커 최소 보장 비율 0.10 (S15P31A101-525)

12차 GPU 세션에서 VLM `0.55` + 아바타 `0.10` 조합으로 분석 워커(`SIMAI_WORKER_TORCH_MEMORY_FRACTION`) 몫이 `0.05`(~4 GB on H100 80 GB)로 줄어 `site_setup` 의 MapAnything(피크 할당 6.4 GB)이 CUDA OOM 으로 실패했다.
2026-11-06 GPU PoC(H200, vLLM 0.21.0 + `vllm-cosmos3`, `--gpu-memory-utilization 0.3`, `--max-model-len 32768`, `--max-num-seqs 4`, `VLLM_USE_FLASHINFER_SAMPLER=0`)로 BF16 과 FP8(`--quantization fp8`, `Fp8OnlineLinearMethod` / `CutlassFP8ScaledMMLinearKernel`)을 비교 측정했다.

| 항목 | BF16 (양자화 없음) | FP8 (`--quantization fp8`) | 비고 |
|---|---|---|---|
| 모델 가중치 적재 메모리 | 16.79 GiB (3.69 s) | **10.05 GiB** (4.38 s, 가중치 적재 3.12 s) | **-6.74 GiB (-40.1%)** 절감 |
| CUDA graph 메모리 | 0.03 GiB (추정 0.04 GiB) | 0.03 GiB (추정 0.04 GiB) | 동일 |
| H200 `0.30` 기준 남는 KV 캐시 | 22.38 GiB (162,976 토큰, 32k 기준 동시 4.97x) | **29.07 GiB** (211,664 토큰, 32k 기준 동시 6.46x) | +6.69 GiB (+48,688 토큰) |
| 실제 프롬프트 1 (2,945 prompt tokens) | 1.73 s (100 completion tokens) | **1.57 s** (89 completion tokens) | 한국어 위험 판정·근거 요지 동등 |
| 실제 프롬프트 2 (2,926 prompt tokens) | 0.20 s (17 completion tokens) | **0.18 s** (17 completion tokens) | 답변 동일(`"네, 라바콘은 화면 오른쪽에 있습니다."`) |

### 결정
- **`simai-bootstrap` vLLM 양자화 스위치 (`SIMAI_VLLM_QUANTIZATION`, 기본값 `fp8`)**:
  - `Supervisor._vllm_argv` 가 기본적으로 `--quantization fp8` 을 전달한다.
  - `SIMAI_VLLM_QUANTIZATION` 이 `none` 또는 빈 값이면 `--quantization` 을 넣지 않고(BF16 복귀 스위치), `SIMAI_VLLM_EXTRA_ARGS` 에 이미 `--quantization` 또는 `-q` 가 있으면 중복 전달하지 않는다.
- **VLM 기본 `SIMAI_WORKER_VLLM_GPU_MEMORY_UTILIZATION = 0.30` 정합화**:
  - H100 80 GiB 의 `0.30` = `24 GiB`: FP8 가중치 `10.05 GiB` + CUDA graph(`0.03~0.04 GiB`)·인코더/활성화 여유 + KV 캐시 약 `13 GiB`(약 `9.4만 토큰`, 32,768 상한 기준 동시 `2.9x`, 실제 요청당 약 `2,950 토큰` × 동시 4 요청 ≈ `1.2만 토큰` 대비 `7~8배` 이상 여유).
  - 위 KV 약 `13 GiB` 는 계산 추정이었다. 같은 절대 크기(H200 에서 비율 `0.1702` = 24,470 MiB, H100 81,559 MiB × 0.30 = 24,468 MiB)로 띄운 2026-11-10 시험에서 vLLM 0.21 이 잡은 KV 는 `10.92 GiB`(79,536 토큰, 32,768 상한 기준 동시 `2.43x`)였고, 가중치·KV 밖 실행 시 메모리가 약 `2.9 GiB` 였다(S15P31A101-580, 명세 3.2 'VLM 메모리 항목'). 동시 4 × 약 2,950 토큰 ≈ 1.2만 토큰 대비 여유는 약 `6.7배` 다. H100 실측은 아니다.
  - `deploy/scripts/simai_bootstrap.py`(`compute_gpu_fractions`, `_vllm_argv`, `preflight`), `ai/src/simai_ai/gpu/budget.py`(`GpuBudget`, `WorkerGpuSettings`), `deploy/compose.yaml` 모두 `0.30` 으로 통일했다.
- **분석 워커 몫 확대 및 최소 보장 비율(`MIN_WORKER_TORCH_FRACTION = 0.10`) 검증**:
  - Tier 0 `0.20`·아바타 `0.10` 은 그대로 두고, 분석 예산 `0.70` 에서 vLLM `0.30`·아바타 `0.10` 을 뺀 분석 워커 기본 몫은 `0.30`(H100 80 GiB 기준 24 GiB, 아바타 미사용 시 `0.40` = 32 GiB)이 된다.
  - `compute_gpu_fractions` 는 `"worker" in services` 일 때 계산되거나 명시된 `worker_fraction` 이 `MIN_WORKER_TORCH_FRACTION`(`0.10`, H100 80 GB 기준 ~8 GB, MapAnything 피크 할당 6.4 GB 대비 약 1.6 GB 여유) 미만이면 기동 전에 종료 코드 64(`EXIT_USAGE`)로 거부하여 12차 세션과 같은 `0.05`(~4 GB) OOM 재발을 막는다.
