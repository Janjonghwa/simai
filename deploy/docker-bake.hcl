# simai 이미지 빌드 정의. 저장소 최상위에서 실행한다 (자세한 절차: deploy/README.md).
#   docker buildx bake tier0                     # 베이스(cu130) → Tier 0, 로컬 이미지로 적재
#   docker buildx bake unified                   # 베이스(cu130) → Tier 0 층 + 워커 스택(vLLM·HMR) 통합 이미지 (S15P31A101-246)
#   docker buildx bake exp02                     # 베이스(cu128 base) → EXP-02 HMR (Detectron2 는 CUDA 확장 없이 빌드)
#   docker buildx bake mapanything               # 베이스(cu128 base, Python 3.12) → EXP-01 MapAnything
#   docker buildx bake risk-engine               # EC2 Risk Engine 컨테이너(CPU, python slim, S15P31A101-99)
#   TAG=$(git rev-parse --short=12 HEAD) docker buildx bake ec2   # EC2 운영 스택 server·web·risk-engine (S15P31A101-66, deploy/ec2)
#   REGISTRY=<레지스트리>/simai TAG=$(git rev-parse --short=12 HEAD) docker buildx bake --push tier0   # 푸시(사람이 확인한 뒤)
# 외부 이미지는 모두 다이제스트로 고정한다. 바꿀 때는 태그와 다이제스트를 함께 바꾸고 MR 에 적는다.

variable "REGISTRY" { default = "simai" }
variable "TAG" { default = "dev" }

# docker.io/nvidia/cuda:13.0.3-base-ubuntu24.04 (CUDA 13.0 런타임 + 순방향 호환 라이브러리. torch 2.11.0 cu130 휠의 CUDA 13.0 런타임과 같은 주·부 판)
variable "CUDA_TIER0" { default = "nvidia/cuda:13.0.3-base-ubuntu24.04@sha256:7c7413a56200486f71f181cad9310f6fd31b6bb21816ade15fc9c1e1e927a5c1" }
# 실험 이미지(S15P31A101-207): torch 2.7.1+cu128 휠이 CUDA 12.8 라이브러리를 직접 가져오므로 base. Detectron2 도 CUDA 확장 없이 base 에서 빌드(S15P31A101-238)
# docker.io/nvidia/cuda:12.8.1-base-ubuntu24.04
variable "CUDA_EXP" { default = "nvidia/cuda:12.8.1-base-ubuntu24.04@sha256:133c78a0575303be34164d0b90137a042172bdf60696af01a3c424ab402d86e2" }
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
# CI Jenkins(S15P31A101-28). docker.io/jenkins/jenkins:lts-jdk21 (2026-10-22: Jenkins 2.580.1, JDK 21.0.12, Debian 13)
# Node·uv·Python 은 위 NODE_WEB·UV_IMAGE·PYTHON_VERSION 을 같이 쓴다(배포 이미지와 같은 판으로 검사)
variable "JENKINS_IMAGE" { default = "jenkins/jenkins:lts-jdk21@sha256:a660310e39ade10631f774bacd5219de767dfd08947ea5c32a739b5e5bb382c1" }
# MapAnything 은 볼륨 venv(Python 3.12)와 같은 판대
variable "PYTHON_VERSION_MAPANYTHING" { default = "3.12.13" }
# Detectron2 CPU 확장 컴파일 병렬 수(exp02). Docker Desktop 메모리가 작으면(2GB) 1~2
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
  target     = "tier0"
  contexts   = { simai-base = "target:base-cu130" }
  args       = { BASE_IMAGE = "simai-base" }
  tags       = ["${REGISTRY}/tier0:${TAG}"]
}

# 통합 이미지(S15P31A101-246): tier0 와 같은 Dockerfile·같은 큰 층 위에 워커 스택(vLLM·HMR)을 더한다. Tier 0 → vLLM → 분석 워커를 simai-bootstrap 이 감시
target "unified" {
  inherits   = ["_common"]
  dockerfile = "deploy/tier0/Dockerfile"
  target     = "unified"
  contexts   = { simai-base = "target:base-cu130" }
  args       = { BASE_IMAGE = "simai-base" }
  tags       = ["${REGISTRY}/unified:${TAG}"]
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
  contexts = { simai-base-cu128 = "target:base-cu128" }
  args = {
    BASE_IMAGE  = "simai-base-cu128"
    BUILD_IMAGE = "simai-base-cu128"
    MAX_JOBS    = "${EXP02_MAX_JOBS}"
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

# CI Jenkins(S15P31A101-28): 공식 LTS + 각 파트 검사 도구 + 플러그인. 앱 배포(ec2 묶음)와 따로 빌드한다
target "jenkins" {
  inherits   = ["_common"]
  dockerfile = "deploy/jenkins/Dockerfile"
  args = {
    JENKINS_IMAGE  = "${JENKINS_IMAGE}"
    NODE_IMAGE     = "${NODE_WEB}"
    UV_IMAGE       = "${UV_IMAGE}"
    PYTHON_VERSION = "${PYTHON_VERSION}"
  }
  tags = ["${REGISTRY}/jenkins:${TAG}"]
}
