# S15P31A101-720 통합 이미지의 Blackwell sm_120(AWS g7e) 대응

관련 명세: 3.12(최소 사양·이미지와 CUDA·GPU별 엔진·"Blackwell 은 실제로 돌려 본 적 없음"), 3.2(Tier 0 우선·GPU 예산), 19.1(Blackwell 실행 미결).
앞 티켓: -703(AWS 서울 공급자, 추천 `g7e.2xlarge`). 대상: `g7e.2xlarge` = RTX PRO 6000 Blackwell Server Edition 96 GB × 1(compute capability 12.0),
vCPU 8, 호스트 메모리 64 GiB(AWS G7e 제품 페이지). **실제 GPU 는 없다.** 확인은 로컬 이미지 `simai/unified:4ef4eedc220e` 정적 검사(cuobjdump 13.1·nvdisasm,
vLLM 0.21 소스 읽기)와 단위 시험뿐이고 실 GPU 확인은 남은 위험이다. 이미지 판은 올리지 않았다(아래 "다음 단계").

## 바꾼 것

| 위치 | 내용 |
|---|---|
| `deploy/scripts/simai_bootstrap.py` | 이미 있는 GPU 감지(`detect_gpu` → `SIMAI_GPU_ARCH`)의 주 번호가 12 면 vLLM 에 `--attention-backend TRITON_ATTN --mm-encoder-attn-backend TORCH_SDPA`, 환경 `VLLM_DISABLED_KERNELS=FlashInferFP8ScaledMMLinearKernel` 을 더한다. `SIMAI_VLLM_EXTRA_ARGS` 에 `--attention-backend`·`--attention-config`(`-ac`, 함께 주면 vLLM 이 거부)·`--mm-encoder-attn-backend` 가 있거나 환경에 `VLLM_DISABLED_KERNELS` 가 있으면 그 값이 우선. 시작 로그 한 줄. 9.0(H100)·10.x·모름은 그대로 |
| `deploy/scripts/check_image.sh` 5c | 이미지 안 cuobjdump(PATH → tokenspeed_triton 13.1 → triton 12.8)가 있을 때만: venv·`/opt/simai`(구운 가중치 제외)·`/usr/local/lib`·`/usr/lib/x86_64-linux-gnu` 의 **모든 .so(크기 무관, ELF 에 nv_fatbin 절)** 를 파일마다 따로 본다. sm_12x SASS → 통과, 없고 일반 PTX(≤ 120, a·f 아님) → 참고(드라이버 PTX JIT), 둘 다 없음 → 실패. 예외 목록(이유 포함)과 flashinfer-cubin 세대·nvcc 유무는 참고로만. 약 45~55초 |
| `ai/tests/deploy/test_simai_supervisor.py` | sm_120·121 경로, 9.0·10.0·모름은 그대로, 사용자 값 우선·중복 없음, `gpu_major` |
| `deploy/README.md` | Blackwell 문단·통합 이미지 vLLM 문단 |

서버(`GpuTiers`·`GpuPowerService`)·AWS 공급자는 고치지 않았다: 96 GB 는 RECOMMENDED 등급(74 GB 이상)이고, "추천" 표시는 H100·H200 이름일 때만이라
g7e 는 추천 별표 없이 RECOMMENDED 로 보인다(켜기를 막지 않음, 엔진 캐시를 위한 의도된 정책, -705). 캐시 키(`gpu_slug` = `nvidia-rtx-pro-6000-blackwell-server-edition`, 48자 안)·
엔진 경로(`engine_cache` `sm_120` 정규식 `sm_[0-9]{2,3}`)·GPU 메모리 몫(비율이라 96 GB 에서 각 몫이 커질 뿐, "80GB 미만 경고" 76,000 MiB 넘음)도 깨지는 곳이 없다.

## 점검 표 (이미지 `4ef4eedc220e`)

