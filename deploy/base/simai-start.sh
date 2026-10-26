#!/usr/bin/env bash
# 공통 시작 스크립트. 공개키가 있으면 키 인증만 받는 sshd 를 띄우고, 넘겨받은 명령을 exec 한다(S15P31A101-32·-205).
# 공개키를 받는 곳(있는 것을 모두 합친다):
#   PUBLIC_KEY                  RunPod 이 계정 SSH 키로 채운다
#   SSH_PUBLIC_KEY              Vast.ai 가 계정 SSH 키로 채운다(Entrypoint 실행 방식. SSH 실행 방식은 이미지 진입점을 바꿔 이 스크립트가 돌지 않는다)
#   SIMAI_AUTHORIZED_KEYS_FILE  공개키 파일 경로(한 줄에 키 하나, 위 두 방식이 없는 곳). 옵션이 붙은 authorized_keys 줄은 버린다
# SIMAI_SSHD=off: 공개키가 있어도 컨테이너 sshd 를 띄우지 않는다(엘리스 GPU VM 처럼 호스트 sshd 로 들어가는 곳).
# 비밀번호·키보드 인증은 항상 끈다. 비밀 값은 여기서 만들거나 저장하지 않는다. 호스트 키는 컨테이너마다 새로 만든다(이미지에 넣지 않음).
# sshd 를 띄우면 SSH 세션용으로 비밀이 아닌 컨테이너 환경 변수를 /etc/profile.d/simai-env.sh 에 적는다(로그인 셸이 읽는다, S15P31A101-286).
# 막힌 망 예비 SSH(S15P31A101-286, 명세 3.12 443 예비): sshd 를 띄운 경우에만, SIMAI_TUNNEL_PREFIX(경로 비밀값, RunPod Secret)가 있으면
#   wstunnel 서버를 ws://0.0.0.0:${SIMAI_TUNNEL_PORT:-8080} 에 띄워 RunPod HTTPS 프록시(443)로 들어온 WebSocket 을 127.0.0.1:22 로만 잇는다.
#   SIMAI_TUNNEL_ALLOW(쉼표로 나눈 포트 번호, 예 17000,17001, S15P31A101-288)를 주면 127.0.0.1 의 그 포트도 잇는다(막힌 망 I0 예비 경로).
#   22 는 늘 허용한다. 목록이 틀리면(포트 번호가 아님·터널 포트 자신·16개 초과) 하나도 더하지 않고 22 만 연다(대상을 넓히지 않는 쪽, SSH 는 남긴다).
# 시험용 경로: SIMAI_SSH_DIR(기본 /root/.ssh), SIMAI_SSHD_CONFIG_DIR(기본 /etc/ssh/sshd_config.d), SIMAI_SSHD_BIN(기본 /usr/sbin/sshd), SIMAI_SSH_KEYGEN_BIN(기본 ssh-keygen),
#   SIMAI_SSH_ENV_FILE(기본 /etc/profile.d/simai-env.sh), SIMAI_WSTUNNEL_BIN(기본 wstunnel).
set -euo pipefail

# HF 토큰은 넘겨받은 명령(simai-bootstrap)에만 넘긴다. 이 스크립트의 다른 자식(cat·tr·awk·mkdir·chmod·wc·ssh-keygen·sshd)은
# 받지 않도록 맨 먼저 내보내지 않는 셸 변수로 옮기고 환경에서 지운 뒤, exec 바로 앞에서만 다시 내보낸다(-205 리뷰 R1).
hf_token=${HF_TOKEN-}
hf_token_set=${HF_TOKEN+1}
unset HF_TOKEN HUGGING_FACE_HUB_TOKEN
# 터널 경로 비밀값은 wstunnel 에만(환경 변수로) 넘긴다. 넘겨받은 명령을 포함해 다른 어느 자식에도 넘기지 않고 출력하지 않는다
tunnel_prefix=${SIMAI_TUNNEL_PREFIX-}
unset SIMAI_TUNNEL_PREFIX

