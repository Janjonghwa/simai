# 한 가상환경(Tier 0 + vLLM + HMR, torch 2.11.0) 잠금과 사전 판정

S15P31A101-235 1단계. 기획서 §3.2(Tier 0 와 분석 워커는 GPU 하나에서 **별도 프로세스**, 분석 워커가 죽어도 Tier 0 는 계속), §3.2 GPU 메모리 예산표,
§3.8(VLM Cosmos3-Nano Reasoner, vLLM 0.19.1 이상 `vllm-cosmos3`), §3.12(GPU 실행 환경, 드라이버 CUDA 13.0 이상), §20.5(GPU Job Queue).
코드는 `ai/pyproject.toml`(extras `tier0`·`hmr`·`vlm`·`worker`, `[tool.uv]`, import-linter `tier0-no-worker-stack`), `ai/uv.lock`, `deploy/weights.json`(묶음 `vlm`).
시험은 `ai/tests/architecture/test_unified_env.py`, `ai/tests/architecture/test_import_rules.py`(Tier 0 금지 규칙). 사전 판정 스크립트는 저장소에 넣지 않았다(아래 "사전 판정", 2단계에서 이미지 점검으로 옮긴다).

이번 단계는 잠금·사전 판정·3차 세션 큐 초안까지다. 통합 Dockerfile·`simai-bootstrap` 감시 프로세스는 2단계(아래 "2단계로 넘긴 일")다. 코드 구조(프로세스 분리)는 바꾸지 않았다.

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
`vae/`(1.41GB)·`sound_tokenizer/`(1.99GB)는 index 에 없어 vLLM safetensors 로더의 index 필터에서 빠진다 → 목록에 넣지 않았다(3.4GB 절약). 넣지 않아도 뜨는지는 3차 세션 v1 이 확인한다.

목록에 넣은 18개(모두 `sets: ["vlm"]`, `weights/cosmos3-nano/<HF 경로>`): 샤드 7개 + vision_encoder(해시 = HF API LFS oid), 작은 파일 10개(config·index·generation·preprocessor·video_preprocessor·chat_template·tokenizer·tokenizer_config·vocab·merges,
고정 커밋에서 받아 sha256). `checkpoint.json`(`{}`)·`model_index.json`(diffusers 파이프라인용)·`text_tokenizer/`·`transformer/config.json` 등은 Reasoner 서빙에 쓰지 않아 뺐다. 빠진 파일 때문에 서버가 안 뜨면 v1 결과로 새 항목을 더한다.
지금 이 묶음을 쓰는 이미지는 없다(2단계 통합 이미지가 `SIMAI_WEIGHTS_SET` 에 더한다). 출처 기록은 `docs/data/weights.md` "Cosmos3-Nano".

## 3차 GPU 세션 큐 초안

통합 이미지(2단계)가 아직 없으므로 3차 세션은 **Tier 0 이미지 Pod 의 가상환경을 이 잠금으로 바꿔**(사전 판정과 같은 명령) 돈다. 실행 코드는 아직 만들지 않았다
(세션 큐 `deploy/scripts/gpu_session1.py` 에 넣는 일은 2단계·Lead). 한 Pod(H100 80GB 권장, 드라이버 CUDA 13.0 이상, 공개 TCP 포트)에서 아래 순서로 돈다. LIVE 관제 시간이 아닐 때만(AGENTS.md).