| 구성 요소 | sm_120 | 조치 |
|---|---|---|
| torch 2.11 `libtorch_cuda`·`_linalg`·`_nvshmem`, torchvision, torchaudio, nvjpeg | SASS 있음 | 없음 |
| TensorRT 11.4 `libnvinfer`·`_plugin` | SASS 있음 | 엔진은 첫 기동 때 `sm_120/trt…` 경로로 새로 빌드 |
| cuDNN(ops·cnn·adv·engines_precompiled·runtime_compiled), cuBLAS·cuBLASLt, cuSPARSE·cuSPARSELt, cuRAND, cuSOLVER, NCCL | SASS 있음 | 없음 |
| cuFFT·cuFFTW | cuobjdump 표준 목록엔 sm_75 하나. 실제로는 fatbin 블롭 23,984개(LTO 묶음)를 싣고 이미지의 `libnvJitLink.so.13` 으로 그 GPU 용을 만든다 | 예외 목록. **LaMa(FFC, torch.fft)** 가 쓰므로 첫 실행 확인 |
| vLLM `_C`·`_moe_C`·`_C_stable_libtorch` | SASS 있음 | 없음 |
| vLLM CUTLASS FP8 `cutlass_scaled_mm_sm120` | `_C_stable_libtorch` 의 sm_120 cubin 16개 중 GEMM 커널(`enable_sm120_only`·`MainloopSm120TmaWarpSpecialized`)에 **실제 FP8 MMA(`QMMA.16832.F32.E4M3.E4M3` 1,024개)**, `TRAP` 0. 같은 묶음의 NVFP4 커널엔 `OMMA.SF…E2M1`(sm_120 전용 기능 명령)도 있다 → 아키텍처 전용 기능으로 컴파일된 진짜 커널(cuobjdump 가 `sm_120a` 로 표시하지 않을 뿐) | 기본 경로 유지(아래 FP8) |
| vLLM FA2 `_vllm_fa2_C` | SASS sm_80 + PTX sm_80(드라이버 JIT 로는 돈다) | cc 12.x 에서 Triton attention 으로 피함 |
| vLLM FA3 `_vllm_fa3_C`, FlashMLA `_flashmla_C`·`_extension_C` | sm_90a(·sm_100) 뿐 | 예외: FA3 는 cc 9.x 에서만 고름(`fa_utils.get_flash_attn_version`), FlashMLA 는 MLA 모델 전용(Cosmos3 = Qwen3-VL GQA) |
| flashinfer 0.6.8.post1 + flashinfer-cubin | cubin 1,479개 모두 `sm_100f`, nvcc 없음 → sm_120 커널 없음·JIT 불가 | cc 12.x 에서 FlashInfer 호출처를 모두 끔(아래) |
| Triton 3.6 | arch ≥ 100 은 `ptxas-blackwell`(CUDA 12.9) 사용, PTX ISA 판을 그 ptxas 판에서 정함 → 어긋남 없음. 대상 `sm_120a` | 없음. gcc(`/usr/bin/gcc`)·`Python.h`(`/opt/uv/python/cpython-3.11.15…/include`) 있음(런처 빌드) |
| open3d 0.19 CUDA 모듈 | SASS sm_75~89 + **PTX sm_90**(앞선 "PTX 없음"은 12.8 판 cuobjdump 를 쓴 탓) | 우리 코드는 CPU 경로만: `texture_mesh` 의 legacy `ScalableTSDFVolume`(CPU)·`o3d.t.geometry.RaycastingScene`(Embree CPU)·기본 장치 CPU 의 `o3d.core.Tensor`. `import open3d` 가 CUDA 모듈을 싣지만 커널은 돌리지 않는다. GPU 로 쓰더라도 PTX JIT 가 있다 |
| cuFile | SASS sm_75~86 + PTX sm_86 | GPUDirect Storage, 쓰지 않음 |
| texrecon 등 /usr/local/bin 바이너리 | CUDA 커널 없음 | 없음 |

## vLLM 0.21 에서 고른 경로와 근거(이미지 안 소스)

- 디코더 attention: `platforms/cuda.py` `_get_backend_priorities` 는 MLA 아니고 주 번호 10 이 아니면 `[FLASH_ATTN, FLASHINFER, TRITON_ATTN, …]`.
  `FlashAttentionBackend.supports_compute_capability` 는 `≥ 8.0` 이라 12.0 에서 FLASH_ATTN 이 뽑히고, `fa_utils.get_flash_attn_version` 은 9→FA3, 10→FA4,
  나머지 FA2 → sm_80 PTX JIT 에 기대게 된다. → `--attention-backend TRITON_ATTN`(`triton_attn.py` `supports_compute_capability` 항상 참, head ≥ 32, bf16, `supports_mm_prefix` 참).
  attention 백엔드 환경 변수(`VLLM_ATTENTION_BACKEND`)는 0.21 `envs.py` 에 없다 — CLI(`engine/arg_utils.py` `--attention-backend`, `AttentionConfig.validate_backend_before`: 이름 대문자, `auto` = 자동)만 된다.
