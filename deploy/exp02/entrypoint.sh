#!/usr/bin/env bash
# EXP-02 이미지의 실행 도우미(simai-exp02). simai-bootstrap 이 exp02 묶음 가중치를 받고 확인한 뒤 부른다:
#   simai-bootstrap run simai-exp02 python <저장소 사본>/ai/experiments/hmr/run_image.py ...
#   SIMAI_EXP02_SKIP_WEIGHTS_CHECK=1 simai-exp02 <명령>   가중치 없이 환경만 볼 때 (import 점검 등)
# 하는 일: Fast 저장소 config(FP16_TYPE float16) + 같은 가중치 링크 = weights/sam-3d-body-dinov3-fastcfg 를 만들고(setup_runpod.sh weights 와 같은 구성),
#          체크포인트가 다 있는지 본 뒤 명령을 exec 한다. 가중치를 받지는 않는다(받기·sha256 은 simai-bootstrap).
# 종료 코드 2: 저장 위치·체크포인트 없음
set -euo pipefail

ROOT=${SIMAI_ROOT:-/workspace/simai}
if [ "${SIMAI_EXP02_SKIP_WEIGHTS_CHECK:-0}" != "1" ]; then
  W=$ROOT/weights
  missing=()
  for f in sam-3d-body-dinov3/model.ckpt sam-3d-body-dinov3/assets/mhr_model.pt yolo/yolo26x-pose.pt \
           moge/moge-2-vits-normal/model.pt moge/moge-2-vitl-normal/model.pt; do
    [ -f "$W/$f" ] || missing+=("$W/$f")
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    echo "simai-exp02: 체크포인트 없음: ${missing[*]}" >&2
    echo "  가중치는 이미지에 넣지 않는다. simai-bootstrap run simai-exp02 ... 또는 simai-bootstrap queue 로 시작하면 받고 sha256 을 확인한다" >&2
    exit 2
  fi
  F=$W/sam-3d-body-dinov3-fastcfg
  mkdir -p "$F"
  ln -sfn "$W/sam-3d-body-dinov3/model.ckpt" "$F/model.ckpt"
  ln -sfn "$W/sam-3d-body-dinov3/assets" "$F/assets"
  cp "${SIMAI_FS3DB_REPO:-/opt/exp02/Fast-SAM-3D-Body}/checkpoints/sam-3d-body-dinov3/model_config.yaml" "$F/model_config.yaml"
fi

if [ "$#" -eq 0 ]; then
  set -- bash
fi
exec "$@"
