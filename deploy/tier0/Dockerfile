# Tier 0 서비스 이미지 (명세 3.1 RunPod Tier 0, 3.12, 21.5). 빌드: deploy/README.md (docker buildx bake tier0).
# - Python 의존성은 ai/uv.lock 그대로(uv sync --locked). pyproject 와 잠금이 어긋나면 빌드가 실패한다.
#   큰 휠은 잠금에서 뽑은 묶음별 층으로 먼저 깐다(S15P31A101-207, docs/design/exp-images.md "Tier 0 층 분할").
# - 가중치(YOLO26x-pose .pt)와 TensorRT 엔진은 이미지에 없다. 둘 다 저장 위치($SIMAI_ROOT)에 둔다.
#   시작하면 simai-bootstrap 이 저장 위치·GPU 를 고르고 가중치를 목록(deploy/weights.json)대로 받아 확인한 뒤(S15P31A101-205),
#   simai-tier0 가 그 GPU 에서 엔진을 만들고 캐시한다(entrypoint → simai_ai.detection.engine_cache).
# - 빌드 컨텍스트는 저장소 최상위이고, 들어오는 파일은 deploy/tier0/Dockerfile.dockerignore 의 허용 목록뿐이다.
ARG BASE_IMAGE=simai-base

# 0) 잠금(ai/uv.lock)에서 층별 설치 목록(해시 포함)을 뽑는다. 잠금이 바뀌면 이 단계는 다시 돌지만, 목록 내용이 같으면
#    아래 COPY --from 의 캐시 키(내용 체크섬)가 같아 큰 층을 다시 만들지 않는다(같은 층 = 같은 다이제스트 = 다시 올리지 않음).
#    묶음(바뀌는 빈도가 낮은 것부터): cuda = torch 가 가져오는 CUDA 13 휠, torch = torch·triton·torchvision,
#    trt = TensorRT libs(pypi.nvidia.com), wheels = 드물게 바뀌는 큰 일반 휠. 나머지 작은 패키지는 4) 층, simai_ai 코드는 5) 층.
#    묶음의 이름이 잠금에 없으면 빌드를 멈춘다(나뉜 줄 알았는데 4) 층 하나에 다 들어가는 일을 막는다).
FROM ${BASE_IMAGE} AS layer-lists
WORKDIR /src
COPY ai/pyproject.toml ai/uv.lock ./
RUN set -eu; \
    uv export -q --locked --no-dev --extra tier0 --no-emit-project --format requirements-txt -o all.txt; \
    pick(){ out=$1; shift; : > "$out"; \
      for n in "$@"; do \
        awk -v n="$n" '/^[^ #]/ { x = $1; sub(/[ =;].*/, "", x); keep = (x == n) } keep && !/^ *#/' all.txt > one.txt; \
        grep -q "^$n==" one.txt || { echo "층 묶음의 $n 이 잠금에 없다(deploy/tier0/Dockerfile 묶음을 고친다)" >&2; exit 1; }; \
        cat one.txt >> "$out"; \
      done; }; \
    pick cuda.txt nvidia-cublas nvidia-cudnn-cu13 nvidia-cufft nvidia-cusolver nvidia-cusparse nvidia-cusparselt-cu13 \
      nvidia-nccl-cu13 nvidia-nvshmem-cu13 nvidia-cuda-nvrtc nvidia-curand nvidia-nvjitlink nvidia-cuda-runtime \
      nvidia-cuda-cupti nvidia-cufile nvidia-nvtx cuda-toolkit; \
    pick torch.txt torch triton torchvision; \
    pick trt.txt tensorrt-cu13-libs; \
    pick wheels.txt onnxruntime-gpu cupy-cuda12x polars-runtime-32 pyarrow opencv-python opencv-python-headless scipy av; \
    rm one.txt

FROM ${BASE_IMAGE}

WORKDIR /opt/simai/ai
ENV UV_PROJECT_ENVIRONMENT=/opt/simai/ai/.venv \
    VIRTUAL_ENV=/opt/simai/ai/.venv \
    PATH=/opt/simai/ai/.venv/bin:${PATH}