- 영상 인코더(ViT): `get_vit_attn_backend`(`model_executor/models/vision.py` → `platforms/cuda.py`)는 `[FLASH_ATTN, TRITON_ATTN, TORCH_SDPA, FLASHINFER]` 순서로 첫 지원을 고른다 →
  12.0 에서 FA2. `--mm-encoder-attn-backend TORCH_SDPA`(`config/multimodal.py`, `auto` 는 받지 않는다): libtorch 의 sm_120 SASS 만 쓰고 JIT 가 없다. Qwen3-VL 의 `cu_seqlens` 는 `torch_sdpa_wrapper` 가 이미지별로 나눠 돈다.
- 샘플러: `v1/sample/ops/topk_topp_sampler.py` 는 `FlashInferBackend.supports_compute_capability`(7.5 ≤ cc ≤ 12.1)라 12.0 에서 FlashInfer 를 고른다 → 기존 기본 `VLLM_USE_FLASHINFER_SAMPLER=0`(-278, 모든 GPU) 이 막는다.
- FP8(`--quantization fp8`, 온라인): `fp8.py` 는 `cutlass_fp8_supported()`(= `_C.cutlass_scaled_mm_supports_fp8(120)`, sm120 커널이 컴파일돼 있음)면 활성화 per-token(`kFp8DynamicTokenSym`).
  후보 순서(`kernels/linear/__init__.py` `_POSSIBLE_FP8_KERNELS`): Marlin(cc ≥ 89 면 거부) → FlashInfer(cc ≥ 100 이면 후보, per-tensor 만 구현 가능) → **Cutlass**(항상 지원) → PerTensorTorch → ChannelWiseTorch.
  per-token 이라 FlashInfer 는 원래 안 뽑히지만, 만약 `cutlass_fp8_supported()` 가 거짓이 되면 per-tensor 로 바뀌어 FlashInfer(`bmm_fp8` → JIT)가 뽑히므로 `VLLM_DISABLED_KERNELS`(`is_supported_and_can_implement_kernel` 이 본다)로 막았다.
  예비: Cutlass sm120 이 실 GPU 에서 실패하면 `VLLM_DISABLED_KERNELS=FlashInferFP8ScaledMMLinearKernel,CutlassFP8ScaledMMLinearKernel` → `ChannelWiseTorchFP8ScaledMMLinearKernel`(per-token 활성화 + per-tensor 가중치, `torch._scaled_mm` = cuBLASLt sm_120). BF16(`SIMAI_VLLM_QUANTIZATION=none`)은 가중치 10 → 약 17 GiB 라 0.30 몫(96 GB 의 약 28.8 GiB)에서 KV 가 크게 줄어 마지막 대안.
- FlashInfer 나머지 호출처(`grep -rl "import flashinfer\|from flashinfer" vllm`, 30 파일) 전수: attention 백엔드 flashinfer·MLA 계열(백엔드를 지정해 검증·import 안 함), `vit_attn_wrappers`(FLASHINFER ViT 일 때만), 샘플러(위),
  RoPE(`rotary_embedding/base.py` 에서 `use_flashinfer=False` 고정), 컴파일 패스 allreduce·collective(TP > 1 만, `config/vllm.py enable_allreduce_rms_fusion`), qk_norm_rope(`enable_qk_norm_rope_fusion` 기본 거짓),
  FlashInfer autotune(`kernel_warmup`: O2 기본 `enable_flashinfer_autotune=False`, `config/vllm.py OPTIMIZATION_LEVEL_02`), MoE·NVFP4·MXFP8·mamba·deepseek(이 모델에 없음), 분산 통신(TP=1).
  IR op 기본 우선순위(`get_default_ir_op_priority`)에도 flashinfer 가 없다. → 지정한 구성에서는 FlashInfer 커널을 부르지 않는다.
  H100 에 `~/.cache/flashinfer/0.6.8.post1/90a/flashinfer_jit.log` 가 생긴 것은 JIT 가 아니라 `flashinfer/jit/core.py` 가 **import 때** 그 로거 파일을 만들기 때문이다(자동 백엔드 선택이 `FlashInferBackend` 클래스를 import).
  `has_flashinfer()` 는 flashinfer-cubin 이 깔려 있어 nvcc 없이도 참이고 환경 변수로 끌 수 없다. 남는 호출이 실 GPU 에서 보이면(가장 작은 방법) 그 호출처의 백엔드·커널을 위와 같은 CLI·`VLLM_DISABLED_KERNELS` 로 하나씩 끈다. 패키지를 빼는 것은 이미지 변경이라 이번 범위 밖.

