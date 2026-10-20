#!/usr/bin/env bash
# 공통 시작 스크립트. 공개키가 있으면 키 인증만 받는 sshd 를 띄우고, 넘겨받은 명령을 exec 한다(S15P31A101-32·-205).
# 공개키를 받는 곳(있는 것을 모두 합친다):
#   PUBLIC_KEY                  RunPod 이 계정 SSH 키로 채운다
#   SSH_PUBLIC_KEY              Vast.ai 가 계정 SSH 키로 채운다(Entrypoint 실행 방식. SSH 실행 방식은 이미지 진입점을 바꿔 이 스크립트가 돌지 않는다)
#   SIMAI_AUTHORIZED_KEYS_FILE  공개키 파일 경로(한 줄에 키 하나, 위 두 방식이 없는 곳). 옵션이 붙은 authorized_keys 줄은 버린다
# SIMAI_SSHD=off: 공개키가 있어도 컨테이너 sshd 를 띄우지 않는다(엘리스 GPU VM 처럼 호스트 sshd 로 들어가는 곳).
# 비밀번호·키보드 인증은 항상 끈다. 비밀 값은 여기서 만들거나 저장하지 않는다. 호스트 키는 컨테이너마다 새로 만든다(이미지에 넣지 않음).
# 시험용 경로: SIMAI_SSH_DIR(기본 /root/.ssh), SIMAI_SSHD_CONFIG_DIR(기본 /etc/ssh/sshd_config.d), SIMAI_SSHD_BIN(기본 /usr/sbin/sshd), SIMAI_SSH_KEYGEN_BIN(기본 ssh-keygen).
set -euo pipefail

# HF 토큰은 넘겨받은 명령(simai-bootstrap)에만 넘긴다. 이 스크립트의 다른 자식(cat·tr·awk·mkdir·chmod·wc·ssh-keygen·sshd)은
# 받지 않도록 맨 먼저 내보내지 않는 셸 변수로 옮기고 환경에서 지운 뒤, exec 바로 앞에서만 다시 내보낸다(-205 리뷰 R1).
hf_token=${HF_TOKEN-}
hf_token_set=${HF_TOKEN+1}
unset HF_TOKEN HUGGING_FACE_HUB_TOKEN

SSH_DIR=${SIMAI_SSH_DIR:-/root/.ssh}
CONF_DIR=${SIMAI_SSHD_CONFIG_DIR:-/etc/ssh/sshd_config.d}
SSHD=${SIMAI_SSHD_BIN:-/usr/sbin/sshd}
KEYGEN=${SIMAI_SSH_KEYGEN_BIN:-ssh-keygen}

keys=""
for k in "${PUBLIC_KEY:-}" "${SSH_PUBLIC_KEY:-}"; do
  if [ -n "$k" ]; then keys+="$k"$'\n'; fi
done
if [ -n "${SIMAI_AUTHORIZED_KEYS_FILE:-}" ]; then
  if [ -r "$SIMAI_AUTHORIZED_KEYS_FILE" ]; then
    keys+="$(cat "$SIMAI_AUTHORIZED_KEYS_FILE")"$'\n'
  else
    echo "simai-start: SIMAI_AUTHORIZED_KEYS_FILE=$SIMAI_AUTHORIZED_KEYS_FILE 를 읽을 수 없다" >&2
  fi
fi
# 한 줄에 공개키 하나만 받는다: '<형식> <base64> [설명]'. 옵션(command=·from= 등)이 앞에 붙은 줄, 제어 문자가 든 줄은 버리고 경고한다.
# 빈 줄·CR(Windows 에서 만든 파일)을 빼고 같은 키는 한 번만
keys=$(printf '%s' "$keys" | tr -d '\r' | awk '
  !NF { next }
  /[[:cntrl:]]/ || $0 !~ /^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com) [A-Za-z0-9+\/]+=*( [^[:cntrl:]]*)?$/ { bad++; next }
  !seen[$0]++ { print }
  END { if (bad) printf "simai-start: 공개키 형식이 아닌 줄 %d개를 버렸다(옵션·제어 문자는 받지 않는다)\n", bad > "/dev/stderr" }')

case "${SIMAI_SSHD:-on}" in
  off|0|false|no)
    if [ -n "$keys" ]; then echo "simai-start: SIMAI_SSHD=off → 컨테이너 sshd 를 띄우지 않는다(호스트 sshd 사용)" >&2; fi
    keys="" ;;
esac

if [ -n "$keys" ]; then
  mkdir -p "$SSH_DIR"
  mkdir -p /run/sshd 2>/dev/null || true  # 이미지 빌드 때도 만든다. 없으면 sshd 가 이유를 출력하고 실패한다
  chmod 700 "$SSH_DIR"
  printf '%s\n' "$keys" > "$SSH_DIR/authorized_keys"
  chmod 600 "$SSH_DIR/authorized_keys"
  "$KEYGEN" -A >/dev/null
  # 설정 파일에도 적어 sshd -T 로 확인할 수 있게 한다(sshd_config.d 는 먼저 읽힌 값이 이긴다)
  mkdir -p "$CONF_DIR"
  printf '%s\n' 'PasswordAuthentication no' 'KbdInteractiveAuthentication no' 'PermitRootLogin prohibit-password' \
    > "$CONF_DIR/00-simai.conf"
  "$SSHD"
  echo "simai-start: sshd 시작 (키 인증만, 키 $(printf '%s\n' "$keys" | wc -l | tr -d ' ')개)" >&2
fi

if [ "$#" -eq 0 ]; then
  set -- bash
fi
if [ -n "$hf_token_set" ]; then export HF_TOKEN="$hf_token"; fi
exec "$@"