SSH_DIR=${SIMAI_SSH_DIR:-/root/.ssh}
CONF_DIR=${SIMAI_SSHD_CONFIG_DIR:-/etc/ssh/sshd_config.d}
SSHD=${SIMAI_SSHD_BIN:-/usr/sbin/sshd}
KEYGEN=${SIMAI_SSH_KEYGEN_BIN:-ssh-keygen}
ENV_FILE=${SIMAI_SSH_ENV_FILE:-/etc/profile.d/simai-env.sh}
WSTUNNEL=${SIMAI_WSTUNNEL_BIN:-wstunnel}
TUNNEL_PORT=${SIMAI_TUNNEL_PORT:-8080}

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

  # SSH 세션에는 컨테이너 ENV 가 없다(sshd 가 새 환경으로 셸을 띄운다). 3차 세션의 /proc/1/environ 수동 절차를 대신해
  # 정해 둔 이름(SIMAI_*·CUDA_*·NVIDIA_*·UV_*·RUNPOD_*·PYTHON*·PATH·LD_LIBRARY_PATH)만 적고, 비밀로 보이는 이름은 뺀다.
  # HF 토큰·터널 비밀값은 위에서 이미 환경에서 지웠다. 값은 작은따옴표로 감싸 sh·bash 어느 쪽이 읽어도 그대로 들어간다.
  sq="'" sq_esc="'\\''"
  if mkdir -p "${ENV_FILE%/*}" && {
       echo "# simai-start 가 sshd 를 띄울 때 적는다(S15P31A101-286). 비밀 값(HF 토큰·API 키·터널 비밀값)은 넣지 않는다"
       for name in $(compgen -e); do
         [[ $name =~ ^(PATH|LD_LIBRARY_PATH|(SIMAI|CUDA|NVIDIA|UV|RUNPOD)_[A-Za-z0-9_]*|PYTHON[A-Za-z0-9_]*)$ ]] || continue
         [[ $name =~ TOKEN|SECRET|PASSW|API_KEY|PRIVATE|CREDENTIAL|^SIMAI_TUNNEL_PREFIX$ ]] && continue
         value=${!name}
         printf "export %s='%s'\n" "$name" "${value//$sq/$sq_esc}"
       done
     } > "$ENV_FILE"; then
    chmod 644 "$ENV_FILE"
  else
    echo "simai-start: $ENV_FILE 을 쓸 수 없다(SSH 세션에 컨테이너 환경 변수가 없다)" >&2
  fi

  # 443 예비 터널. 프록시 주소는 공개이므로 경로 비밀값이 없거나 약하면 띄우지 않는다(명세 3.12: 토큰 인증 필수).
  # 비밀값은 명령줄(ps 에 보임)이 아니라 wstunnel 의 환경 변수로만 넘기고, wstunnel 로그는 규칙(비밀값 포함)을 찍으므로 끄고 버린다.
  if [ -z "$tunnel_prefix" ]; then
    echo "simai-start: SIMAI_TUNNEL_PREFIX 없음 → 443 예비 터널(wstunnel)을 띄우지 않는다" >&2
  elif ! [[ $tunnel_prefix =~ ^[A-Za-z0-9_-]{32,128}$ ]]; then
    echo "simai-start: SIMAI_TUNNEL_PREFIX 가 약하거나 형식이 틀려 터널을 띄우지 않는다(영문·숫자·_- 32~128자)" >&2
  elif ! [[ $TUNNEL_PORT =~ ^[1-9][0-9]{0,4}$ ]] || (( 10#$TUNNEL_PORT > 65535 )); then
    echo "simai-start: SIMAI_TUNNEL_PORT=$TUNNEL_PORT 가 포트 번호가 아니라 터널을 띄우지 않는다" >&2
  elif ! command -v "$WSTUNNEL" >/dev/null; then
    echo "simai-start: $WSTUNNEL 이 없어 터널을 띄우지 않는다" >&2
  else
    # 허용 대상(S15P31A101-288): 22 는 늘, SIMAI_TUNNEL_ALLOW 의 포트는 127.0.0.1 로만 더한다(같은 포트는 한 번)
    restrict=(--restrict-to 127.0.0.1:22 --restrict-to localhost:22)
    targets="127.0.0.1:22"
    if [ -n "${SIMAI_TUNNEL_ALLOW:-}" ]; then
      extra=() bad=""
      IFS=',' read -ra items <<<"$SIMAI_TUNNEL_ALLOW"
      for p in "${items[@]}"; do
        p=${p//[[:space:]]/}
        [ -z "$p" ] && continue
        if ! [[ $p =~ ^[1-9][0-9]{0,4}$ ]] || (( 10#$p > 65535 )) || [ "$p" = "$TUNNEL_PORT" ]; then bad=1; break; fi
        [ "$p" = 22 ] && continue
        [[ " ${extra[*]} " == *" $p "* ]] || extra+=("$p")
      done
      if [ -n "$bad" ] || (( ${#extra[@]} > 16 )); then
        echo "simai-start: SIMAI_TUNNEL_ALLOW 가 포트 목록이 아니다(쉼표로 나눈 1~65535, 터널 포트 제외, 16개 이하) → 하나도 더하지 않고 22 만 연다" >&2
      else
        for p in "${extra[@]}"; do restrict+=(--restrict-to "127.0.0.1:$p"); targets+=",127.0.0.1:$p"; done
      fi
    fi
    WSTUNNEL_RESTRICT_HTTP_UPGRADE_PATH_PREFIX="$tunnel_prefix" "$WSTUNNEL" server --log-lvl OFF \
      "${restrict[@]}" "ws://0.0.0.0:$TUNNEL_PORT" </dev/null >/dev/null 2>&1 &
    echo "simai-start: wstunnel 시작 (ws://0.0.0.0:$TUNNEL_PORT → $targets 만, 경로 비밀값 인증, 로그 끔)" >&2
  fi
elif [ -n "$tunnel_prefix" ]; then
  echo "simai-start: sshd 를 띄우지 않아 443 예비 터널(wstunnel)도 띄우지 않는다" >&2
fi

if [ "$#" -eq 0 ]; then
  set -- bash
fi
if [ -n "$hf_token_set" ]; then export HF_TOKEN="$hf_token"; fi
exec "$@"
