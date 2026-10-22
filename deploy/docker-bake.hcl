# simai 이미지 빌드 정의. 저장소 최상위에서 실행한다 (자세한 절차: deploy/README.md).
#   docker buildx bake tier0                     # 베이스(cu130) → Tier 0, 로컬 이미지로 적재
#   docker buildx bake exp02                     # 베이스(cu128 base·devel) → EXP-02 HMR (Detectron2 를 devel 단계에서 빌드)
#   docker buildx bake mapanything               # 베이스(cu128 base, Python 3.12) → EXP-01 MapAnything
#   docker buildx bake risk-engine               # EC2 Risk Engine 컨테이너(CPU, python slim, S15P31A101-99)
#   TAG=$(git rev-parse --short=12 HEAD) docker buildx bake ec2   # EC2 운영 스택 server·web·risk-engine (S15P31A101-66, deploy/ec2)
#   REGISTRY=<레지스트리>/simai TAG=$(git rev-parse --short=12 HEAD) docker buildx bake --push tier0   # 푸시(사람이 확인한 뒤)
# 외부 이미지는 모두 다이제스트로 고정한다. 바꿀 때는 태그와 다이제스트를 함께 바꾸고 MR 에 적는다.

variable "REGISTRY" { default = "simai" }
variable "TAG" { default = "dev" }

# docker.io/nvidia/cuda:13.0.3-base-ubuntu24.04 (CUDA 13.0 런타임 + 순방향 호환 라이브러리. torch 2.14.1 휠의 nvidia-cuda-runtime 13.0.96 과 같은 판)
variable "CUDA_TIER0" { default = "nvidia/cuda:13.0.3-base-ubuntu24.04@sha256:7c7413a56200486f71f181cad9310f6fd31b6bb21816ade15fc9c1e1e927a5c1" }
# 실험 이미지(S15P31A101-207): torch 2.7.1+cu128 휠이 CUDA 12.8 라이브러리를 직접 가져오므로 실행은 base, Detectron2 빌드만 devel(nvcc 12.8: sm_100·sm_120 지원)
# docker.io/nvidia/cuda:12.8.1-base-ubuntu24.04
variable "CUDA_EXP" { default = "nvidia/cuda:12.8.1-base-ubuntu24.04@sha256:133c78a0575303be34164d0b90137a042172bdf60696af01a3c424ab402d86e2" }
# docker.io/nvidia/cuda:12.8.1-devel-ubuntu24.04 (빌드 단계만. 실행 이미지에 들어가지 않는다)
variable "CUDA_EXP_DEVEL" { default = "nvidia/cuda:12.8.1-devel-ubuntu24.04@sha256:520292dbb4f755fd360766059e62956e9379485d9e073bbd2f6e3c20c270ed66" }
# ghcr.io/astral-sh/uv:0.11.27 (노트북 uv 와 같은 판)
variable "UV_IMAGE" { default = "ghcr.io/astral-sh/uv:0.11.27@sha256:4d01caf3b22dfd11003455e2e68153da08c4ee1fa54fdbd166c6282d22693419" }
variable "PYTHON_VERSION" { default = "3.11.15" }
# docker.io/library/python:3.11.15-slim-trixie (EC2 Risk Engine 컨테이너, CPU 전용. Tier 0 와 같은 Python 판)
variable "PYTHON_RISK_ENGINE" { default = "python:3.11.15-slim-trixie@sha256:90744cff8f32887f075c47d747a173ff333e9e98801667af93c357fa9f5e28ff" }
# EC2 운영 스택(S15P31A101-66). Java 21 은 server/build.gradle toolchain, Node·pnpm 은 front/.node-version·packageManager 와 같은 판
# docker.io/library/eclipse-temurin:21-jdk-noble (server 빌드 단계만)
variable "JDK_SERVER" { default = "eclipse-temurin:21-jdk-noble@sha256:107215954359f1ab0be058a247d7c64ed6e69c71c1682d471d38ef751ec8e964" }
# docker.io/library/eclipse-temurin:21-jre-noble (server 실행)
variable "JRE_SERVER" { default = "eclipse-temurin:21-jre-noble@sha256:35501d3f93173b420ac9001658eb68157e813316b865087e345c187fca422c4c" }
# docker.io/library/node:24.19.0-bookworm-slim (관제 앱 빌드 단계만)
variable "NODE_WEB" { default = "node:24.19.0-bookworm-slim@sha256:a9f5f7c91a432850b2a8a7797adf5eadb6c733ceed61167806cee7ea7fbc29df" }
# docker.io/library/nginx:1.30.6-alpine
variable "NGINX_WEB" { default = "nginx:1.30.6-alpine@sha256:c59d1f8b1e0e42da75be973424cd72e23f7f8adfb54e982f4d1cd0216a5a5576" }
# 관제 앱이 부를 공개 도메인(빌드에 박힌다). deploy/ec2 의 SIMAI_DOMAIN 과 같게 둔다
variable "SIMAI_DOMAIN" { default = "simai.live" }
# MapAnything 은 볼륨 venv(Python 3.12)와 같은 판대
variable "PYTHON_VERSION_MAPANYTHING" { default = "3.12.13" }
# EXP-02 Detectron2 CUDA 확장 아키텍처: Ampere 이후 전부(A100 8.0, A40·A10 8.6, L4·L40S·4090 8.9, H100 9.0, B200 10.0, RTX 5090·PRO 6000 12.0).
# 같은 주 버전의 더 높은 부 버전(8.7, 10.3, 12.1)은 SASS 호환으로 돈다. +PTX 는 넣지 않는다(근거: docs/design/exp-images.md)
variable "EXP02_CUDA_ARCH" { default = "8.0;8.6;8.9;9.0;10.0;12.0" }
# Detectron2 nvcc 병렬 수. Docker Desktop 메모리가 작으면(2GB) 1~2
variable "EXP02_MAX_JOBS" { default = "2" }