## 남은 위험

1. **실 GPU 미검증.** 위 판단은 모두 정적이다. 특히 Triton attention·SDPA ViT 의 정확도·속도, CUTLASS sm120 FP8 GEMM 실행, cuFFT nvJitLink(LaMa)는 g7e 에서 처음 돈다.
2. **드라이버 R580 이상 필요.** 이미지 CUDA 13.0.3. `/usr/local/cuda/compat`(libcuda 580.126.20)는 `LD_LIBRARY_PATH`(`/usr/local/nvidia/lib:…/lib64:/usr/local/cuda/lib64`)·`/etc/ld.so.conf.d` 어디에도 없어 이미지가 스스로 싣지 않는다.
   다만 호스트 드라이버가 570·575 이면 `NVIDIA_REQUIRE_CUDA` 의 brand 조항으로 컨테이너는 뜨고, Container Toolkit 의 CUDA 호환 훅이 compat 를 붙일 수 있다. 이 GPU 에서 순방향 호환이 되는지 확인하지 못했으므로 R580+ AMI 만 쓴다.
3. **첫 엔진 빌드 시간.** sm_120 TensorRT 엔진은 S3 GPU 캐시에 없으니 첫 기동에 새로 만든다(H100 은 수 분). vLLM 은 그 뒤(Tier 0 표시 파일 running·reserve OK)에만 뜨므로 빌드와 겹치지 않는다. 만든 엔진은 -700 캐시가 `nvidia-rtx-pro-6000-blackwell-server-edition/sm_120/…` 키로 올린다.
   vLLM 첫 기동도 Triton attention·inductor 커널 컴파일이 더해진다(`SIMAI_VLLM_READY_TIMEOUT_S` 1800 안).
4. **성능 차.** FA3(H100) → Triton attention, FA2 ViT → SDPA. 동시 4·약 3k 토큰/요청 규모라 감당할 것으로 보지만 재지 않았다. GDDR7 대역(약 1.8 TB/s, H100 3.35 TB/s)이라 디코드도 느리다.
5. **vCPU 8.** `SIMAI_SIDECAR_CPUS=auto` 는 n = 8 이라 Tier 0 0-3, vLLM·워커·아바타 4-7 로 나눈다(H100 Pod 는 Tier 0 12개). Tier 0 BLAS 1·사이드카 OMP 1·nice 10, 굽기(Open3D TSDF·texrecon)는 `default_bake_threads()` = 묶인 4개의 절반 = 2 스레드, OpenCV 1.
   이미 과하지 않아 코드는 바꾸지 않았다. Tier 0 4코어에 4대 CPU 디코딩 + 검출 전후처리 + 기하가 들어가는지는 재야 한다(13차 혼잡 GIL 붕괴 전례).
6. **호스트 메모리 64 GiB < VRAM 96 GB.** 겹칠 수 있는 것: Tier 0 엔진 빌드(빌더 호스트 메모리) → 끝난 뒤 vLLM 이 BF16 체크포인트 약 17 GiB 를 읽어 GPU 에서 FP8 로 바꿈(페이지 캐시 + 프로세스), 워커·아바타 가중치(MapAnything·WorldMirror 2.0·LaMa·YOLO seg·HMR)를 CPU 로 읽고 올림,
   굽기(texrecon·TSDF). 순서상 엔진 빌드와 vLLM 은 겹치지 않지만 vLLM·워커·아바타 적재는 거의 동시다. `docker run` 에 메모리 제한이 없어 OOM killer 가 Tier 0 를 고를 수도 있다. 피크를 재지 않았다.
7. CUDA 캐시: Triton·inductor·CUDA JIT 캐시는 프로세스별 `/tmp/simai-cache`(컨테이너 디스크)라 인스턴스를 새로 켜면 다시 컴파일한다.

## 다음 AWS 첫 실행 체크리스트 (순서대로, 실패 시 대안)

1. 호스트: `nvidia-smi --query-gpu=name,driver_version,compute_cap,memory.total --format=csv` → 이름 `NVIDIA RTX PRO 6000 Blackwell Server Edition`, cc `12.0`, 드라이버 **580 이상**, `free -g`(64 GiB), `nproc`(8).
   대안: 580 미만이면 AMI 를 바꾼다(드라이버만 올리는 손 작업은 하지 않음).
