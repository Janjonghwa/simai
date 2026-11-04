# simai 공통 베이스: CUDA 이미지 + Python 3.11(uv 관리) + uv + RunPod SSH 시작 스크립트 + 막힌 망 예비 SSH 용 wstunnel.
# CUDA 이미지는 모델마다 다르므로 인자로 받는다(다이제스트 고정 값은 deploy/docker-bake.hcl 에 있다).
#   Tier 0: nvidia/cuda:13.0.3-base-ubuntu24.04  (ai/uv.lock 의 torch 2.14.1 휠이 CUDA 13.0 런타임을 직접 가져온다)
#   EXP-02: nvidia/cuda:12.4.1-cudnn-devel-ubuntu22.04 (torch 2.5.1+cu124, Detectron2 CUDA 확장 빌드에 nvcc 필요)
# 이 이미지에는 가중치·데이터·비밀 값을 넣지 않는다. SSH 호스트 키도 컨테이너가 시작할 때 만든다.
ARG CUDA_IMAGE
ARG UV_IMAGE
FROM ${UV_IMAGE} AS uv

# 막힌 망 예비 SSH(S15P31A101-286, 명세 3.12 443 예비): RunPod HTTPS 프록시 위 WebSocket 을 sshd 로 잇는 wstunnel 서버.
# 고정 판 릴리스를 받아 sha256 이 다르면 빌드를 멈춘다. 받은 묶음이 최종 이미지 층에 남지 않게 따로 단계를 둔다(이미지는 linux/amd64 만).
FROM ${CUDA_IMAGE} AS wstunnel
ARG WSTUNNEL_VERSION=10.7.1
ARG WSTUNNEL_SHA256=fa842ed53fbb14b1c69cd98829f9895d7f8a6b0d562c57c1175851a52cea9ea2
ADD https://github.com/erebe/wstunnel/releases/download/v${WSTUNNEL_VERSION}/wstunnel_${WSTUNNEL_VERSION}_linux_amd64.tar.gz /tmp/wstunnel.tar.gz
RUN echo "${WSTUNNEL_SHA256}  /tmp/wstunnel.tar.gz" | sha256sum -c - \
 && tar -xzf /tmp/wstunnel.tar.gz -C /tmp wstunnel

FROM ${CUDA_IMAGE}
ARG PYTHON_VERSION=3.11.15

# tini: PID 1 신호 전달·좀비 회수. openssh-server: RunPod·Vast.ai 공개 IP:포트 SSH(키 인증만, 명세 3.12. 엘리스 VM 은 호스트 sshd).
# 설치 때 만들어지는 SSH 호스트 키는 지운다(이미지에 남으면 모든 Pod 가 같은 호스트 키를 쓴다).
# hadolint ignore=DL3008
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates tini openssh-server \
 && rm -rf /var/lib/apt/lists/* /etc/ssh/ssh_host_* \
 && mkdir -p /run/sshd

COPY --from=uv /uv /uvx /usr/local/bin/
# 묶음 안 실행 파일은 0644 라 실행 권한을 준다. /tmp 는 Pod 에서 실행이 막힐 수 있어(noexec) PATH 의 /usr/local/bin 에 둔다
COPY --from=wstunnel --chmod=0755 /tmp/wstunnel /usr/local/bin/wstunnel

# Python 은 uv 가 관리하는 고정 버전(python-build-standalone)을 쓴다. 우분투 기본 Python(22.04 는 3.10, 24.04 는 3.12)을 쓰지 않는다.
ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python \
    UV_PYTHON=${PYTHON_VERSION} \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
# SIMAI_ROOT 는 고정하지 않는다: Tier 0 는 simai-bootstrap 이 저장 위치를 고르고(S15P31A101-205), EXP-02 는 자기 Dockerfile 에서 정한다.
RUN uv python install ${PYTHON_VERSION} \
 && ln -s "$(uv python find ${PYTHON_VERSION})" /usr/local/bin/python3

COPY --chmod=0755 deploy/base/simai-start.sh /usr/local/bin/simai-start
# 윈도 체크아웃의 CRLF 가 shebang 을 깨뜨리지 않게 빌드에서 CR 을 뺀다(check_image.sh 는 결과를 다시 검사한다)
RUN sed -i 's/\r$//' /usr/local/bin/simai-start

# RunPod 은 컨테이너를 root 로 띄우고 Network Volume(/workspace)은 chmod 가 적용되지 않는다(명세 3.12).
# sshd 도 root 가 필요하므로 사용자를 바꾸지 않는다.
# hadolint ignore=DL3002
USER root
WORKDIR /opt/simai
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/simai-start"]
CMD ["bash"]
