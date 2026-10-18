#!/usr/bin/env bash
# Tier 0 이미지 점검 (GPU 없이, 노트북·CI·Pod 어디서나). 이미지를 레지스트리에 올리기 전에 돌린다.
#   bash deploy/scripts/check_image.sh simai/tier0:local
# 확인: 비밀·가중치·엔진·SSH 호스트 키가 이미지에 없음, 패키지 import, 진입점 도움말,
#       볼륨 없음·가중치 없음·해시 불일치·GPU 없음이 각각 명확한 코드와 메시지로 끝남, GPU 별 엔진 경로가 다름,
#       PUBLIC_KEY 를 주면 키 인증만 받는 sshd 가 뜸.
# 컨테이너 볼륨은 이름 있는 Docker 볼륨을 만들어 쓰고 끝나면 지운다(호스트 경로 변환 문제를 피한다).
set -uo pipefail
export MSYS_NO_PATHCONV=1

IMG=${1:?사용법: check_image.sh <이미지>}
VOL=simai-check-$$
FAILS=0
pass(){ echo "PASS  $*"; }
fail(){ echo "FAIL  $*"; FAILS=$((FAILS + 1)); }
# shellcheck disable=SC2317  # trap 으로 부른다
cleanup(){ docker volume rm -f "$VOL" >/dev/null 2>&1; }
trap cleanup EXIT

# 1) 이미지 층 이력·환경 변수에 비밀이 없다
SECRET_RE='hf_[A-Za-z0-9]{20,}|HF_TOKEN=[^ ]|HUGGING_FACE_HUB_TOKEN=|RUNPOD_API_KEY=|PUBLIC_KEY=ssh-|BEGIN [A-Z ]*PRIVATE KEY'
if docker history --no-trunc --format '{{.CreatedBy}}' "$IMG" | grep -Eq "$SECRET_RE"; then
  fail "docker history 에 비밀로 보이는 값이 있다"
else
  pass "docker history 에 비밀 값 없음"
fi
if docker image inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$IMG" | grep -Eiq '^(HF_TOKEN|HUGGING_FACE_HUB_TOKEN|RUNPOD_API_KEY|PUBLIC_KEY|.*SECRET.*|.*PASSWORD.*)='; then
  fail "이미지 환경 변수에 비밀 이름이 있다"
else
  pass "이미지 환경 변수에 비밀 이름 없음"
fi