group "default" { targets = ["tier0"] }
group "exp" { targets = ["exp02", "mapanything"] }
group "ec2" { targets = ["server", "web", "risk-engine"] }

target "_common" {
  context   = "."
  platforms = ["linux/amd64"]
  labels = {
    "org.opencontainers.image.source"   = "S15P31A101 simai"
    "org.opencontainers.image.revision" = "${TAG}"
  }
}

target "base-cu130" {
  inherits   = ["_common"]
  dockerfile = "deploy/base/Dockerfile"
  args = {
    CUDA_IMAGE     = "${CUDA_TIER0}"
    UV_IMAGE       = "${UV_IMAGE}"
    PYTHON_VERSION = "${PYTHON_VERSION}"
  }
  tags = ["${REGISTRY}/base:cu130-${TAG}"]
}

target "tier0" {
  inherits   = ["_common"]
  dockerfile = "deploy/tier0/Dockerfile"
  contexts   = { simai-base = "target:base-cu130" }
  args       = { BASE_IMAGE = "simai-base" }
  tags       = ["${REGISTRY}/tier0:${TAG}"]
}

target "base-cu128" {
  inherits   = ["_common"]
  dockerfile = "deploy/base/Dockerfile"
  args = {
    CUDA_IMAGE     = "${CUDA_EXP}"
    UV_IMAGE       = "${UV_IMAGE}"
    PYTHON_VERSION = "${PYTHON_VERSION}"
  }
  tags = ["${REGISTRY}/base:cu128-${TAG}"]
}

target "base-cu128-devel" {
  inherits   = ["_common"]
  dockerfile = "deploy/base/Dockerfile"
  args = {
    CUDA_IMAGE     = "${CUDA_EXP_DEVEL}"
    UV_IMAGE       = "${UV_IMAGE}"
    PYTHON_VERSION = "${PYTHON_VERSION}"
  }
  tags = ["${REGISTRY}/base:cu128-devel-${TAG}"]
}

target "base-cu128-py312" {
  inherits   = ["_common"]
  dockerfile = "deploy/base/Dockerfile"
  args = {
    CUDA_IMAGE     = "${CUDA_EXP}"
    UV_IMAGE       = "${UV_IMAGE}"
    PYTHON_VERSION = "${PYTHON_VERSION_MAPANYTHING}"
  }
  tags = ["${REGISTRY}/base:cu128-py312-${TAG}"]
}

target "exp02" {
  inherits   = ["_common"]
  dockerfile = "deploy/exp02/Dockerfile"
  contexts = {
    simai-base-cu128       = "target:base-cu128"
    simai-base-cu128-devel = "target:base-cu128-devel"
  }
  args = {
    BASE_IMAGE           = "simai-base-cu128"
    BUILD_IMAGE          = "simai-base-cu128-devel"
    TORCH_CUDA_ARCH_LIST = "${EXP02_CUDA_ARCH}"
    MAX_JOBS             = "${EXP02_MAX_JOBS}"
  }
  tags = ["${REGISTRY}/exp02-fs3db:${TAG}"]
}

target "mapanything" {
  inherits   = ["_common"]
  dockerfile = "deploy/mapanything/Dockerfile"
  contexts   = { simai-base-cu128-py312 = "target:base-cu128-py312" }
  args       = { BASE_IMAGE = "simai-base-cu128-py312" }
  tags       = ["${REGISTRY}/mapanything:${TAG}"]
}

# EC2 Risk Engine 컨테이너(S15P31A101-99): CPU 전용, CUDA 베이스를 쓰지 않는다
target "risk-engine" {
  inherits   = ["_common"]
  dockerfile = "deploy/risk-engine/Dockerfile"
  args = {
    PYTHON_IMAGE = "${PYTHON_RISK_ENGINE}"
    UV_IMAGE     = "${UV_IMAGE}"
  }
  tags = ["${REGISTRY}/risk-engine:${TAG}"]
}

# EC2 Spring 서버(S15P31A101-66): JDK 로 bootJar, JRE 로 실행
target "server" {
  inherits   = ["_common"]
  dockerfile = "deploy/server/Dockerfile"
  args = {
    JDK_IMAGE = "${JDK_SERVER}"
    JRE_IMAGE = "${JRE_SERVER}"
  }
  tags = ["${REGISTRY}/server:${TAG}"]
}

# EC2 Nginx(S15P31A101-66): 관제 앱 정적 파일 + HTTPS + /api·/ws 프록시
target "web" {
  inherits   = ["_common"]
  dockerfile = "deploy/web/Dockerfile"
  args = {
    NODE_IMAGE        = "${NODE_WEB}"
    NGINX_IMAGE       = "${NGINX_WEB}"
    VITE_API_BASE_URL = "https://${SIMAI_DOMAIN}"
    VITE_STOMP_URL    = "wss://${SIMAI_DOMAIN}/ws"
  }
  tags = ["${REGISTRY}/web:${TAG}"]
}