# 1)~3) 큰 휠을 묶음마다 다른 층으로(--require-hashes: 잠금의 해시와 다르면 실패, --no-deps: 의존성은 4) 의 uv sync 가 맞춘다).
#    한 층(11.5GB)이던 것을 나눠 레지스트리에 병렬로 올리고, 끊겨도 끝난 층은 다시 올리지 않는다.
RUN uv venv -q /opt/simai/ai/.venv
COPY --from=layer-lists /src/cuda.txt /tmp/layers/
RUN --mount=type=cache,target=/root/.cache/uv uv pip install -q --no-deps --require-hashes -r /tmp/layers/cuda.txt
COPY --from=layer-lists /src/torch.txt /tmp/layers/
RUN --mount=type=cache,target=/root/.cache/uv uv pip install -q --no-deps --require-hashes -r /tmp/layers/torch.txt
# TensorRT libs 는 pypi.nvidia.com 에만 있다(ai/pyproject.toml [tool.uv.sources]). 이 층만 그 색인을 쓴다
COPY --from=layer-lists /src/trt.txt /tmp/layers/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install -q --no-deps --require-hashes --index-url https://pypi.nvidia.com -r /tmp/layers/trt.txt
COPY --from=layer-lists /src/wheels.txt /tmp/layers/
RUN --mount=type=cache,target=/root/.cache/uv uv pip install -q --no-deps --require-hashes -r /tmp/layers/wheels.txt

# 4) 나머지 의존성(잠금이 바뀌면 이 층부터 다시). --locked: 잠금이 pyproject 와 다르면 실패. 위 층의 패키지는 같은 판이라 건너뛴다.
COPY ai/pyproject.toml ai/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra tier0 --no-install-project

# 5) simai_ai 패키지(편집 모드 아님)와 배포 단위(services 는 설치하지 않고 이 폴더에서 실행), 계약 스키마
COPY ai/src ./src
COPY ai/services ./services
COPY contracts /opt/simai/contracts
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra tier0 --no-editable \
 && rm -rf ./src

COPY --chmod=0755 deploy/tier0/entrypoint.sh /usr/local/bin/simai-tier0
# 분석 워커를 Tier 0 준비(표시 파일 phase=running·reserve OK) 뒤에 띄우는 도구 (S15P31A101-60·-183, deploy/README "분석 워커")
COPY --chmod=0755 deploy/scripts/wait_tier0_ready.py /usr/local/bin/simai-wait-tier0
# 어느 GPU·지역·사이트에서든 같은 순서로 준비하는 시작 스크립트와 가중치 목록 (S15P31A101-205, deploy/README "simai-bootstrap")
COPY --chmod=0755 deploy/scripts/simai_bootstrap.py /usr/local/bin/simai-bootstrap
# 윈도 체크아웃의 CRLF 가 shebang 을 깨뜨리지 않게 빌드에서 CR 을 뺀다(check_image.sh 는 결과를 다시 검사한다)
RUN sed -i 's/\r$//' /usr/local/bin/simai-tier0 /usr/local/bin/simai-wait-tier0 /usr/local/bin/simai-bootstrap
COPY deploy/weights.json /opt/simai/deploy/weights.json

# 실행 중에 패키지를 설치하지 않는다(ultralytics 자동 설치 끔). 없는 의존성은 실패로 드러나야 한다.
# NVIDIA_DRIVER_CAPABILITIES 의 video: LIVE 입력 NVDEC(libnvcuvid)을 컨테이너에 올린다(S15P31A101-182).
# SIMAI_ROOT 는 고정하지 않는다(simai-bootstrap 이 SIMAI_ROOT → /workspace/simai → /srv/simai 순서로 고른다).
ENV SIMAI_CONTRACTS_DIR=/opt/simai/contracts \
    SIMAI_WEIGHTS_MANIFEST=/opt/simai/deploy/weights.json \
    YOLO_AUTOINSTALL=false \
    YOLO_OFFLINE=true \
    YOLO_CONFIG_DIR=/tmp/ultralytics \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility,video

# 기본(인자 없음)은 준비 → Tier 0. 'sleep infinity'·bash 같은 다른 명령은 준비 없이 그대로 실행한다.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/simai-start", "/usr/local/bin/simai-bootstrap"]
CMD ["tier0"]
