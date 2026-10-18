#!/usr/bin/env bash
# 공통 시작 스크립트. RunPod 이 PUBLIC_KEY 를 주면 키 인증만 받는 sshd 를 띄우고, 넘겨받은 명령을 exec 한다.
# 비밀 값은 여기서 만들거나 저장하지 않는다. 호스트 키는 컨테이너마다 새로 만든다(이미지에 넣지 않음).
set -euo pipefail

if [ -n "${PUBLIC_KEY:-}" ]; then
  mkdir -p /root/.ssh /run/sshd
  chmod 700 /root/.ssh
  printf '%s\n' "$PUBLIC_KEY" > /root/.ssh/authorized_keys
  chmod 600 /root/.ssh/authorized_keys
  ssh-keygen -A >/dev/null
  # 설정 파일에도 적어 sshd -T 로 확인할 수 있게 한다(sshd_config.d 는 먼저 읽힌 값이 이긴다)
  mkdir -p /etc/ssh/sshd_config.d
  printf '%s\n' 'PasswordAuthentication no' 'KbdInteractiveAuthentication no' 'PermitRootLogin prohibit-password' \
    > /etc/ssh/sshd_config.d/00-simai.conf
  /usr/sbin/sshd
  echo "simai-start: sshd 시작 (키 인증만)" >&2
fi

if [ "$#" -eq 0 ]; then
  set -- bash
fi
exec "$@"
