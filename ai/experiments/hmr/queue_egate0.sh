#!/usr/bin/env bash
# EXP-02 E-GATE-0 남은 Run(H2~H5)과 카메라 라이브를 한 번에 돌리는 다음 GPU 세션 큐. GPU 를 혼자 쓰도록 하나씩 실행한다.
#   nohup bash setup/hmr/queue_egate0.sh > logs/exp02_egate0.log 2>&1 &
# 앞선 최소 큐(queue_min.sh: 가중치·H6·H0·H1)가 끝난 볼륨을 전제로 한다. 예상: 벤치마크 약 25~35분 + 라이브 15분.
# 마지막 단계는 opt_compile 라이브 서버(15분 뒤 자동 종료)다. 노트북에서 SSH 터널을 열고 live_client.py --source rtsp 로 C110 을 확인한다.
set -u
cd "${SIMAI_ROOT:-/workspace/simai}"
PY=envs/fast_sam3db/bin/python; S=setup/hmr
run(){ echo "[$(date -u +%H:%M:%S)] START $*"; "$@" 2>&1 | grep -E "^\[(H|L)[0-9]|^\[live\]|RESULT|done|Error|error" ; echo "[$(date -u +%H:%M:%S)] END"; }
run $PY $S/benchmark.py --run-id H2_bench_opt --preset opt --modes split split_pose --e2e
run $PY $S/benchmark.py --run-id H3_bench_off --preset off --e2e
run $PY $S/benchmark.py --run-id H4_bench_opt_compile --preset opt_compile --modes split_pose --e2e
run $PY $S/run_image.py --run-id H5_official_off --images datasets/hmr_poc14 --mode official --preset off
echo "[$(date -u +%H:%M:%S)] BENCH DONE — 라이브 서버(opt_compile) 시작, 준비되면 [live] ready"
timeout 900 $PY $S/live_server.py --preset opt_compile --run-id L7_live_c110_compile > logs/live_server.log 2>&1
echo "[$(date -u +%H:%M:%S)] ALL DONE"