2. 부트스트랩 로그: `GPU … sm_120 …`, 감시 시작 줄의 `vLLM 을 sm_12x 경로로`, CPU 묶기 `Tier 0 0-3 / 사이드카 4-7`.
3. Tier 0 단독: 엔진 첫 빌드 시간·`sm_120/trt…` 경로, 4대 LIVE p95(단계별), `top`·`/proc/pressure/cpu`·`free`(빌드 중 호스트 메모리 피크).
4. vLLM: `logs/vllm.log` 에 `Using AttentionBackendEnum.TRITON_ATTN backend.`, `Using backend AttentionBackendEnum.TORCH_SDPA for vit attention`(또는 `for MMEncoderAttention`), `Selected CutlassFP8ScaledMMLinearKernel`, `using PyTorch-native sampler`,
   FlashInfer JIT 빌드 줄 없음(`~/.cache/flashinfer/*/flashinfer_jit.log` 에 컴파일 기록 없음). 이미지 포함 VLM 요청 1건 완주·한국어 판정 품질, TTFT.
   대안: CUTLASS FP8 오류 → `VLLM_DISABLED_KERNELS=FlashInferFP8ScaledMMLinearKernel,CutlassFP8ScaledMMLinearKernel`(ChannelWiseTorch) → 그래도 안 되면 `SIMAI_VLLM_QUANTIZATION=none`(BF16, 몫 재계산 필요).
   Triton attention 오류 → `SIMAI_VLLM_EXTRA_ARGS="--attention-backend FLASH_ATTN"`(FA2 PTX JIT, 첫 요청이 느림). 기동 시한 초과 → `--enforce-eager` 를 더해 원인 분리.
5. 워커 1회씩: MapAnything·WorldMirror·LaMa(cuFFT nvJitLink)·사람 분할·굽기(texrecon), 아바타 HMR. `no kernel image is available` 이 나오면 그 .so 를 `check_image.sh` 5c 표에서 찾는다.
6. 전체 동시 부하: Tier 0 + vLLM + 워커 + 아바타(+굽기) 에서 Tier 0 p95, 호스트 메모리 피크·스왑, `nvidia-smi dmon`(메모리 대역·전력·클럭). Tier 0 가 밀리면 `SIMAI_BAKE_NUM_THREADS=1`·`SIMAI_TIER0_CPUS`/`SIMAI_SIDECAR_CPUS` 를 손으로 나눠 보고, 그래도 안 되면 `g7e.4xlarge`(vCPU 16).
7. 결과를 이 문서와 명세 3.12·19.1(Blackwell 실행 미결) 정리 MR 에 넘긴다.

## 다음 단계 (이번에 하지 않음)

- **이미지 판 올림.** vLLM·flashinfer 상위 판의 sm_120 정식 지원은 근거가 갈린다: vLLM 0.10.1 릴리스 노트에 SM120(RTX 5090·RTX PRO 6000) Block FP8·CUTLASS NVFP4 지원이 있고(이번 이미지에도 그 커널이 있다),
  FlashInfer 쪽은 RTX PRO 6000 이 `12.0a` 가 아니라 `12.0` 으로 보이고 일부 백엔드가 SM100 전용 검사에 막힌다는 이슈(flashinfer-ai/flashinfer#2723)가 있다. 상위 vllm-flash-attn 이 sm_120 SASS 를 싣는지,
  flashinfer-cubin 이 sm_120 cubin 을 내는지는 이미지 안 메타데이터(이번 판은 `sm_100f` 뿐)로만 확인했고 상위 판은 확인하지 못했다. g7e 실측(체크리스트 4) 뒤 필요하면 별도 티켓으로 판을 올리고 이 문서의 5c 점검으로 다시 본다.
- 명세 3.12 의 vLLM 기본값 문단에 cc 12.x 분기를 더하는 것은 명세 정리 MR 로 넘긴다.

## 버린 대안

- FA2 를 그대로(드라이버 PTX JIT): 첫 요청 지연·JIT 캐시(기본 256 MB, 프로세스별 `/tmp`)·sm_80 명령 한계. Triton 이 동봉 ptxas 로 sm_120a 를 직접 만든다.
- ViT 를 Triton attention 으로: 가능하지만 SDPA 는 컴파일이 없어 첫 실행 위험이 가장 작다. ViT 는 요청당 한 번이라 속도 차가 작다.
- 런타임 이미지에 nvcc 를 넣어 FlashInfer JIT: 이미지가 커지고 8 vCPU 첫 기동에서 JIT 가 엔진 빌드·적재와 경쟁한다.
- GPU 이름 매칭: 같은 세대 다른 카드(RTX 5090 등)를 놓친다. compute capability 주 번호로 정한다.
