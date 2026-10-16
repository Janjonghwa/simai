#!/usr/bin/env bash
# EXP-02 최소 검증 큐: 설치 직후 GPU 를 짧게 쓰고 끝낸다. 설치 스크립트(setup_hmr.sh)가 돌고 있으면 끝날 때까지 기다린다.
#   nohup bash setup/hmr/queue_min.sh > logs/exp02_min.log 2>&1 &
# 순서: 가중치(YOLO26x-Pose·Fast config) → 검출 시간(H6) → 스모크(H0) → 14장 공식 경로(H1) → 라이브 서버 10분(테스트 영상으로 지연 측정 후 자동 종료)
set -u
cd "${SIMAI_ROOT:-/workspace/simai}"
PY=envs/fast_sam3db/bin/python; S=setup/hmr
log(){ echo "[$(date -u +%H:%M:%S)] $*"; }
while pgrep -f "setup/setup_hm[r].sh" >/dev/null; do sleep 15; done
grep -q "^\[.*\] done" logs/setup_hmr.log || { log "설치 실패: logs/setup_hmr.log 확인"; exit 1; }
log "weights"; bash setup/setup_hmr_new.sh weights 2>&1 | grep -E "^\[|rror" ; ls -l weights/yolo/
log "H6 det"; $PY $S/det_bench.py --run-id H6_det_yolo26x --half 2>&1 | grep -E "^\[H6|rror"
log "H0 smoke"; $PY $S/run_image.py --run-id H0_smoke --images datasets/hmr_poc14/t1_front_taichi.jpg --mode official --preset opt 2>&1 | grep -E "^\[H0|rror"
log "H1 official 14"; $PY $S/run_image.py --run-id H1_official_opt --images datasets/hmr_poc14 --mode official --preset opt 2>&1 | grep -E "^\[H1|rror"
log "live server (10 min)"; timeout 600 $PY $S/live_server.py --preset opt --run-id L4_live_jp > logs/live_server.log 2>&1
log "ALL DONE"