| 키 | 확인 | 명령·방법 | 기대 / 기록 |
|---|---|---|---|
| u0 | 통합 환경 준비 | Tier 0 이미지 Pod 에서 `cd /opt/simai/ai && uv sync --locked --no-dev --extra tier0 --extra worker --no-install-project --reinstall-package opencv-python-headless`(발견 1) → 잠금 밖 휠 3개(빌드 도구 `build-essential`·`git`, `FORCE_CUDA=0`, `--no-deps`) → 프로젝트 설치 | 받기 크기·시간 기록. 사전 판정의 import 점검(Tier 0·vLLM·플러그인·HMR) 모두 OK |
| u1 | CUDA 실제 적재 | `python -c "import torch,tensorrt; print(torch.cuda.is_available(), torch.backends.cudnn.version())"`, `vllm collect-env` | True, cudnn 91900. 노트북 CPU 컨테이너에서 못 본 `libcuda` 경로 |
| b | 엔진 빌드(torch 2.11) | 기존 b 항목(`session1_engine.sh`, 실제 YOLO26x-pose 가중치, 1280·batch 4·FP16) | 통과. 빌드 시간(1차 세션 약 550초 대비)·엔진 입출력 shape 기록. 엔진 캐시 키는 ultralytics·TensorRT 판으로 정해져 torch 판이 바뀌어도 키가 같다 → **기존 캐시를 지우고** 빌드한다 |
| c | LIVE 입력(c0 vCPU 기록 포함, -236) | 기존 c 항목(`session1_live.py all`: c0·c3·c4·c5a) | 기존 기준. c0 `usable_vcpu`·`cpu_model`, c5a 10명 pipeline p95 ≤ 50 ms(-237) |
| d | GPU 우선 보호(-60) | 기존 d 항목(`session1_gpu_priority.py`) | 기존 P0~P7 기준 |
| v1 | VLM 단독 적재·VRAM | `simai-bootstrap` 으로 `vlm` 묶음(18개, 31.5GB)을 받은 뒤 `VLLM_USE_DEEP_GEMM=0 vllm serve $R/weights/cosmos3-nano --hf-overrides '{"architectures":["Cosmos3ReasonerForConditionalGeneration"]}' --gpu-memory-utilization 0.55 --max-num-seqs 4 --port 8000`(README 의 `--mm-encoder-tp-mode data --async-scheduling` 포함) | "Application startup complete". 기록: 적재 시간, vLLM 로그의 가중치 메모리(GiB)·KV 캐시 크기, `nvidia-smi` 최대 사용량. vae·sound_tokenizer 없이 뜨는지(안 뜨면 빠진 파일을 weights.json 에 더함) |
| v2 | G7 지연(VLM 빠른 경로 모양) | 사람 없는 시험 그림 1장 + 무늬 영상 3~5초(`live_video` 와 같은 생성기) + 짧은 구조화 상태 텍스트로 OpenAI 호환 `/v1/chat/completions` 20회, 동시 1·4 | 요청별 첫 토큰·전체 지연 p50·p95 기록. G7 목표 p95 ≤ 5초(§15)와 비교만(판정은 실제 이벤트 입력으로 따로) |
| v3 | Tier 0 + VLM 동시(G0·CPU 경합) | c4 와 같은 LIVE 하네스(디코더 cpu, 4대 2304x1296 25 fps)를 띄우고 v1 서버에 v2 요청을 동시 4로 10분 반복. `tier0_priority_probe.py watch` 로 표시 파일 판정, `pidstat`/`top` 으로 프로세스별 CPU | Tier 0 `capture_to_risk`(하네스 `pipeline_lag_ms`·`frame_age_ms_p95`) 가 VLM 없는 c4 대비 악화 기록, `gpu_priority` 판정 통과, Tier 0 메모리 부족 0건. CPU 합이 c0 `usable_vcpu` 에 닿는지 |
| g0·g4 | HMR(통합 환경, -238) | 같은 Pod 에 Fast-SAM-3D-Body @d72aa36·DINOv3 허브 코드 @6876159 사본을 받아(`SIMAI_FS3DB_REPO`, 지금은 exp02 이미지에만 있음) `exp_image_check.py` 의 exp02 점검(Detectron2 LazyConfig·pyrender EGL)과 g4(교실 사진 20장 + 보정 K, `docs/design/exp-images.md` "3차 세션 항목") | g0 통과, g4 기대 그대로. torch 2.11 이라 g1~g3 지연은 기대값 표에 없는 조합(기록만) |
| w1·w2 | MapAnything 알려진 K(-102) | `mapanything` 이미지 Pod 따로(통합 대상 아님, 설치 때 단독 실행) | `docs/design/world-frame-from-mapanything.md` "3차 GPU 세션 큐 항목" 그대로 |

