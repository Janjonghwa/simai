# EC2 Risk Engine 컨테이너 이미지 (명세 3.2, 3.8, 12.2, S15P31A101-99). 빌드: deploy/README.md (docker buildx bake risk-engine).
# - CPU 전용 python slim. Tier 0 와 같은 ai/uv.lock·같은 simai_ai 코드(Risk Engine 라이브러리)이고 extra 는 risk_api 만 넣는다
#   (fastapi·uvicorn·jsonschema. torch·numpy·opencv 없음). 판정은 simai_ai.risk.plan, API 는 services/risk_engine.
# - 비루트 사용자(uid 10001)로 0.0.0.0:8080 에서 듣고 포트를 공개하지 않는다(EXPOSE 없음, compose 내부망만, deploy/compose.yaml).
# - 비밀 값·가중치·외부 저장소 접근 없음. 계약 스키마(contracts/, 예시 제외)와 공통 테스트 벡터(D-G3-04)를 싣는다.
# - 빌드 컨텍스트는 저장소 최상위이고, 들어오는 파일은 deploy/risk-engine/Dockerfile.dockerignore 의 허용 목록뿐이다.
ARG PYTHON_IMAGE
ARG UV_IMAGE
FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /opt/simai/ai
# 이미지의 Python(/usr/local/bin/python3.11)만 쓴다(uv 가 다른 Python 을 받지 않는다)
ENV UV_PROJECT_ENVIRONMENT=/opt/simai/ai/.venv \
    UV_PYTHON_PREFERENCE=only-system \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
# --locked: 잠금이 pyproject 와 다르면 빌드가 실패한다
COPY ai/pyproject.toml ai/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra risk_api --no-install-project
COPY ai/src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra risk_api --no-editable

FROM ${PYTHON_IMAGE}
RUN groupadd --system --gid 10001 simai \
 && useradd --system --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin simai
WORKDIR /opt/simai/ai
COPY --from=build /opt/simai/ai/.venv ./.venv
COPY ai/services/__init__.py ./services/__init__.py
COPY ai/services/risk_engine ./services/risk_engine
COPY contracts /opt/simai/contracts
COPY ai/tests/risk/vectors /opt/simai/vectors
ENV PATH=/opt/simai/ai/.venv/bin:${PATH} \
    VIRTUAL_ENV=/opt/simai/ai/.venv \
    SIMAI_CONTRACTS_DIR=/opt/simai/contracts \
    SIMAI_IMAGE_KIND=risk_engine \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status == 200 else 1)"]
CMD ["python", "-m", "services.risk_engine", "--host", "0.0.0.0", "--port", "8080"]
