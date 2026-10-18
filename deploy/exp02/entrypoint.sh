#!/usr/bin/env bash
# EXP-02 이미지 진입점: Network Volume 의 체크포인트가 있는지 먼저 보고, 없으면 무엇을 해야 하는지 알리고 멈춘다.
#   simai-exp02 <명령> [인자...]     확인 뒤 명령 실행 (예: python /workspace/simai/setup/hmr/run_image.py ...)
#   SIMAI_EXP02_SKIP_WEIGHTS_CHECK=1  가중치 없이 환경만 볼 때 (import 점검 등)
# 종료 코드 2: Network Volume·체크포인트 없음
set -euo pipefail

ROOT=${SIMAI_ROOT:-/workspace/simai}
if [ "${SIMAI_EXP02_SKIP_WEIGHTS_CHECK:-0}" != "1" ]; then
  if [ ! -d "$ROOT" ]; then
    echo "simai-exp02: $ROOT 가 없다. Network Volume 을 /workspace 에 붙인 Pod 에서 실행한다 (deploy/README.md)" >&2
    exit 2
  fi
  missing=()
  for f in weights/sam-3d-body-dinov3/model.ckpt weights/sam-3d-body-dinov3-fastcfg/model_config.yaml weights/yolo/yolo26x-pose.pt; do
    [ -e "$ROOT/$f" ] || missing+=("$ROOT/$f")
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    echo "simai-exp02: 체크포인트 없음: ${missing[*]}" >&2
    echo "  가중치는 이미지에 넣지 않는다. 볼륨에서 bash ai/experiments/hmr/setup_runpod.sh weights 로 받는다" >&2
    echo "  (HF 토큰은 /workspace/.simai-private/hf.env 에만 둔다)" >&2
    exit 2
  fi
fi

if [ "$#" -eq 0 ]; then
  set -- bash
fi
exec "$@"