- 순서 근거: Tier 0 가 먼저(b·c·d 가 통과해야 통합 환경이 Tier 0 를 해치지 않음을 안다), 그다음 VLM 단독(v1·v2), 마지막에 동시(v3). HMR·MapAnything 은 관제와 무관한 확인이라 뒤에 둔다.
- 결과로 §3.2 예산표(VLM 0.55, 분석 워커 0.70)를 고칠 근거(v1 VRAM)와 2단계 감시 프로세스의 기동 순서·스레드 제한 값(v3 CPU 경합)을 얻는다.
- 비용: u0 받기(torch·CUDA·vLLM 휠, 노트북 컨테이너에서 uv 캐시 7.7GB, 약 15분) + `vlm` 가중치 31.5GB 받기가 가장 길다. 볼륨이 없으므로 결과 묶음만 가져오고 Pod 는 바로 Stop 한다.

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
- vLLM·플러그인은 import·모델 클래스 등록·설정 읽기까지만 봤다. 가중치 적재·추론·VRAM 은 3차 세션 v1~v3.
- constraint 로 묶은 transformers 5.9 등은 플러그인 자체 잠금(vLLM 0.22)과 같은 메이저·마이너이고 vLLM 0.21 과의 조합 그대로를 공식이 시험한 것은 아니다.
- opencv 5.0(Tier 0 가 검증한 판)은 vLLM CI(4.13)보다 새 메이저다. vLLM 이 cv2 를 쓰는 곳은 영상 프레임 읽기다(v2 의 영상 입력 요청으로 본다).
- **기존 결함(이 티켓이 만든 것 아님, 사전 판정 중 발견, -237 코드, 별도 버그 티켓에서 고침).** Linux 휠 numpy 의 OpenBLAS 에서 `np.linalg.svd` 가 inf·NaN 이 든 행렬에서 돌아오지 않는다.
  - 재현 조건: numpy 2.4.6(OpenBLAS 0.3.31.188.0, dev Tier 0 이미지 torch 2.14.1)과 numpy 2.3.5(OpenBLAS 0.3.30, 이 잠금) 둘 다. 최소 재현 `A = np.ones((3, 4, 4)); A[1, 0, 0] = np.inf; np.linalg.svd(A)` 가 60초 넘게 끝나지 않음(NaN 만 넣은 경우는 따로 재지 않았다. 멈춘 시험 행에는 inf·NaN 이 함께 있다). Windows(노트북)에서는 시험이 통과한다.
  - 멈춘 시험: `tests/geometry/test_geometry_speedup_regression.py::test_degenerate_rays_match_previous` 하나(전체 실행에서 다른 멈춤 없음). 입력은 `_baseline_rows(cam1, cam2)`(rig `wide`, 25행 × 2시점 × 2)의 특수 행 — `[0,0,0]=inf`, `[1,1,1]=-inf`, `[2,0,1]=NaN`, `[3,1]=[3,0]`(같은 정규화 좌표).
  - py-spy 위치: 시험 안의 이전 구현 `legacy_batch_triangulation.py:647 _dlt_batch` 와 **제품 코드** `svd (numpy/linalg/_linalg.py:1862)` ← `_dlt_batch (simai_ai/geometry/triangulation.py:659)` ← `_solve_batch (:636)` ← `triangulate_normalized_many (:429)`(같은 행을 제품 함수에 직접 넣어 120초 넘게 멈춤).
  - 영향: Tier 0 판정 루프가 non-finite 정규화 좌표를 이 함수에 넘기면 멈출 수 있다. 이 MR 에서는 고치지 않았다(제품 코드 범위 밖).

## 2단계로 넘긴 일

- 통합 Dockerfile(이 잠금 하나로 Tier 0 + 워커 + vLLM, 잠금 밖 휠 설치 순서, `deploy/tier0/Dockerfile` 묶음 목록에서 `opencv-python` 빼기, EGL·X11 라이브러리, `NVIDIA_DRIVER_CAPABILITIES` 합집합 compute·utility·video·graphics, 이미지 전역 `VIRTUAL_ENV` 정리).
- `simai-bootstrap` 감시 프로세스(프로세스 그룹 분리, Tier 0 먼저·워커 가중치(`vlm` 35GB 중 31.5GB)는 뒤 단계, 종료 순서 워커 → vLLM → Tier 0, 지연 초과 지속 시 워커 그룹 SIGKILL, 스레드 제한·nice, 프로세스별 TRITON·Inductor 캐시).
- `exp02` 이미지를 통합 잠금으로 옮길지(E-GATE-0 대조 기준 torch 판 줄 `compare_egate0.EXPECTED` 에 2.11.0 추가 필요), `deploy/exp02/requirements.in` 정리.
- Tier 0 의 CUDA 12 계열 미사용 짐(`onnxruntime-gpu`·`cupy-cuda12x`, 약 0.65GB, modelopt[onnx] 의존성) 정리: override 가 필요하고 엔진 빌드를 GPU 에서 다시 봐야 한다.
- 명세 §3.2 예산표의 VLM 0.55(48GB 에서 26.4GB)는 v1 의 Reasoner VRAM 실측 뒤 §3.2·§3.12 정리(-172, 명세 결함 후보).