# 2) 파일 시스템에 가중치·엔진·비밀 파일·SSH 호스트 키가 없다
#    가중치 확장자는 256KB 넘는 것만 본다(패키지가 싣는 .pth 경로 설정 파일·onnx 시험용 작은 모델은 가중치가 아니다).
#    .pem·.key 는 개인 키가 든 것만 본다(certifi 인증서 묶음 같은 공개 인증서는 괜찮다).
FOUND=$(docker run --rm --entrypoint /bin/sh "$IMG" -c '
  find / -xdev \( -path /proc -o -path /sys -o -path /dev \) -prune -o -type f -size +256k \( \
    -name "*.pt" -o -name "*.pth" -o -name "*.engine" -o -name "*.onnx" -o -name "*.safetensors" -o -name "*.ckpt" \
    -o -name "*.trt" -o -name "*.bin" -path "*weights*" \) -print 2>/dev/null
  find / -xdev \( -path /proc -o -path /sys -o -path /dev \) -prune -o -type f \( \
    -name "hf.env" -o -name ".env" -o -name "*.env" -o -name "id_rsa*" -o -name "id_ed25519*" -o -name "authorized_keys" \
    -o -path "/etc/ssh/ssh_host_*" -o -path "*/.simai-private/*" \) -print 2>/dev/null
  find / -xdev \( -path /proc -o -path /sys -o -path /dev \) -prune -o -type f \( -name "*.pem" -o -name "*.key" \) \
    -exec grep -l "PRIVATE KEY" {} + 2>/dev/null
  [ -e /workspace ] && echo "/workspace (이미지 안에 볼륨 경로가 있다)"
  [ -d /opt/simai/ai/src ] && echo "/opt/simai/ai/src (소스 사본이 남았다)"
  true')
if [ -z "$FOUND" ]; then
  pass "가중치·엔진·비밀 파일·SSH 호스트 키 없음"
else
  fail "이미지에 있으면 안 되는 파일:"; while IFS= read -r line; do echo "      $line"; done <<<"$FOUND"
fi

# 3) import·버전·잠금
if OUT=$(docker run --rm --entrypoint python "$IMG" -c '
import importlib.metadata as m, sys
import simai_ai, services.tier0, simai_ai.detection.engine_cache, cv2, ultralytics, tensorrt, onnx
print(sys.version.split()[0], "ultralytics", m.version("ultralytics"), "torch", m.version("torch"), "tensorrt", m.version("tensorrt-cu13"))' 2>&1); then
  pass "import simai_ai·services.tier0·engine_cache·cv2·ultralytics·tensorrt·onnx ($OUT)"
else
  fail "import 실패: $OUT"
fi
if OUT=$(docker run --rm "$IMG" --help 2>&1) && grep -q "Tier 0" <<<"$OUT"; then
  pass "simai-tier0 --help"
else
  fail "simai-tier0 --help: $OUT"
fi

# 4) 볼륨·가중치·해시·GPU 문제는 Tier 0 를 띄우지 않고 명확한 코드로 끝난다
expect(){  # 이름 기대코드 기대문구 docker-run-인자...
  local name=$1 code=$2 text=$3; shift 3
  local out rc
  out=$(docker run --rm "$@" 2>&1); rc=$?
  if [ "$rc" -eq "$code" ] && grep -q "$text" <<<"$out" && ! grep -q "아직 조립하지 않았다" <<<"$out"; then
    pass "$name → 종료 코드 $rc"
  else
    fail "$name: 종료 코드 $rc (기대 $code, 문구 '$text')"; tail -5 <<<"$out" | sed 's/^/      /'
  fi
}
expect "볼륨 없음" 2 "Network Volume" "$IMG"
docker volume create "$VOL" >/dev/null
docker run --rm -v "$VOL:/workspace" --entrypoint /bin/sh "$IMG" -c 'mkdir -p /workspace/simai/weights/yolo' >/dev/null
expect "가중치 없음" 2 "가중치 없음" -v "$VOL:/workspace" "$IMG"
docker run --rm -v "$VOL:/workspace" --entrypoint /bin/sh "$IMG" \
  -c 'printf "not-real-weights" > /workspace/simai/weights/yolo/yolo26x-pose.pt' >/dev/null
expect "가중치 해시 불일치" 2 "해시 불일치" -v "$VOL:/workspace" \
  -e SIMAI_TIER0_WEIGHTS_SHA256=0000000000000000000000000000000000000000000000000000000000000000 "$IMG"
expect "GPU 없음(가중치는 있음)" 3 "CUDA GPU" -v "$VOL:/workspace" "$IMG"

# 5) GPU 아키텍처마다 엔진 경로가 다르다 (같은 볼륨에서 A40 캐시를 H100 이 고르지 않는다)
plan(){ docker run --rm -v "$VOL:/workspace" "$IMG" python -m simai_ai.detection.engine_cache plan \
  --weights /workspace/simai/weights/yolo/yolo26x-pose.pt --cache-root /workspace/simai/engines \
  --gpu-arch "$1" --tensorrt 11.4.0.106 --ultralytics 8.4.178 2>&1; }
P90=$(plan sm_90); P86=$(plan sm_86)
if [ "$P90" != "$P86" ] && grep -q "/sm_90/trt" <<<"$P90" && grep -q "/sm_86/trt" <<<"$P86"; then
  pass "엔진 경로 sm_90 ≠ sm_86 (${P90#*	})"
else
  fail "엔진 경로가 GPU 별로 나뉘지 않는다: $P90 / $P86"
fi

# 6) SSH: PUBLIC_KEY 를 주면 키 인증만 받는 sshd 가 뜬다(호스트 키는 이때 만든다)
OUT=$(docker run --rm -e "PUBLIC_KEY=ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEZha2VLZXlGb3JDaGVja09ubHkwMDAwMDAwMDAwMDA check" "$IMG"   bash -c 'pgrep -x sshd >/dev/null && echo running; sshd -T 2>/dev/null | grep -Ei "^(passwordauthentication|kbdinteractiveauthentication|permitrootlogin) "' 2>/dev/null)
if grep -q running <<<"$OUT" && grep -qi "^passwordauthentication no" <<<"$OUT"    && grep -qi "^kbdinteractiveauthentication no" <<<"$OUT" && grep -qi "^permitrootlogin without-password" <<<"$OUT"; then
  pass "SSH: sshd 기동, 비밀번호·키보드 인증 끔"
else
  fail "SSH 설정: $(tr '
' ' ' <<<"$OUT")"
fi

echo
if [ "$FAILS" -eq 0 ]; then echo "check_image: 모두 통과 ($IMG)"; else echo "check_image: 실패 $FAILS 건 ($IMG)"; fi
exit "$FAILS"
