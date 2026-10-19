# Tier 0 서비스 이미지 (명세 3.1 RunPod Tier 0, 3.12, 21.5). 빌드: deploy/README.md (docker buildx bake tier0).
# - Python 의존성은 ai/uv.lock 그대로(uv sync --locked). pyproject 와 잠금이 어긋나면 빌드가 실패한다.
# - 가중치(YOLO26x-pose .pt)와 TensorRT 엔진은 이미지에 없다. 둘 다 Network Volume(/workspace/simai)에 둔다.
#   엔진은 Pod 첫 기동 때 그 GPU 에서 만들고 캐시한다(entrypoint → simai_ai.detection.engine_cache).
# - 빌드 컨텍스트는 저장소 최상위이고, 들어오는 파일은 deploy/tier0/Dockerfile.dockerignore 의 허용 목록뿐이다.
ARG BASE_IMAGE=simai-base
FROM ${BASE_IMAGE}

WORKDIR /opt/simai/ai
ENV UV_PROJECT_ENVIRONMENT=/opt/simai/ai/.venv \
    VIRTUAL_ENV=/opt/simai/ai/.venv \
    PATH=/opt/simai/ai/.venv/bin:${PATH}

# 1) 의존성만 (소스가 바뀌어도 이 층은 다시 받지 않는다). --locked: 잠금이 pyproject 와 다르면 실패.
COPY ai/pyproject.toml ai/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra tier0 --no-install-project

# 2) simai_ai 패키지(편집 모드 아님)와 배포 단위(services 는 설치하지 않고 이 폴더에서 실행), 계약 스키마
COPY ai/src ./src
COPY ai/services ./services
COPY contracts /opt/simai/contracts
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra tier0 --no-editable \
 && rm -rf ./src

COPY --chmod=0755 deploy/tier0/entrypoint.sh /usr/local/bin/simai-tier0
# 분석 워커를 Tier 0 준비(표시 파일 phase=running·reserve OK) 뒤에 띄우는 도구 (S15P31A101-60·-183, deploy/README "분석 워커")
COPY --chmod=0755 deploy/scripts/wait_tier0_ready.py /usr/local/bin/simai-wait-tier0

# 실행 중에 패키지를 설치하지 않는다(ultralytics 자동 설치 끔). 없는 의존성은 실패로 드러나야 한다.
# NVIDIA_DRIVER_CAPABILITIES 의 video: LIVE 입력 NVDEC(libnvcuvid)을 컨테이너에 올린다(S15P31A101-182).
ENV SIMAI_CONTRACTS_DIR=/opt/simai/contracts \
    SIMAI_ROOT=/workspace/simai \
    YOLO_AUTOINSTALL=false \
    YOLO_OFFLINE=true \
    YOLO_CONFIG_DIR=/tmp/ultralytics \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility,video

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/simai-start", "/usr/local/bin/simai-tier0"]
CMD []
