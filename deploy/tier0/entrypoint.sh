#!/usr/bin/env bash
# Tier 0 이미지 진입점. 명세 18.2 시작 순서의 "Pod 기동(TensorRT 엔진은 볼륨 캐시에서 로드)" 부분이다.
#   simai-tier0 [Tier 0 인자...]   가중치·엔진 준비 → python -m services.tier0 (엔진 경로는 SIMAI_TIER0_ENGINE 으로 넘긴다)
#   simai-tier0 --help             준비 없이 Tier 0 도움말
#   simai-tier0 <명령> [인자...]    첫 인자가 옵션이 아니면 그 명령을 그대로 실행 (점검·셸·엔진 계획)
# 종료 코드: 2 Network Volume·가중치 문제, 3 GPU·TensorRT 없음, 4 엔진 캐시 위치 문제, 5 엔진 빌드 실패 (engine_cache 와 같음)
set -euo pipefail

case "${1:-}" in
  -h|--help) exec python -m services.tier0 --help ;;
  ""|-*) ;;
  *) exec "$@" ;;
esac

ROOT=${SIMAI_ROOT:-/workspace/simai}
WEIGHTS=${SIMAI_TIER0_WEIGHTS:-$ROOT/weights/yolo/yolo26x-pose.pt}
CACHE=${SIMAI_ENGINE_CACHE:-$ROOT/engines}

if [ ! -d "$ROOT" ]; then
  echo "simai-tier0: $ROOT 가 없다. Network Volume 을 /workspace 에 붙인 Pod 에서 실행한다 (deploy/README.md)" >&2
  exit 2
fi

ARGS=(ensure --weights "$WEIGHTS" --cache-root "$CACHE"
      --mode "${SIMAI_TIER0_MODE:-e2e}" --imgsz "${SIMAI_TIER0_IMGSZ:-1280}"
      --max-batch "${SIMAI_TIER0_MAX_BATCH:-4}" --camera "${SIMAI_TIER0_CAMERA:-2304x1296}")
if [ -n "${SIMAI_TIER0_WEIGHTS_SHA256:-}" ]; then
  ARGS+=(--expect-sha256 "$SIMAI_TIER0_WEIGHTS_SHA256")
fi

# 엔진 준비는 Tier 0 와 다른 프로세스에서 한다(빌더가 남긴 장치 메모리가 Tier 0 지연을 늘리지 않게).
set +e
OUT=$(python -m simai_ai.detection.engine_cache "${ARGS[@]}")
CODE=$?
set -e
if [ "$CODE" -ne 0 ]; then
  echo "simai-tier0: 엔진 준비 실패(종료 코드 $CODE). Tier 0 를 시작하지 않는다" >&2
  exit "$CODE"
fi
SIMAI_TIER0_ENGINE=$(printf '%s\n' "$OUT" | tail -n 1)
export SIMAI_TIER0_ENGINE
echo "simai-tier0: 엔진 $SIMAI_TIER0_ENGINE" >&2

exec python -m services.tier0 "$@"
