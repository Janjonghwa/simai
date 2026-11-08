#!/usr/bin/env python3
"""simai-bootstrap: 어느 GPU·지역·사이트(RunPod·Vast.ai·엘리스 GPU VM)에서든 같은 순서로 이미지를 준비한다 (S15P31A101-205·-207·-246·-250·-382·-414).

    simai-bootstrap tier0 [Tier 0 인자...]      준비 → simai-tier0 (엔진 준비 engine_cache ensure → python -m services.tier0). 옵션으로 시작해도(--config ...) tier0
                                                SIMAI_SERVICES 에 vllm·worker·avatar·ppe 가 있으면 감시 프로세스로 Tier 0 → vLLM 서버 → 분석 워커·아바타·PPE (아래)
    simai-bootstrap                             (인자 없음) 이 사용법만 보이고 64 로 끝난다(아무것도 띄우지 않음. 이미지 기본 CMD 는 tier0 를 명시한다)
    simai-bootstrap lobby [로비 인자...]        로비 LIVE 통합 기동: endpoint.json·tier0-live.json·pod.jsonl 준비 → 단일 Supervisor 로
                                                Tier 0·vLLM·worker·avatar 를 한 번에 띄우고 Tier 0 종료 코드 그대로 끝난다. 프로세스는 --services(기본 넷 다)만 따르고
                                                SIMAI_SERVICES 는 보지 않는다. 워커 ID 는 --worker-id → SIMAI_WORKER_ID → 토큰 파일 이름(worker-s10.token → worker-s10)
                                                모든 프로세스 환경에 SIMAI_TIER0_MARKER=<work>/state/tier0.live·SIMAI_STATE_DIR=<work>/state 를 넣는다
                                                (워커의 site_setup 배경 묶음·활성 보정·scene_regenerate 가 Tier 0 와 같은 상태 폴더를 본다, S15P31A101-473)
    simai-bootstrap queue [gpu_session1 run 인자...]   준비 → GPU 1차 세션 큐 (저장소 사본 $SIMAI_SESSION_REPO, 기본 $SIMAI_ROOT/setup/session1/repo)
    simai-bootstrap check [--preflight|--sets ...]     준비만 하고 결과 JSON 을 출력한다 (--preflight / preflight 는 가중치 묶음·GPU 예산·런타임 일괄 점검)
    simai-bootstrap preflight [check 인자...]   tier0,avatar,static,vlm 가중치·GPU 예산(lobby 기본 넷)·texrecon·open3d·ultralytics 일괄 점검(하나라도 실패하면 비-0 종료).
                                                py-spy 부착 가능 여부는 안내만 한다(RunPod 는 SYS_PTRACE 가 없어 불가 → 내장 계측 -383)
    simai-bootstrap run <명령...>               준비 → 그 명령 (실험 이미지: simai-bootstrap run simai-exp02 python ...)
    simai-bootstrap scene [--] <명령...>        장면 준비 모드(일회성, 통합 이미지의 MapAnything): Tier 0·GPU 확인 → static 가중치 → 재확인 → 그 명령을
                                                자식으로 실행하며 Tier 0 시작을 감시(나타나면 장면 작업을 끝내고 75). 명령의 종료 코드로 끝난다(아래 "장면 준비 모드")
    simai-bootstrap after-live [--work 로비 작업 폴더] [--worker-token-file F | --env-file F] [--ec2-base-url URL] [-- 워커 인자...]
                                                관제 뒤 배경 복원(S15P31A101-403): Tier 0 정상 종료 기록을 확인하고 장면 준비 모드로 분석 워커만 띄운다.
                                                워커는 관제가 빈 뒤 사람 지운 배경 묶음을 올려 static_reconstruct 를 등록·처리하고, 그 작업이 끝나면 0 으로 끝난다
    simai-bootstrap --help                      이 설명 + Tier 0 도움말 (준비 없음)
    simai-bootstrap <다른 명령...>              준비 없이 그대로 실행 (sleep infinity, bash, simai-wait-tier0 등)

준비 순서:
  1) 저장 위치: SIMAI_ROOT 가 있으면 그것, 없으면 /workspace 가 마운트 지점(볼륨)이고 쓸 수 있을 때만 /workspace/simai(RunPod 볼륨·Pod 볼륨·docker -v, S15P31A101-311),
     둘 다 아니면 컨테이너 디스크 /srv/simai. 만들고 남은 용량이 하한(SIMAI_MIN_FREE_GB, 기본 20) 이상인지 본다.
  2) GPU: nvidia-smi 의 compute_cap → sm_XX, 메모리를 기록한다. GPU 가 없거나 최소 사양(Ampere 이상 = compute_cap 8.0,
     VRAM 24GB급) 미만이면 시작하지 않는다.
  3) 가중치: 목록 파일(SIMAI_WEIGHTS_MANIFEST, 기본 deploy/weights.json)에서 묶음 항목을 $SIMAI_ROOT/<path> 에 둔다.
     묶음은 tier0 명령이면 tier0, check·queue·run 이면 SIMAI_WEIGHTS_SET(이미지가 정함: Tier 0 tier0, HMR exp02, MapAnything static).
     이미 있고 sha256 이 맞으면 건너뛰고(엘리스 블록 스토리지 재사용), 없으면 받아서 확인한다. 다르면 시작하지 않는다(받은 파일은 버리고,
     있던 파일은 지우지 않는다). HF 는 환경 변수 HF_TOKEN 만 쓰고, 토큰은 다른 호스트로 넘어가는 리다이렉트·출력·기록에 남지 않는다.
     받기는 끊기면 지수 백오프로 다시 시도하고(SIMAI_FETCH_ATTEMPTS, 기본 5회) 옆 임시 파일(<이름>.part)에서 이어받는다(HTTP Range).
     tier0 묶음에 없는 항목(워커·실험 가중치)은 확인한 뒤 검증 스탬프($SIMAI_ROOT/weights/.verified/<이름>.json: 경로·크기·mtime·sha256)를
     남기고, 다음 시작에 크기·mtime 이 같으면 다시 해시하지 않는다(Cosmos3-Nano 31.5GB 재해싱을 피한다). Tier 0 가중치는 매번 해시한다.
  4) 기록 $SIMAI_ROOT/runs/bootstrap/<UTC>-<pid>.json (추가만), 그다음 실행할 명령으로 exec 한다.
     queue 는 엔진 준비를 하지 않는다: 세션 b 항목이 엔진 첫 빌드 시간을 재기 때문이다.

감시 프로세스(SIMAI_SERVICES=tier0,vllm,worker, 통합 이미지 기본. 없거나 'tier0' 이면 위처럼 simai-tier0 로 exec 하는 Tier 0 단독):
  - Tier 0 가중치만 확인한 뒤 simai-tier0 을 새 프로세스 그룹으로 띄운다. Tier 0 표시 파일(state/tier0.live)이 phase=running·
    reserve.state=OK(simai-wait-tier0 와 같은 판정)이고 이번에 띄운 Tier 0 가 기동 뒤에 쓴 것(gpu_priority.written_at ≥ 기동 시각, pid 같음)이면 워커 가중치 묶음(SIMAI_WORKER_WEIGHTS_SET, 기본 vlm)을 받고(실패해도 Tier 0 는 계속),
    vLLM 서버(127.0.0.1:SIMAI_VLLM_PORT, /health 확인) → 분석 워커 순서로 띄운다. GPU VRAM 이 48GB급 미만이면 워커 쪽을 띄우지 않는다(명세 3.12).
  - 분석 워커를 띄우면(run 단계에 들어서면) 뒤에서 현장 세팅(site_setup)의 MapAnything 가중치를 받는다: static 묶음 중 워커가 쓰는 모델 폴더
    (SIMAI_MAPANYTHING_MODEL, 기본 $SIMAI_ROOT/weights/map-anything) 항목만, 이미 있으면 건너뛴다(S15P31A101-473). 워커 시작을 막지 않고, 실패하면
    경고만 남긴다(그 상태로 세팅하면 STATIC_MODEL_MISSING. 'simai-bootstrap preflight' 로 확인). 끄려면 SIMAI_WORKER_SETUP_WEIGHTS=0.
  - vLLM·워커가 죽으면 그 프로세스 그룹을 SIGKILL 로 정리하고(vLLM EngineCore 같은 손자 포함), 지수 백오프 뒤 Tier 0 가 준비 상태일 때만
    다시 띄운다. 워커·아바타는 계속 재시도하고 vLLM 은 빠른 실패가 연달아 SIMAI_SUPERVISE_MAX_FAST_FAILURES(기본 5)번이면 포기한다. Tier 0 는 건드리지 않는다.
  - Tier 0 가 끝나면 워커 → vLLM 을 내리고 Tier 0 종료 코드 그대로 끝낸다. SIGTERM·SIGINT 를 받으면 워커 → vLLM → Tier 0 순서로
    그룹에 SIGTERM, SIMAI_STOP_GRACE_S(기본 15)초 안에 안 끝나면 그룹 SIGKILL. docker stop 은 -t 60 이상으로 준다.
  - 출력은 줄마다 [tier0]·[vllm]·[worker] 접두로 표준 오류에, 원문은 $SIMAI_ROOT/logs/<이름>.log 에 덧붙인다.
  - vLLM·워커는 스레드 제한(OMP·MKL·OpenBLAS·NumExpr = SIMAI_SIDECAR_THREADS, 기본 1. torch intra-op 도 OMP 를 따른다)·nice(SIMAI_SIDECAR_NICE, 기본 10)·
    CPU 고정(SIMAI_SIDECAR_CPUS, 기본 auto = 쓸 수 있는 vCPU n = min(affinity 개수, cgroup 할당량 올림)이 8 이상이면 affinity 뒤쪽 n//2 개,
    none·'4-7,10'. 계산 결과는 시작 로그 한 줄), 세 프로세스 모두 다른
    TRITON_CACHE_DIR·TORCHINDUCTOR_CACHE_DIR·CUDA_CACHE_PATH(SIMAI_CACHE_ROOT, 기본 /tmp/simai-cache/<이름>/)로 띄운다.
    vLLM 기본값: --max-model-len SIMAI_WORKER_VLLM_MAX_MODEL_LEN(32768, SIMAI_VLLM_EXTRA_ARGS 에 있으면 그 값),
    --quantization SIMAI_VLLM_QUANTIZATION(fp8, none·빈 값이면 비활성, SIMAI_VLLM_EXTRA_ARGS 에 --quantization/-q 가 있으면 그 값, S15P31A101-525),
    --gpu-memory-utilization SIMAI_WORKER_VLLM_GPU_MEMORY_UTILIZATION(0.30, H100 80 GiB 기준 24 GiB = FP8 가중치 10.05 GiB + CUDA graph·활성화 여유 + KV 캐시),
    환경 VLLM_USE_DEEP_GEMM=0·VLLM_USE_FLASHINFER_SAMPLER=0(환경에 주면 그 값)·HF_HUB_OFFLINE=1.
    vLLM 은 표시 파일이 phase=running·reserve OK 일 때만 띄우므로 Tier 0 의 TensorRT 엔진 빌드·적재와 GPU 선확보가 끝난 뒤다(진입점이 엔진을 준비한 뒤에 서비스가 표시 파일을 쓴다). 워커 토큰 파일(SIMAI_WORKER_ENV_FILE, 없으면
    $SIMAI_ROOT/../.simai-private/worker.env 가 있을 때)은 simai-wait-tier0 --env-file 로 워커 환경에만 넣는다.
    워커 인자는 SIMAI_WORKER_ARGS(예: --config <워커 설정>: 받을 작업 유형 VLM·hmr_refine 등의 처리기)를 덧붙인다.
  - ppe(명시 선택, lobby 기본에 없음, 명세 5.5, S15P31A101-507): Tier 0 준비 뒤 ppe 가중치 묶음(SIMAI_PPE_WEIGHTS_SET, 기본 ppe)을 워커 가중치와
    함께 받고 python -m services.ppe 를 띄운다. Tier 0 환경에 SIMAI_AVATAR_SHARE=1(입력 공유 파일)·SIMAI_PPE_RELAY=1(PPE outbox 를 Tier 0 I1 로 대신
    송신)을, PPE 환경에 SIMAI_PPE_WEIGHTS·SIMAI_PPE_WEIGHTS_SHA256·SIMAI_PPE_GPU_MEMORY_FRACTION(기본 0.05, 분석 워커 예산 밖)을 넣는다.
    GPU 예산은 Tier 0 + 분석 워커 + PPE ≤ 0.95 를 보고, 넘으면 64 로 시작하지 않는다(명세 3.2). PPE 는 I1 연결·워커 토큰을 쓰지 않는다.

장면 준비 모드(S15P31A101-250, simai-bootstrap scene): MapAnything 같은 준비 단계 일회성 작업을 LIVE 서비스(Tier 0·vLLM·워커)와 겹치지 않게 돌린다.
  - 가중치 묶음은 SIMAI_WEIGHTS_SET 과 상관없이 static(MapAnything 기본·apache)이다. SIMAI_SERVICES(이미지 기본 tier0,vllm,worker)는 tier0 명령만 쓰므로 보지 않는다.
  - Tier 0 와 상호 배제(리뷰 R1, Tier 0 우선): tier0 명령은 GPU 를 잡기 전에 $SIMAI_ROOT/locks/tier0.lock 공유 잠금을 쥐고(exec 를 지나 Tier 0 가
    끝날 때까지), 장면 작업이 돌고 있으면(locks/scene.lock) 그 작업이 멈추기를 SCENE_PREEMPT_WAIT_S(20초)까지 기다리고 남은 그룹을 회수한 뒤
    (최악 약 40초) 시작한다. 회수하지 못하면 경고·기록(75)을 남기고 그대로 시작한다(Tier 0 가용성 우선, 의도된 정책). 장면 모드는
    scene.lock 을 쥐고, 명령을 새 프로세스 그룹 자식으로 돌리며 0.5초마다 tier0.lock·표시 파일을 본다. Tier 0 가 나타나면 그룹 SIGTERM → 10초 → SIGKILL, 75.
    감시 부모가 멈추거나 죽어도: 자식 그룹을 locks/scene.pgid(pgid·시작 표지·PID namespace)에 남기고(자식은 기록이 공개된 뒤에만 시작), Tier 0 시작 경로가 잠금이 풀렸거나 시한이 지나도 그 그룹이
    살아 있으면 직접 SIGTERM → 10초 → SIGKILL 로 회수한다(실패는 기록하고 진행). Linux 에서는 자식에 PR_SET_PDEATHSIG(SIGKILL)도 건다.
  - LIVE 판정(하나라도 맞으면 시작하지 않고 코드 75): Tier 0 시작 잠금이 쥐어짐, Tier 0 표시 파일(SIMAI_TIER0_MARKER, 기본 $SIMAI_ROOT/state/tier0.live)이 30초 안에 갱신됨
    (세션 큐 live_guard 의 marker_max_age_s 와 같은 값. Tier 0 는 5초마다 쓴다), 표시 파일을 확인하지 못함(모름), 가중치 준비 뒤 GPU 사용 중 메모리가
    1024 MiB 초과(Tier 0 엔진 빌드·REPLAY·vLLM·다른 컨테이너처럼 표시 파일로 안 보이는 사용), 사용 중 메모리를 읽지 못함(모름).
    표시 파일·GPU 메모리는 가중치를 받기 전과 명령 직전에 두 번 본다(받는 동안 Tier 0 가 떴을 수 있다. 받기 전에 GPU 메모리를 못 읽으면
    준비의 GPU 단계가 판정). 거부하면 기록($SIMAI_ROOT/runs/bootstrap/)에 exit 75 를 추가한다.
  - 장면 작업 중에 Tier 0 를 띄우면 Tier 0 는 막히지 않고 장면 작업이 멈춘다. 그래도 장면 준비는 관제 시간 밖에서 Tier 0 보다 먼저 끝낸다(deploy/README).

관제 뒤 배경 복원(after-live, S15P31A101-403): 로비 Supervisor 는 Tier 0 가 끝나면 워커도 내리므로, 세션을 끝낸 뒤 이 명령으로 워커만 다시 띄운다.
  - 로비 작업 폴더(--work, 기본 $SIMAI_ROOT/runs/lobby-live)의 state/ 를 그대로 쓴다: SIMAI_TIER0_MARKER=<work>/state/tier0.live,
    SIMAI_STATE_DIR=<work>/state(사람 지운 배경 묶음·Tier 0 활성 보정·자동 제출 기록).
  - Tier 0 정상 종료 기록(<표시 파일>.stopped, Tier 0 가 모든 종료 단계를 마치고 남김)이 없으면 시작하지 않는다(75): 워커의 접수 문은
    그 기록이 있을 때만 'LIVE 아님'으로 보므로 띄워도 아무것도 하지 않는다. 표시 파일이 남아 있어도(관제 중·비정상 종료) 75.
  - 나머지는 장면 준비 모드와 같다(Tier 0 시작 잠금·GPU 사용 중 메모리 확인, static 가중치, Tier 0 가 시작하면 워커 그룹을 끝내고 75).
    워커는 --once 없이 SIMAI_AUTO_STATIC_EXIT_WHEN_DONE=1 로 돈다: 마지막 LIVE 뒤 SIMAI_AUTO_STATIC_QUIET_S(300초)를 기다려 묶음을 올리고,
    그 작업이 끝나거나 낼 묶음이 없으면 스스로 끝난다(종료 코드 0). torch 상한은 분석 워커 예산 전체(vLLM 이 없으므로).
  - Tier 0 시작 경로(tier0·lobby·감시 프로세스)는 GPU 를 잡기 전에 정상 종료 기록을 지운다(엔진 준비 중을 'LIVE 아님'으로 보지 않게).

HF 토큰: 시작하자마자 HF_TOKEN 을 환경에서 꺼내 이 프로세스 안에만 들고(가중치 받기에만 쓴다), HUGGING_FACE_HUB_TOKEN 은 읽지 않고 지우기만 한다.
그래서 nvidia-smi 조회·도움말·그대로 넘기는 명령·Tier 0·vLLM·워커·큐 등 어떤 자식 프로세스에도 토큰이 상속되지 않는다(S15P31A101-205 리뷰 R1).

자식 환경(S15P31A101-414): 이미지 ai 폴더(SIMAI_AI_DIR, 기본 /opt/simai/ai — services 패키지가 있을 때만)를 PYTHONPATH 끝에 더해 SSH 셸처럼 WORKDIR 밖에서
기동해도 python -m services.* 를 찾는다(cwd 는 바꾸지 않아 상대 경로·저장소 사본이 앞선다). 감시 프로세스는 정한 워커 ID 를 worker·avatar 환경 SIMAI_WORKER_ID 로 넣는다.

종료 코드(simai-tier0·engine_cache 와 같은 규칙): 0 정상, 2 저장 위치·용량·가중치(없음·해시 불일치·받기 실패·목록 오류·묶음 이름 오류),
3 GPU 없음·최소 사양 미만, 64 사용법 오류(run·scene 뒤에 명령 없음, SIMAI_SERVICES 오류), 75 장면 준비·after-live 거부(LIVE 서비스·GPU 사용 중·모름·
정상 종료 기록 없음, sysexits EX_TEMPFAIL),
127 Tier 0 명령을 띄우지 못함.
감시 프로세스 모드는 Tier 0 의 종료 코드를 그대로 돌려준다(신호로 끝났으면 128 + 신호 번호).
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import http.client
import importlib.machinery
import importlib.util
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

EXIT_OK, EXIT_STORAGE, EXIT_GPU, EXIT_USAGE, EXIT_NOT_FOUND = 0, 2, 3, 64, 127
EXIT_BUSY = 75  # 장면 준비 거부: LIVE 서비스가 떠 있거나 GPU 를 다른 프로세스가 쓴다(또는 모름). sysexits EX_TEMPFAIL(나중에 다시)
WORKSPACE = Path("/workspace")
CONTAINER_ROOT = Path("/srv/simai")  # 볼륨이 없을 때. Pod 를 지우면 같이 사라진다(결과는 지우기 전에 scp)
DEFAULT_MANIFEST = Path(__file__).resolve().parents[1] / "weights.json"  # 저장소. 이미지는 SIMAI_WEIGHTS_MANIFEST 로 준다
MIN_CC = (8, 0)  # Ampere 이상
MIN_VRAM_MB = 22_500  # 24GB급(A10·L4 는 nvidia-smi 에서 약 23,000 MiB 로 보인다)
MIN_WORKER_VRAM_MB = 45_000  # 48GB급(A40·L40S 는 약 46,000 MiB). 그보다 작으면 분석 부하를 같은 GPU 에 올리지 않는다(명세 3.12)
TOKEN_ENVS = ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN")  # 자식 환경에서 지울 이름. 읽는 것은 HF_TOKEN 하나
SECRET_ENVS = (*TOKEN_ENVS, "SIMAI_WORKER_TOKEN", "SIMAI_TIER0_TOKEN")
_hf_token: str | None = None  # take_token() 이 환경에서 꺼내 둔 값. _request·_redact 만 쓴다
_extra_secrets: set[str] = set()  # 파일에서 읽은 비밀 토큰(_redact 가 로그·기록에서 가린다)
TIER0_CMD = "simai-tier0"
SET_ENV = "SIMAI_WEIGHTS_SET"  # check·queue·run 이 받을 가중치 묶음(이미지 ENV). 없으면 tier0
SERVICES_ENV = "SIMAI_SERVICES"  # tier0 명령이 띄울 프로세스. 없으면 tier0(단독)
WORKER_SET_ENV = "SIMAI_WORKER_WEIGHTS_SET"  # 감시 모드에서 Tier 0 준비 뒤 받는 묶음. 없으면 vlm
# avatar는 명시 선택, 기본은 꺼짐(lobby 는 기본으로 켠다). ppe 도 명시 선택이고 lobby 기본에도 없다(G9 전, S15P31A101-507)
SERVICES = ("tier0", "vllm", "worker", "avatar", "ppe")
PPE_FRACTION_DEFAULT = 0.05  # 명세 3.2: PPE 는 분석 워커 밖의 별도 몫. Tier 0 + 분석 워커 + PPE ≤ 0.95
LOBBY_SERVICES = "tier0,vllm,worker,avatar"  # lobby·preflight 기본. 아바타는 시연 필수(S15P31A101-414)
IMAGE_AI_DIR = Path("/opt/simai/ai")  # 이미지 WORKDIR: services 패키지(설치하지 않음)가 여기 있다. SIMAI_AI_DIR 로 바꾼다
WORKER_ID_ENV = "SIMAI_WORKER_ID"
WORKER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")  # EC2 MachineTokenService.ID 와 같은 규칙
GENERIC_TOKEN_STEMS = {"worker", "token", "worker-token", "secret"}  # 이름이 ID 가 아닌 흔한 토큰 파일(worker.token·worker.env)
CAP_SYS_PTRACE = 19  # linux/capability.h. py-spy 부착에 필요(RunPod 컨테이너에는 없다)
PREFLIGHT_SETS = ("tier0", "avatar", "static", "vlm")
PREFLIGHT_BINS = ("texrecon",)
PREFLIGHT_MODULES = ("open3d", "ultralytics")
SCENE_SET = "static"  # 장면 준비 모드의 가중치 묶음(MapAnything 기본·apache)
SCENE_MARKER_MAX_AGE_S = 30.0  # 이 안에 갱신된 Tier 0 표시 파일 = LIVE (gpu_session1 live_guard 의 marker_max_age_s 와 같음)
SCENE_POLL_S = 0.5  # 장면 작업 실행 중 Tier 0 시작 확인 주기
SCENE_STOP_GRACE_S = 10.0  # Tier 0 가 시작하면 장면 그룹 SIGTERM 뒤 SIGKILL 까지
SCENE_PREEMPT_WAIT_S = 20.0  # Tier 0 시작 경로가 장면 잠금 해제를 기다리는 상한(감시 주기 + 시한 + 여유). 그 뒤 남은 그룹 회수(TERM 10초 + 확인 10초)까지
#                               최악 약 40초 늦게 시작한다. 지나면 그대로 시작
TIER0_LOCK = ("locks", "tier0.lock")  # Tier 0 시작 의도(공유 잠금, Tier 0 프로세스가 끝날 때까지). flock 은 커널이 프로세스 종료 때 푼다
SCENE_LOCK = ("locks", "scene.lock")  # 장면 작업 실행 중(배타)
SCENE_PGID = ("locks", "scene.pgid")  # 실행 중인 장면 그룹(pgid·시작 표지). 감시 부모가 멈추거나 죽어도 Tier 0 가 이것으로 회수한다
CLEAN_STOP_SUFFIX = ".stopped"  # Tier 0 정상 종료 기록 = <표시 파일>.stopped (simai_ai.gpu.admission.clean_stop_path 와 같은 규칙, S15P31A101-403)
CLEAN_STOP_PHASE = "stopped_clean"
SCENE_MAX_GPU_USED_MB = 1024  # 넘으면 다른 프로세스가 GPU 를 쓴다. 빈 GPU 는 0~수 MiB, 다른 프로세스의 빈 CUDA 문맥은 약 0.5 GiB, Tier 0·vLLM 은 수 GiB 이상
FETCH_ATTEMPTS = 5
RETRY_HTTP = {408, 425, 429, 500, 502, 503, 504}
LOCK_DIR = ("weights", ".locks")  # 항목마다 지우지 않는 잠금 파일(Codex 리뷰 R1)
STAMP_DIR = ("weights", ".verified")  # 모델 폴더 밖(vLLM 이 읽는 weights/cosmos3-nano/ 에 섞이지 않게), Tier 0 state/ 밖
VLLM_HF_OVERRIDES = '{"architectures": ["Cosmos3ReasonerForConditionalGeneration"]}'  # Reasoner 탑만 싣는다(docs/design/unified-env.md)
VLLM_MAX_MODEL_LEN = "32768"  # H100·0.30(FP8 가중치 10.05 GiB)에서 KV 약 13 GiB ≈ 9.4만 토큰(동시 4 × 실사용 ~3k 토큰/요청 충분), H200·0.30 에서는 KV 29.07 GiB = 211,664 토큰(32k 기준 동시 6.46x, 11-06 GPU PoC)
VLLM_GPU_MEMORY_UTILIZATION = "0.30"  # 11-06 GPU PoC FP8(S15P31A101-525): H100 80 GiB 의 0.30 = 24 GiB = 가중치 10.05 GiB + CUDA graph(0.03~0.04 GiB)·활성화 여유 + KV 캐시
VLLM_QUANTIZATION = "fp8"  # 11-06 GPU PoC(S15P31A101-525): FP8 온라인 양자화(가중치 16.79 → 10.05 GiB, 첫 응답 1.73 → 1.57 s). SIMAI_VLLM_QUANTIZATION=none·빈 값이면 비활성
MIN_WORKER_TORCH_FRACTION = 0.10  # H100 80GB 기준 ~8GB: 12차 GPU 세션에서 워커 몫 0.05(~4GB)로 MapAnything(피크 6.4GB)이 OOM 난 문제 재발 방지(S15P31A101-525)
KILL = getattr(signal, "SIGKILL", 9)  # Windows(노트북 시험)에는 SIGKILL 이 없다(Proc.signal 이 kill() 로 바꾼다)
_out_lock = threading.Lock()


class BootstrapError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def take_token() -> None:
    """HF 토큰을 환경에서 꺼내 프로세스 안에만 둔다. 이 뒤의 모든 자식 프로세스는 토큰 없는 환경을 받는다."""
    global _hf_token
    _hf_token = os.environ.pop("HF_TOKEN", None) or None
    for k in TOKEN_ENVS:
        os.environ.pop(k, None)


def child_env() -> dict[str, str]:
    """자식 프로세스 환경: 지금 환경에서 토큰 이름을 뺀 사본(take_token 전에 불려도 새지 않게)."""
    return with_image_pythonpath({k: v for k, v in os.environ.items() if k not in TOKEN_ENVS})


def with_image_pythonpath(env: dict[str, str]) -> dict[str, str]:
    """이미지 ai 폴더(WORKDIR /opt/simai/ai)를 PYTHONPATH 끝에 더한다(S15P31A101-414, 10차 세션).
    SSH 셸에서 다른 폴더(/srv/simai/setup/...)에서 기동하면 `python -m services.tier0` 가 ModuleNotFoundError 였다(services 는 설치하지 않고
    WORKDIR 에서 찾는다). cwd 는 바꾸지 않는다: 상대 경로 인자(--work·--bundle-dir)와 저장소 사본을 cwd·PYTHONPATH 앞쪽에 두는 방식이 그대로 이긴다."""
    ai_dir = env.get("SIMAI_AI_DIR") or str(IMAGE_AI_DIR)
    if not (Path(ai_dir) / "services").is_dir():
        return env
    parts = [p for p in (env.get("PYTHONPATH") or "").split(os.pathsep) if p]
    if ai_dir not in parts:
        env["PYTHONPATH"] = os.pathsep.join([*parts, ai_dir])
    return env


def _file_value(path: Path | None, key: str) -> str | None:
    """KEY=VALUE 파일에서 비밀이 아닌 값 하나(SIMAI_WORKER_ID)를 읽는다. 없거나 못 읽으면 None."""
    if path is None:
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    result = None
    for line in lines:
        k, sep, v = line.strip().partition("=")
        if sep and k.strip().removeprefix("export ").strip() == key:
            result = v.strip().strip("'\"")
    return result


def resolve_worker_id(explicit: str | None, env: dict[str, str], token_file: Path | None = None,
                      env_file: Path | None = None) -> tuple[str | None, str]:
    """워커 ID 를 정한다(S15P31A101-414): --worker-id → SIMAI_WORKER_ID → 토큰·env 파일 안의 SIMAI_WORKER_ID → 토큰 파일 이름
    (worker-s10.token → worker-s10, EC2 ID 규칙에 맞고 worker.token 같은 흔한 이름이 아닐 때). 못 정하면 (None, 이유).
    10차 세션: 토큰 파일만 주고 ID 를 안 넘겨 워커가 기본 worker-01 로 떠 EC2 대여가 401 이었다."""
    if explicit:
        if not WORKER_ID_RE.match(explicit):
            raise BootstrapError(EXIT_USAGE, f"--worker-id {explicit!r} 형식 오류(EC2 규칙: 소문자·숫자·- 1~63자, 첫 글자 소문자·숫자)")
        return explicit, "--worker-id"
    if env.get(WORKER_ID_ENV):
        return env[WORKER_ID_ENV], WORKER_ID_ENV
    for f in (token_file, env_file):
        if (v := _file_value(f, WORKER_ID_ENV)) is not None:
            return v, f"{f.name} 의 {WORKER_ID_ENV}"
    if token_file is not None:
        stem = token_file.name.split(".", 1)[0]
        if WORKER_ID_RE.match(stem) and stem not in GENERIC_TOKEN_STEMS:
            return stem, f"토큰 파일 이름 {token_file.name}"
        return None, f"토큰 파일 이름 {token_file.name} 이 워커 ID 가 아니다"
    return None, "워커 ID·토큰 파일 없음"


def _redact(text: str) -> str:
    for tok in {_hf_token, *_extra_secrets, *(os.environ.get(k) for k in SECRET_ENVS)}:
        if tok:
            text = text.replace(tok, "***")
    return re.sub(r"hf_[A-Za-z0-9]{20,}", "hf_***", text)


def read_secret_file(path: Path, env_key: str) -> str:
    """토큰 파일(단일 토큰 문자열 또는 KEY=VALUE 형식)에서 비밀 값을 읽는다. 명령줄·로그에 노출하지 않는다."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise BootstrapError(EXIT_STORAGE, f"토큰 파일 {path} 를 읽지 못함 ({type(e).__name__})") from e
    token = ""
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "=" in s:
            k, v = s.split("=", 1)
            if k.strip().removeprefix("export ").strip() == env_key:
                token = v.strip().strip("'\"")
                break
        elif not token:
            token = s
    if not token:
        raise BootstrapError(EXIT_STORAGE, f"토큰 파일 {path} 에 {env_key} 값이 없다")
    _extra_secrets.add(token)
    return token


def ensure_worker_env_file(token_file: Path, out_path: Path) -> Path:
    """워커 토큰 파일을 읽어 0600 권한의 KEY=VALUE env-file 로 만든다(simai-wait-tier0 --env-file 전용)."""
    token = read_secret_file(token_file, "SIMAI_WORKER_TOKEN")
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(f"SIMAI_WORKER_TOKEN={token}\n")
    except OSError as e:
        raise BootstrapError(EXIT_STORAGE, f"워커 env 파일 {out_path} 를 쓰지 못함 ({type(e).__name__})") from e
    return out_path


def log(msg: str) -> None:
    with _out_lock:
        print(f"simai-bootstrap: {_redact(msg)}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 저장 위치


def choose_root(env: dict | None = None, workspace: Path | None = None, container: Path | None = None,
                is_mount=None) -> tuple[Path, str]:
    """(SIMAI_ROOT, 고른 이유 env|workspace|container). /workspace 는 마운트 지점(볼륨)이고 쓸 수 있을 때만 고른다(S15P31A101-311)."""
    env = os.environ if env is None else env
    workspace = WORKSPACE if workspace is None else workspace
    container = CONTAINER_ROOT if container is None else container
    is_mount = os.path.ismount if is_mount is None else is_mount
    if env.get("SIMAI_ROOT"):
        return Path(env["SIMAI_ROOT"]), "env"
    if workspace.is_dir() and is_mount(workspace) and os.access(workspace, os.W_OK):
        return workspace / "simai", "workspace"
    return container, "container"


def prepare_storage(root: Path, source: str, min_free_gb: float) -> dict:
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise BootstrapError(EXIT_STORAGE, f"저장 위치 {root} 를 만들 수 없다({source}): {type(e).__name__}: {e}") from e
    free_gb = shutil.disk_usage(root).free / 1e9
    info = {"root": str(root), "source": source, "is_mount": os.path.ismount(root.parent) or os.path.ismount(root),
            "free_gb": round(free_gb, 2), "min_free_gb": min_free_gb}
    if free_gb < min_free_gb:
        raise BootstrapError(EXIT_STORAGE, f"{root} 남은 용량 {free_gb:.1f} GB < 하한 {min_free_gb} GB "
                                           "(볼륨이 없으면 컨테이너 디스크를 100GB 이상으로)")
    return info


# ---------------------------------------------------------------- GPU


def detect_gpu(smi: list[str]) -> dict:
    try:
        r = subprocess.run([*smi, "--query-gpu=name,compute_cap,memory.total,driver_version", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, env=child_env())
    except (OSError, subprocess.SubprocessError) as e:
        raise BootstrapError(EXIT_GPU, f"GPU 없음: nvidia-smi 를 실행하지 못함 ({type(e).__name__}: {e})") from e
    rows = [[v.strip() for v in line.split(",")] for line in r.stdout.strip().splitlines() if line.strip()]
    if r.returncode != 0 or not rows or len(rows[0]) < 4:
        raise BootstrapError(EXIT_GPU, f"GPU 없음: nvidia-smi 종료 {r.returncode}: {(r.stdout + r.stderr).strip()[:200]}")
    name, cc, total, driver = rows[0][:4]
    m = re.fullmatch(r"(\d+)\.(\d+)", cc)
    try:
        total_mb = float(total)
    except ValueError:
        total_mb = None
    if m is None or total_mb is None:
        raise BootstrapError(EXIT_GPU, f"GPU 정보를 읽지 못함(compute_cap {cc!r}, memory.total {total!r}): 사양을 모르면 시작하지 않는다")
    gpu = {"name": name, "compute_cap": cc, "arch": f"sm_{m.group(1)}{m.group(2)}", "memory_total_mb": total_mb,
           "driver": driver, "count": len(rows)}
    if (int(m.group(1)), int(m.group(2))) < MIN_CC:
        raise BootstrapError(EXIT_GPU, f"{name} compute_cap {cc} < 8.0: 최소 사양(Ampere 이상) 미만")
    if total_mb < MIN_VRAM_MB:
        raise BootstrapError(EXIT_GPU, f"{name} VRAM {total_mb:.0f} MiB: 최소 사양(Tier 0 24GB급) 미만")
    return gpu


# ---------------------------------------------------------------- 가중치


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest(path: Path, wanted: str) -> list[dict]:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", wanted):
        raise BootstrapError(EXIT_STORAGE, f"가중치 묶음 이름 {wanted!r} 오류({SET_ENV})")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        entries = [w for w in doc["weights"] if wanted in w.get("sets", [])]
        for w in entries:
            rel = Path(w["path"])
            if rel.is_absolute() or ".." in rel.parts:
                raise ValueError(f"{w['name']}: path 는 SIMAI_ROOT 아래 상대 경로여야 한다")
            if not ("url" in w["source"] or "hf" in w["source"]):
                raise ValueError(f"{w['name']}: source 에 url 또는 hf 가 없다")
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise BootstrapError(EXIT_STORAGE, f"가중치 목록 {path} 오류: {type(e).__name__}: {e}") from e
    if not entries:  # 묶음 이름 오타로 아무것도 받지 않고 시작하는 일을 막는다
        raise BootstrapError(EXIT_STORAGE, f"가중치 목록 {path} 에 묶음 {wanted!r} 항목이 없다({SET_ENV})")
    return entries


def _request(source: dict) -> urllib.request.Request:
    if "url" in source:
        return urllib.request.Request(source["url"])
    hf = source["hf"]
    endpoint = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
    req = urllib.request.Request(f"{endpoint}/{hf['repo']}/resolve/{hf.get('revision', 'main')}/{hf['file']}")
    if _hf_token:
        # unredirected: HF 는 CDN(다른 호스트)으로 리다이렉트한다. 토큰은 처음 요청에만 싣는다
        req.add_unredirected_header("Authorization", f"Bearer {_hf_token}")
    return req


def urlopen(req: urllib.request.Request):  # 시험에서 바꿔 끼운다
    return urllib.request.urlopen(req, timeout=60)  # noqa: S310 - 목록 파일의 https·HF 주소만


def backoff_sleep(seconds: float) -> None:  # 시험에서 바꿔 끼운다
    time.sleep(seconds)


class _Retry(Exception):
    """다시 시도하면 될 수 있는 받기 실패(망 끊김·5xx·429·이어받기 응답 불일치)."""


def _lock_file(f, what: str) -> None:
    """열린 잠금 파일에 배타 잠금을 건다(다른 프로세스가 쥐고 있으면 끝날 때까지 기다린다). 프로세스가 죽으면 풀린다.
    POSIX 는 flock, Windows(노트북 시험)는 msvcrt.locking(첫 바이트). 둘 다 다른 열기(open)끼리 막는다(같은 프로세스의 스레드끼리도)."""
    try:
        import fcntl
    except ImportError:
        fcntl = None
    if fcntl is not None:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log(f"{what}: 다른 프로세스가 받거나 확인하는 중이라 끝날 때까지 기다린다")
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        return
    import msvcrt

    told = False
    while True:
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            if not told:
                log(f"{what}: 다른 프로세스가 받거나 확인하는 중이라 끝날 때까지 기다린다")
                told = True
            time.sleep(0.05)


class weight_lock:
    """가중치 항목 하나의 임계 구역: 기존 파일 재확인 → 받기 → 해시 → 게시(rename) → 스탬프. 잠금 파일은 지우지 않는다
    (지우거나 이름을 바꾸면 기다리던 쪽과 새로 연 쪽의 inode 가 갈라져 둘 다 잠금을 쥔다). 모델 폴더 밖 weights/.locks/ 에 둔다."""

    def __init__(self, root: Path, w: dict) -> None:
        self.path = root.joinpath(*LOCK_DIR, re.sub(r"[^A-Za-z0-9_.-]", "_", w["path"]) + ".lock")
        self.what = w["name"]
        self.f = None

    def __enter__(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.f = open(self.path, "a+b")  # noqa: SIM115 - __exit__ 가 닫는다(닫으면 잠금이 풀린다)
        except OSError as e:
            raise BootstrapError(EXIT_STORAGE, f"{self.what} 잠금 파일 {self.path} 을 열 수 없다: {e}") from e
        _lock_file(self.f, self.what)
        return self

    def __exit__(self, *_exc) -> None:
        self.f.close()


def _download_once(entry: dict, f, have: int):
    """f(추가 모드 임시 파일)에 받는다. have 바이트가 이미 있으면 Range 로 이어받는다. 끝까지 받은 내용의 sha256 객체."""
    h = hashlib.sha256()
    if have:
        f.seek(0)
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    req = _request(entry["source"])
    if have:
        req.add_header("Range", f"bytes={have}-")  # 리다이렉트에도 실린다(Authorization 과 달리 일반 헤더)
    try:
        resp = urlopen(req)
    except urllib.error.HTTPError as e:
        if e.code == 416:  # 이어받을 범위가 없다(임시 파일이 원본보다 길거나 원본이 바뀜): 처음부터
            f.seek(0)
            f.truncate()
            raise _Retry("HTTP 416(이어받기 범위 없음), 처음부터 다시") from e
        if e.code in RETRY_HTTP:
            raise _Retry(f"HTTP {e.code}") from e
        raise
    with resp:
        status = getattr(resp, "status", None)
        if not (have and status == 206 and str(resp.headers.get("Content-Range", "")).startswith(f"bytes {have}-")):
            if have:  # 원본이 Range 를 무시했다(200) 또는 응답 범위가 다르다: 이어붙이면 파일이 깨지므로 처음부터
                log(f"{entry['name']} 원본이 이어받기를 받지 않아(상태 {status}) 처음부터 받는다")
            f.seek(0)
            f.truncate()
            h = hashlib.sha256()
        for chunk in iter(lambda: resp.read(1 << 20), b""):
            h.update(chunk)
            f.write(chunk)
        f.flush()
        os.fsync(f.fileno())
    return h


def fetch(entry: dict, target: Path) -> str:
    """target 옆 임시 파일(<이름>.part)에 받고 해시를 맞춘 뒤에만 이름을 바꾼다. 받은 파일의 sha256.
    부르는 쪽(ensure_weights)이 weight_lock 을 쥐고 부른다: 받기·해시·게시가 한 임계 구역이다(Codex 리뷰 R1).
    끊기면 지수 백오프로 다시 시도하며 임시 파일에서 이어받는다. 해시가 다르면 임시 파일을 지운다(깨진 파일을 이어받지 않게)."""
    part = target.with_name(f"{target.name}.part")
    attempts = max(1, int(os.environ.get("SIMAI_FETCH_ATTEMPTS") or FETCH_ATTEMPTS))
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(part, "a+b") as f:
            for attempt in range(1, attempts + 1):
                f.flush()
                have = os.fstat(f.fileno()).st_size
                try:
                    got = _download_once(entry, f, have).hexdigest()
                    break
                except urllib.error.HTTPError:
                    raise  # 401·403·404 등은 다시 해도 같다
                except (_Retry, urllib.error.URLError, http.client.HTTPException, OSError) as e:
                    if attempt == attempts:
                        raise BootstrapError(EXIT_STORAGE, f"{entry['name']} 받기 실패({attempts}회 시도, 받은 {have} 바이트는 "
                                                           f"{part.name} 에 남김): {type(e).__name__}: {e}") from e
                    wait = min(60.0, 2.0 ** attempt)
                    f.flush()
                    log(f"{entry['name']} 받기 끊김({type(e).__name__}: {e}) → {wait:.0f}s 뒤 다시 "
                        f"({attempt}/{attempts}, {os.fstat(f.fileno()).st_size} 바이트부터 이어받기)")
                    backoff_sleep(wait)
        if got != entry["sha256"]:
            part.unlink(missing_ok=True)
            raise BootstrapError(EXIT_STORAGE, f"{entry['name']} 받은 파일 해시 불일치 {got[:12]}… ≠ 목록 {entry['sha256'][:12]}… "
                                               "(버림, 시작하지 않음)")
        os.replace(part, target)
        return got
    except BootstrapError:
        _drop_empty(part)
        raise
    except Exception as e:  # noqa: BLE001 - 받기 실패는 모두 가중치 문제(코드 2)로. 메시지는 _redact 를 거쳐 나간다
        _drop_empty(part)
        raise BootstrapError(EXIT_STORAGE, f"{entry['name']} 받기 실패: {type(e).__name__}: {e}") from e


def _drop_empty(part: Path) -> None:
    """받은 것이 없는 임시 파일만 지운다(받은 바이트는 다음 시작에 이어받는다)."""
    try:
        if part.stat().st_size == 0:
            part.unlink()
    except OSError:
        pass


def _stamped(w: dict) -> bool:
    """검증 스탬프를 쓰는 항목: tier0 묶음에 없는 것(워커·실험 가중치). Tier 0 가중치는 매번 해시한다."""
    return "tier0" not in w.get("sets", []) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", w.get("name", "")))


def _stamp_file(root: Path, w: dict) -> Path:
    return root.joinpath(*STAMP_DIR, f"{w['name']}.json")


def _stamp_matches(root: Path, w: dict, target: Path) -> bool:
    try:
        d = json.loads(_stamp_file(root, w).read_text(encoding="utf-8"))
        st = target.stat()
    except (OSError, ValueError):
        return False
    return isinstance(d, dict) and d.get("path") == w["path"] and d.get("sha256") == w["sha256"] and d.get("stat") == _ident(st)


def _ident(st: os.stat_result) -> list[int]:
    """파일 식별: 크기·mtime_ns·inode·ctime_ns. mtime 을 되돌린 복사·교체(새 inode, ctime 갱신)도 다시 해시하게 한다(Codex 리뷰 권장)."""
    return [st.st_size, st.st_mtime_ns, st.st_ino, st.st_ctime_ns]


def _write_stamp(root: Path, w: dict, target: Path, st: os.stat_result) -> None:
    """st: 해시하기 전에 잰 stat. 해시한 뒤의 stat 과 다르면(해시하는 동안 바뀜) 스탬프를 남기지 않는다."""
    f = _stamp_file(root, w)
    try:
        if _ident(target.stat()) != _ident(st):
            log(f"{w['name']} 확인하는 동안 파일이 바뀌어 검증 스탬프를 남기지 않는다(다음 시작에 다시 해시)")
            return
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f"{f.name}.tmp-{os.getpid()}")
        tmp.write_text(json.dumps({"path": w["path"], "sha256": w["sha256"], "stat": _ident(st),
                                   "verified_at": time.time_ns() // 1_000_000}) + "\n", encoding="utf-8")
        os.replace(tmp, f)
    except OSError as e:
        log(f"{w['name']} 검증 스탬프를 쓰지 못함(다음 시작에 다시 해시한다): {e}")


def ensure_weights(root: Path, entries: list[dict]) -> list[dict]:
    # 해시를 모르는 항목이 하나라도 있으면 아무것도 받기 전에 멈춘다(앞 항목 수 GB 를 받고 나서 실패하지 않게, S15P31A101-207)
    for w in entries:
        if not re.fullmatch(r"[0-9a-f]{64}", w.get("sha256") or ""):
            raise BootstrapError(EXIT_STORAGE, f"{w['name']}: 목록에 sha256 이 없어 확인할 수 없다(모르면 시작하지 않는다)")
    out = []
    for w in entries:
        target = root / w["path"]
        want = w["sha256"]
        stamped = _stamped(w)
        rec = {"name": w["name"], "path": str(target), "license": w.get("license", "")}
        with weight_lock(root, w):  # 재확인·받기·해시·게시·스탬프를 한 임계 구역으로(기다린 쪽은 먼저 끝난 쪽의 파일을 다시 확인한다)
            if target.is_file():
                if stamped and _stamp_matches(root, w, target):
                    rec["status"] = "present-stamp"  # 크기·mtime·inode·ctime 이 확인 때와 같다: 다시 해시하지 않는다
                else:
                    st = target.stat()
                    got = sha256_file(target)
                    if got != want:
                        raise BootstrapError(EXIT_STORAGE, f"{w['name']} 가중치 해시 불일치: {target} {got[:12]}… ≠ 목록 {want[:12]}… "
                                                           "(지우지 않음, 시작하지 않음)")
                    rec["status"] = "present"
                    if stamped:
                        _write_stamp(root, w, target, st)
            else:
                log(f"{w['name']} 받는 중 → {target}")
                fetch(w, target)
                rec["status"] = "downloaded"
                if stamped:
                    _write_stamp(root, w, target, target.stat())  # 받은 내용은 fetch 가 해시했다(같은 임계 구역 안)
        rec["sha256"] = want
        if w.get("env"):
            os.environ.setdefault(w["env"], str(target))
            os.environ.setdefault(f"{w['env']}_SHA256", want)
        out.append(rec)
    return out


# ---------------------------------------------------------------- 준비·실행


def prepare(want_set: str = "tier0") -> dict:
    """준비 1)~3). 실패하면 BootstrapError. 기록은 저장 위치를 쓸 수 있을 때 남긴다."""
    started = time.time_ns() // 1_000_000
    rec: dict = {"bootstrap": "S15P31A101-205", "started_at": started, "set": want_set,
                 "hf_token": "present" if _hf_token else "absent"}
    root, source = choose_root()
    smi = shlex.split(os.environ.get("SIMAI_NVIDIA_SMI") or "nvidia-smi")
    min_free = float(os.environ.get("SIMAI_MIN_FREE_GB") or 20)
    manifest = Path(os.environ.get("SIMAI_WEIGHTS_MANIFEST") or DEFAULT_MANIFEST)
    storage_ok = False
    try:
        rec["storage"] = prepare_storage(root, source, min_free)
        storage_ok = True
        rec["gpu"] = detect_gpu(smi)
        rec["weights"] = ensure_weights(root, load_manifest(manifest, want_set))
        rec["exit"] = EXIT_OK
    except BootstrapError as e:
        rec |= {"exit": e.code, "error": _redact(str(e))}
        raise
    finally:
        rec["finished_at"] = time.time_ns() // 1_000_000
        if storage_ok or root.is_dir():
            _write_record(root, rec)
    os.environ["SIMAI_ROOT"] = str(root)
    os.environ["SIMAI_GPU_ARCH"] = rec["gpu"]["arch"]
    gpu = rec["gpu"]
    log(f"저장 {root} ({source}, 남은 {rec['storage']['free_gb']} GB), GPU {gpu['name']} {gpu['arch']} "
        f"{gpu['memory_total_mb']:.0f} MiB, 가중치 " + ", ".join(f"{w['name']} {w['status']}" for w in rec["weights"]))
    return rec


def _write_record(root: Path, rec: dict) -> None:
    d = root / "runs" / "bootstrap"
    name = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}.json"
    n = 0
    while (d / name).exists():  # 같은 초·같은 프로세스의 두 번째 기록(감시 모드의 워커 가중치). 추가만 하고, 이름순 = 시간순('~' 는 '.' 뒤)
        n += 1
        name = f"{name.split('.')[0].split('~')[0]}~{n}.json"
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(_redact(json.dumps(rec, ensure_ascii=False, indent=2)) + "\n", encoding="utf-8")
        rec["record"] = str(d / name)
    except OSError as e:
        log(f"기록을 {d} 에 쓰지 못함: {e}")


def execvp(argv: list[str]) -> None:  # 시험에서 바꿔 끼운다
    os.execvpe(argv[0], argv, child_env())


# ---------------------------------------------------------------- 감시 프로세스 (S15P31A101-246)


def parse_services(text: str | None) -> list[str]:
    """SIMAI_SERVICES → 기동 순서 목록. tier0 가 꼭 있어야 하고 모르는 이름·중복은 사용법 오류(받기 전에 멈춘다)."""
    names = [s.strip() for s in (text or "tier0").split(",") if s.strip()]
    if not names or "tier0" not in names or len(set(names)) != len(names) or set(names) - set(SERVICES):
        hint = " (scene 은 서비스가 아니다: 장면 준비는 LIVE 와 따로 'simai-bootstrap scene <명령>')" if "scene" in names else ""
        raise BootstrapError(EXIT_USAGE, f"{SERVICES_ENV}={text!r} 오류: tier0 를 넣고 tier0·vllm·worker·avatar·ppe 중에서 쉼표로 고른다{hint}")
    return [n for n in SERVICES if n in names]


def exit_code(rc: int) -> int:
    return 128 - rc if rc < 0 else rc  # Popen 은 신호 종료를 음수로 준다


def _env_float(env: dict, name: str, default: float) -> float:
    try:
        return float(env.get(name) or default)
    except ValueError as e:
        raise BootstrapError(EXIT_USAGE, f"{name}={env.get(name)!r} 는 숫자여야 한다") from e


def _load_waiter():
    """simai-wait-tier0(deploy/scripts/wait_tier0_ready.py)의 표시 파일 판정을 그대로 쓴다(같은 규칙을 두 번 쓰지 않는다)."""
    here = Path(__file__).resolve().parent
    for cand in (here / "wait_tier0_ready.py", here / "simai-wait-tier0"):
        if cand.is_file():
            loader = importlib.machinery.SourceFileLoader("simai_wait_tier0", str(cand))
            spec = importlib.util.spec_from_loader(loader.name, loader)
            mod = importlib.util.module_from_spec(spec)
            loader.exec_module(mod)
            return mod
    raise BootstrapError(EXIT_USAGE, f"simai-wait-tier0 가 {here} 에 없다(Tier 0 준비를 판정할 수 없다)")


def tier0_marker_ready(marker: Path, waiter, since_ms: int | None, pid: int | None) -> tuple[bool, str]:
    """simai-wait-tier0 판정(phase=running·reserve OK·15초 안 갱신)에 더해, 이번에 띄운 Tier 0 가 기동 뒤에 쓴 표시 파일인지 본다(리뷰 -246).
    이전 실행이 비정상 종료로 남긴 running 표시(mtime 이 아직 새것)를 새 Tier 0 의 준비로 보면, 진입점이 엔진을 다시 만드는 동안(약 550초)
    vLLM·워커가 GPU 를 먼저 잡는다. 표시 파일은 지우지 않는다(Tier 0 가 previous_unclean 판정에 쓴다). 표시 파일 형식은 그대로:
    gpu_priority.written_at(Tier 0 가 쓴 UTC ms)이 이번 기동 시각 이후이고 gpu_priority.pid 가 띄운 프로세스(simai-tier0 이 exec 해 같은 pid)여야 한다."""
    ok, why = waiter.readiness(marker, {"OK"})
    if not ok:
        return ok, why
    try:
        gp = json.loads(marker.read_text(encoding="utf-8"))["gpu_priority"]
        written, owner = gp.get("written_at"), gp.get("pid")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False, "표시 파일을 다시 읽지 못함"
    if since_ms is None or not isinstance(written, int) or isinstance(written, bool) or written < since_ms:
        return False, f"이번 Tier 0 기동 전의 표시 파일(written_at {written!r} < 기동 {since_ms})"
    if pid is not None and owner != pid:
        return False, f"다른 프로세스가 쓴 표시 파일(pid {owner!r} ≠ 띄운 Tier 0 {pid})"
    return True, why


def http_ok(url: str) -> bool:  # 시험에서 바꿔 끼운다
    try:
        with urllib.request.urlopen(url, timeout=2) as r:  # noqa: S310 - 127.0.0.1 의 vLLM /health
            return getattr(r, "status", 200) == 200
    except (OSError, http.client.HTTPException):
        return False


def _die_with_parent() -> None:
    """Linux prctl(PR_SET_PDEATHSIG, SIGKILL): 감시 부모가 죽으면 이 자식도 끝난다(자식 쪽 fork 뒤 exec 전에 돈다)."""
    import ctypes

    ctypes.CDLL(None, use_errno=True).prctl(1, int(KILL), 0, 0, 0)  # PR_SET_PDEATHSIG = 1


class Proc:
    """서비스 프로세스 하나. 새 세션(= 새 프로세스 그룹)으로 띄워 그룹 전체에 신호를 보낸다(손자 프로세스까지).
    출력은 줄마다 [이름] 접두로 표준 오류에 내보내고 원문을 로그 파일에 덧붙인다."""

    def __init__(self, name: str, argv: list[str], env: dict[str, str], log_dir: Path, *, die_with_parent: bool = False,
                 stdin_pipe: bool = False) -> None:
        self.name = name
        log_dir.mkdir(parents=True, exist_ok=True)
        self._log = open(log_dir / f"{name}.log", "ab")  # noqa: SIM115 - 출력 스레드가 닫는다
        kw: dict = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if die_with_parent and sys.platform.startswith("linux"):
            kw["preexec_fn"] = _die_with_parent  # 리더만 받는다. 손자는 Tier 0 쪽 그룹 회수(reap_scene_group)가 맡는다
        try:
            self.p = subprocess.Popen(argv, env=env, stdin=subprocess.PIPE if stdin_pipe else subprocess.DEVNULL,
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)
        except OSError:
            self._log.close()
            raise
        self.pid = self.p.pid
        self._pump = threading.Thread(target=self._copy_output, name=f"out-{name}", daemon=True)
        self._pump.start()

    def _copy_output(self) -> None:
        try:
            for raw in iter(self.p.stdout.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                with _out_lock:
                    print(f"[{self.name}] {line}", file=sys.stderr, flush=True)
                self._log.write(raw if raw.endswith(b"\n") else raw + b"\n")
                self._log.flush()
        finally:
            self._log.close()

    def poll(self) -> int | None:
        return self.p.poll()

    def signal(self, sig: int) -> None:
        if os.name == "nt":  # 노트북 시험: 그룹 신호가 없다
            try:
                self.p.kill() if sig == KILL else self.p.terminate()
            except OSError:
                pass
            return
        try:
            os.killpg(self.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass  # 그룹이 이미 비었다

    def wait(self, timeout: float) -> int | None:
        try:
            return self.p.wait(timeout)
        except subprocess.TimeoutExpired:
            return None

    def pin(self, cpus: set[int] | None) -> None:
        if cpus and hasattr(os, "sched_setaffinity"):
            try:
                os.sched_setaffinity(self.pid, cpus)  # 리더에 걸면 그 뒤의 손자가 물려받는다(nice 와 같음)
            except OSError as e:
                log(f"{self.name} CPU 고정 {sorted(cpus)} 실패: {e}")

    def renice(self, value: int) -> None:
        if value and hasattr(os, "setpriority"):
            try:
                os.setpriority(os.PRIO_PROCESS, self.pid, value)  # 이 뒤에 만드는 손자(vLLM EngineCore 등)가 물려받는다
            except OSError as e:
                log(f"{self.name} nice {value} 실패: {e}")


def usable_cpus() -> list[int]:  # 시험에서 바꿔 끼운다
    if hasattr(os, "sched_getaffinity"):
        return sorted(os.sched_getaffinity(0))  # 컨테이너 cpuset 안의 CPU
    return list(range(os.cpu_count() or 1))


# cgroup CPU 할당량. session1_live(c0 기록)도 이 함수들을 쓴다(S15P31A101-278 에서 옮김)
def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def parse_quota(cpu_max: str | None = None, quota_us: str | None = None, period_us: str | None = None) -> float | None:
    """cgroup CPU 할당량 값(vCPU). v2 cpu.max("quota period" 또는 "max period") 또는 v1 cfs_quota/period. 제한 없음·깨진 값은 None."""
    try:
        if cpu_max is not None:
            quota, period = cpu_max.split()[:2]
            return None if quota == "max" else round(int(quota) / int(period), 2)
        if quota_us is not None and period_us is not None and int(quota_us) > 0:
            return round(int(quota_us) / int(period_us), 2)
    except (ValueError, ZeroDivisionError):
        pass
    return None


def _cpu_cgroup_dirs(read) -> list[tuple[str, str]]:
    """자기 프로세스의 CPU 컨트롤러 cgroup 디렉터리들(잎 → 마운트 기준점 순서, ("v1"|"v2", 경로)).

    /proc/self/cgroup 으로 자기 그룹 경로를 찾고, /proc/self/mountinfo 의 cgroup2 마운트·cpu 가 든 cgroup(v1) 마운트(공동 마운트
    cpu,cpuacct 포함)에 붙인다. 마운트 root 가 그룹 경로의 앞부분이면(컨테이너 cgroup 이름공간 밖 경로) 그만큼 뗀다.
    할당량은 조상 그룹에도 걸리므로 잎부터 마운트 기준점까지 모두 돌려준다.
    """
    groups: dict[str, str] = {}  # "v2" 또는 v1 컨트롤러 이름 → 그룹 경로
    for ln in (read("/proc/self/cgroup") or "").splitlines():
        parts = ln.strip().split(":", 2)
        if len(parts) != 3:
            continue
        _, controllers, path = parts
        if controllers == "":
            groups["v2"] = path
        else:
            for c in controllers.split(","):
                groups[c] = path
    mounts: list[tuple[str, str, str]] = []  # (종류, 마운트 root, 마운트 지점)
    for ln in (read("/proc/self/mountinfo") or "").splitlines():
        left, sep, right = ln.partition(" - ")
        f, r = left.split(), right.split()
        if not sep or len(f) < 5 or not r:
            continue
        if r[0] == "cgroup2":
            mounts.append(("v2", f[3], f[4]))
        elif r[0] == "cgroup" and len(r) >= 3 and "cpu" in r[2].split(","):
            mounts.append(("v1", f[3], f[4]))
    if not mounts:  # mountinfo 를 못 읽으면 흔한 마운트 지점만 본다(그룹 경로는 그대로 붙인다)
        mounts = [("v2", "/", "/sys/fs/cgroup"), ("v1", "/", "/sys/fs/cgroup/cpu,cpuacct"), ("v1", "/", "/sys/fs/cgroup/cpu")]
    out: list[tuple[str, str]] = []
    for kind, root, point in mounts:
        path = groups.get("v2" if kind == "v2" else "cpu")
        if path is None:
            continue
        rel = path
        if root != "/" and (path == root or path.startswith(root.rstrip("/") + "/")):
            rel = path[len(root.rstrip("/")):]
        segs = [x for x in rel.split("/") if x]
        for i in range(len(segs), -1, -1):
            out.append((kind, "/".join([point.rstrip("/"), *segs[:i]])))
    return out


def cgroup_vcpu(read=None) -> tuple[float | None, str | None]:  # 시험에서 바꿔 끼운다
    """자기 cgroup 의 CPU 할당량(vCPU)과 읽은 파일. 잎·조상 중 가장 작은 할당량이 실제 한도다. 제한 없음·못 읽음은 (None, None)."""
    read = read or _read
    best: tuple[float | None, str | None] = (None, None)
    for kind, d in _cpu_cgroup_dirs(read):
        if kind == "v2":
            value, src = parse_quota(cpu_max=read(f"{d}/cpu.max")), f"{d}/cpu.max"
        else:
            value = parse_quota(quota_us=read(f"{d}/cpu.cfs_quota_us"), period_us=read(f"{d}/cpu.cfs_period_us"))
            src = f"{d}/cpu.cfs_quota_us"
        if value is not None and (best[0] is None or value < best[0]):
            best = (value, src)
    return best


def _cpu_ranges(cpus) -> str:
    """{4,5,6,10} → '4-6,10' (로그용)"""
    out: list[str] = []
    for c in sorted(cpus):
        if out and int(out[-1].rpartition("-")[2]) == c - 1:
            out[-1] = f"{out[-1].partition('-')[0]}-{c}"
        else:
            out.append(str(c))
    return ",".join(out)


def sidecar_cpus(spec: str | None, avail: list[int], quota: float | None = None) -> set[int] | None:
    """vLLM·워커를 묶을 CPU. auto(기본): 쓸 수 있는 vCPU 수 n = min(affinity 개수, cgroup 할당량 올림)(할당량을 못 읽거나 제한 없음이면
    affinity 개수)가 8 이상이면 affinity 목록의 뒤쪽 n//2 개(앞쪽은 Tier 0 의 CPU 디코딩·기하 단계 몫), 8 미만이면 묶지 않는다(나누면 둘 다
    모자란다). 할당량은 컨테이너 전체가 나눠 쓰므로 affinity 224·할당량 23.8 인 Pod 에서 뒤쪽 112개에 묶으면 제한이 되지 않는다(3차 GPU 세션).
    none: 묶지 않음. '4-7,10': 그 목록(쓸 수 있는 것과 겹치는 것만). Tier 0 는 묶지 않는다."""
    spec = (spec or "auto").strip().lower()
    if spec in ("none", "off"):
        return None
    if spec == "auto":
        n = len(avail) if quota is None else min(len(avail), math.ceil(quota))
        return set(avail[len(avail) - n // 2:]) if n >= 8 else None
    out: set[int] = set()
    try:
        for part in spec.split(","):
            a, _, b = part.strip().partition("-")
            out |= set(range(int(a), int(b or a) + 1))
    except ValueError as e:
        raise BootstrapError(EXIT_USAGE, f"SIMAI_SIDECAR_CPUS={spec!r} 형식 오류(예: auto, none, 4-7,10)") from e
    out &= set(avail)
    if not out:
        raise BootstrapError(EXIT_USAGE, f"SIMAI_SIDECAR_CPUS={spec!r} 에 쓸 수 있는 CPU 가 없다(쓸 수 있는 {avail})")
    return out


def compute_gpu_fractions(services: list[str], env: dict[str, str]) -> dict[str, float]:
    """서비스 목록에 맞춰 GPU 메모리 상한(vllm·avatar·worker·ppe)을 계산·검증한다 (S15P31A101-382·-525).

    분석 예산 0.70 안에서 vLLM 0.30·avatar 0.10·worker 나머지(0.30, 아바타 없으면 0.40)를 나눈다. 명시 값은 예산 안에서 우선한다.
    워커가 켜진 구성에서는 워커 몫이 최소 보장 비율(MIN_WORKER_TORCH_FRACTION=0.10, 80GB 기준 ~8GB) 이상이어야 한다(12차 GPU 세션 MapAnything 6.4GB OOM 방지).
    ppe 는 분석 워커 예산 밖의 별도 몫(기본 0.05)이고 Tier 0 + 분석 워커 + PPE ≤ 0.95 를 본다(명세 3.2, S15P31A101-507).
    """
    from simai_ai.gpu.budget import HEADROOM_FRACTION, BudgetError, WorkerGpuSettings

    env = dict(env)
    env.setdefault("SIMAI_WORKER_VLLM_GPU_MEMORY_UTILIZATION", VLLM_GPU_MEMORY_UTILIZATION)
    try:
        budget = WorkerGpuSettings.from_env(env).budget
    except BudgetError as e:
        raise BootstrapError(EXIT_USAGE, f"GPU 예산 설정 오류: {e}") from e
    vllm_fraction = budget.vllm_gpu_memory_utilization if "vllm" in services else 0.0
    avatar_fraction = _env_float(env, "SIMAI_AVATAR_GPU_MEMORY_FRACTION", 0.10) if "avatar" in services else 0.0
    remaining = round(budget.worker_fraction - vllm_fraction - avatar_fraction, 6)
    worker_fraction = (
        _env_float(env, "SIMAI_WORKER_TORCH_MEMORY_FRACTION", remaining)
        if "worker" in services
        else remaining
    )
    if (
        ("avatar" in services and not 0 < avatar_fraction <= budget.worker_fraction)
        or remaining < 0
        or ("worker" in services and not 0 < worker_fraction <= remaining)
    ):
        raise BootstrapError(EXIT_USAGE, "avatar·vLLM·worker GPU 상한 합이 분석 워커 예산을 넘는다")
    if "worker" in services and worker_fraction + 1e-9 < MIN_WORKER_TORCH_FRACTION:
        raise BootstrapError(
            EXIT_USAGE,
            f"분석 워커 GPU 몫({worker_fraction:.2f})이 최소 보장 비율({MIN_WORKER_TORCH_FRACTION:.2f})보다 작다 "
            f"(vllm={vllm_fraction:.2f}, avatar={avatar_fraction:.2f}, 남은 예산={remaining:.2f}; MapAnything 6.4GB OOM 방지)",
        )
    ppe_fraction = _env_float(env, "SIMAI_PPE_GPU_MEMORY_FRACTION", PPE_FRACTION_DEFAULT) if "ppe" in services else 0.0
    if "ppe" in services:
        limit = 1 - HEADROOM_FRACTION
        if not 0 < ppe_fraction <= 1:
            raise BootstrapError(EXIT_USAGE, f"SIMAI_PPE_GPU_MEMORY_FRACTION={ppe_fraction} 는 0 초과 1 이하다")
        if budget.tier0_fraction + budget.worker_fraction + ppe_fraction > limit + 1e-9:
            raise BootstrapError(EXIT_USAGE, f"Tier 0 {budget.tier0_fraction} + 분석 워커 {budget.worker_fraction} + PPE "
                                             f"{ppe_fraction} 이 {limit:.2f} 을 넘는다(명세 3.2)")
    return {
        "budget_worker_fraction": budget.worker_fraction,
        "vllm_fraction": vllm_fraction,
        "avatar_fraction": avatar_fraction,
        "worker_fraction": worker_fraction,
        "ppe_fraction": ppe_fraction,
    }


class Service:
    def __init__(self, name: str, argv: list[str], env: dict[str, str]) -> None:
        self.name, self.argv, self.env = name, argv, env
        self.proc: Proc | None = None
        self.started = 0.0
        self.healthy = False  # vLLM 은 /health 200 을 본 뒤, 워커는 띄우자마자
        self.ever_healthy = False
        self.last_health = -1e9
        self.fails = 0  # 연달아 빨리 죽은 수
        self.next_at = 0.0
        self.given_up = False


class Supervisor:
    """Tier 0 → (Tier 0 준비) → 워커 가중치 → vLLM → 분석 워커. Tier 0 가 끝나면 나머지를 내리고 Tier 0 종료 코드로 끝난다."""

    def __init__(self, root: Path, services: list[str], tier0_argv: list[str], env: dict[str, str] | None = None, *,
                 spawn=Proc, ready=None, health=None, clock=time.monotonic, sleep=time.sleep, wall_ms=None,
                 pod_jsonl: Path | None = None, worker_id: tuple[str | None, str] | None = None) -> None:
        env = dict(os.environ) if env is None else env
        self.root = root
        self.services = services
        self.spawn, self.clock, self.sleep = spawn, clock, sleep
        self.wall_ms = wall_ms or (lambda: time.time_ns() // 1_000_000)
        self.t0_since_ms: int | None = None  # 이번에 띄운 Tier 0 의 기동 시각·pid(표시 파일이 그 뒤의 것인지 본다)
        self.t0_pid: int | None = None
        self.log_dir = root / "logs"
        self.marker = Path(env.get("SIMAI_TIER0_MARKER") or root / "state" / "tier0.live")
        self.pod_jsonl = pod_jsonl or (Path(env["SIMAI_POD_JSONL"]) if env.get("SIMAI_POD_JSONL") else None)
        self._last_marker_written: int | None = None
        self.port = int(_env_float(env, "SIMAI_VLLM_PORT", 8000))
        self.ready_timeout_s = _env_float(env, "SIMAI_VLLM_READY_TIMEOUT_S", 1800)
        self.grace_s = _env_float(env, "SIMAI_STOP_GRACE_S", 15)
        self.backoff_s = _env_float(env, "SIMAI_SUPERVISE_BACKOFF_S", 5)
        self.backoff_max_s = _env_float(env, "SIMAI_SUPERVISE_BACKOFF_MAX_S", 300)
        self.min_uptime_s = _env_float(env, "SIMAI_SUPERVISE_MIN_UPTIME_S", 60)
        self.max_fast = int(_env_float(env, "SIMAI_SUPERVISE_MAX_FAST_FAILURES", 5))
        self.nice = int(_env_float(env, "SIMAI_SIDECAR_NICE", 10))
        self.tick_s = _env_float(env, "SIMAI_SUPERVISE_TICK_S", 0.5)
        self.worker_set = env.get(WORKER_SET_ENV) or "vlm"
        self.avatar_set = env.get("SIMAI_AVATAR_WEIGHTS_SET") or "avatar"
        self.ppe_set = env.get("SIMAI_PPE_WEIGHTS_SET") or "ppe"
        # 현장 세팅(site_setup) MapAnything·WorldMirror 2.0·LaMa 가중치(reconstruct_tasks 와 같은 규칙). 워커를 띄울 때만 뒤에서 받는다(-473, -539)
        self.setup_model_dir = Path(env.get("SIMAI_MAPANYTHING_MODEL") or root / "weights" / "map-anything")
        self.setup_wm2_dir = Path(
            env.get("SIMAI_WM2_MODEL")
            or env.get("SIMAI_WORLDMIRROR_MODEL")
            or root / "weights" / "hy-world-2.0" / "HY-WorldMirror-2.0"
        )
        self.setup_lama_path = Path(env.get("SIMAI_LAMA_WEIGHTS") or root / "weights" / "lama" / "big-lama.pt")
        self.setup_weights = (env.get("SIMAI_WORKER_SETUP_WEIGHTS") or "1").strip() not in ("0", "false", "no", "off")
        self._setup_weights_thread: threading.Thread | None = None
        self.setup_weights_error: str | None = None
        self.manifest = Path(env.get("SIMAI_WEIGHTS_MANIFEST") or DEFAULT_MANIFEST)
        if ready is None:
            waiter = _load_waiter()
            ready = lambda: tier0_marker_ready(self.marker, waiter, self.t0_since_ms, self.t0_pid)  # noqa: E731
        self.ready = ready
        self.health = health or (lambda: http_ok(f"http://127.0.0.1:{self.port}/health"))
        base = {k: v for k, v in env.items() if k not in (*SECRET_ENVS, "SIMAI_TIER0_TOKEN_FILE", "SIMAI_WORKER_TOKEN_FILE")}
        base["SIMAI_ROOT"] = str(root)
        base["SIMAI_TIER0_MARKER"] = str(self.marker)
        with_image_pythonpath(base)  # SSH 셸처럼 WORKDIR 밖에서 띄워도 python -m services.* 를 찾는다(-414)
        # 워커 ID(-414): 워커·아바타 환경에 넣는다. lobby 는 정한 값을 넘기고, tier0 감시 모드는 환경·토큰 파일 이름에서 정한다
        if worker_id is None:
            worker_id = resolve_worker_id(
                None, env, Path(env["SIMAI_WORKER_TOKEN_FILE"]) if env.get("SIMAI_WORKER_TOKEN_FILE") else None,
                Path(env["SIMAI_WORKER_ENV_FILE"]) if env.get("SIMAI_WORKER_ENV_FILE") else None)
        self.worker_id, wid_src = worker_id
        self.worker_id_note = None
        if "worker" in services or "avatar" in services:
            self.worker_id_note = (f"워커 ID {self.worker_id} ({wid_src}) → worker·avatar 환경 {WORKER_ID_ENV}" if self.worker_id else
                                   f"주의: 워커 ID 를 정하지 못함({wid_src}) → 워커는 기본 worker-01 로 뜬다. EC2 에 등록한 ID 와 다르면 대여가 401 이다"
                                   f"(--worker-id·{WORKER_ID_ENV} 또는 토큰 파일 이름 <워커 ID>.token)")
        threads = env.get("SIMAI_SIDECAR_THREADS") or "1"
        avail = usable_cpus()
        quota, quota_src = cgroup_vcpu()
        self.cpus = sidecar_cpus(env.get("SIMAI_SIDECAR_CPUS"), avail, quota)
        self.cpu_note = (f"CPU 묶기({env.get('SIMAI_SIDECAR_CPUS') or 'auto'}): affinity {len(avail)}개, cgroup 상한 "
                         f"{f'{quota} vCPU ({quota_src})' if quota is not None else '없음·못 읽음'} → vLLM·워커 "
                         f"{f'{_cpu_ranges(self.cpus)} ({len(self.cpus)}개)' if self.cpus else '묶지 않음'}, Tier 0 묶지 않음")
        # 프로세스마다 JIT·커널 캐시를 나눈다(같은 판이어도 동시에 쓰는 경합을 피한다). 컨테이너 디스크(/tmp): 저장 위치에 남길 산출물이 아니다
        cache_root = Path(env.get("SIMAI_CACHE_ROOT") or ("/tmp/simai-cache" if os.name != "nt" else Path(tempfile.gettempdir()) / "simai-cache"))
        worker_env = dict(env)
        if not worker_env.get("SIMAI_WORKER_ENV_FILE") and worker_env.get("SIMAI_WORKER_TOKEN_FILE"):
            worker_env["SIMAI_WORKER_ENV_FILE"] = str(
                ensure_worker_env_file(Path(worker_env["SIMAI_WORKER_TOKEN_FILE"]), cache_root / "private" / "worker.env")
            )
        tier0_token = (
            read_secret_file(Path(env["SIMAI_TIER0_TOKEN_FILE"]), "SIMAI_TIER0_TOKEN")
            if env.get("SIMAI_TIER0_TOKEN_FILE")
            else env.get("SIMAI_TIER0_TOKEN")
        )
        if tier0_token:
            _extra_secrets.add(tier0_token)
        budget_env = dict(env)
        env_path = worker_env.get("SIMAI_WORKER_ENV_FILE")
        if not env_path and (default := self.root.parent / ".simai-private" / "worker.env").is_file():
            env_path = str(default)
        if env_path:
            path = Path(env_path)
            if not path.is_file():
                raise BootstrapError(EXIT_STORAGE, "워커 env 파일을 읽을 수 없다")
            value = _file_value(path, "SIMAI_WORKER_TORCH_MEMORY_FRACTION")
            if value is not None:
                budget_env["SIMAI_WORKER_TORCH_MEMORY_FRACTION"] = value
        fractions = (compute_gpu_fractions(services, budget_env)
                     if any(s in services for s in ("vllm", "worker", "avatar", "ppe")) else None)
        # PPE 체크포인트(가중치 목록 ppe 묶음 중 env SIMAI_PPE_WEIGHTS 항목): 경로·sha256 을 PPE 환경에 넣는다(받기는 Tier 0 준비 뒤)
        ppe_weights = None
        if "ppe" in services:
            ppe_weights = next((w for w in load_manifest(self.manifest, self.ppe_set) if w.get("env") == "SIMAI_PPE_WEIGHTS"),
                               None)
            if ppe_weights is None:
                raise BootstrapError(EXIT_STORAGE, f"가중치 목록 {self.manifest} 의 {self.ppe_set} 묶음에 SIMAI_PPE_WEIGHTS 항목이 없다")
        self.svc: dict[str, Service] = {}
        for name in services:
            e = dict(base)
            if name == "tier0" and tier0_token:
                e["SIMAI_TIER0_TOKEN"] = tier0_token
            if name in ("worker", "avatar") and self.worker_id:
                e[WORKER_ID_ENV] = self.worker_id
            if fractions is not None:
                if "avatar" in services:
                    e["SIMAI_AVATAR_GPU_MEMORY_FRACTION"] = str(fractions["avatar_fraction"])
                    if name == "tier0":
                        e["SIMAI_AVATAR_SHARE"] = "1"
                    if name == "avatar" and not e.get("SIMAI_AVATAR_CHANNEL") and e.get("SIMAI_AVATAR_PORT"):
                        e["SIMAI_AVATAR_CHANNEL"] = f"wss://127.0.0.1:{e['SIMAI_AVATAR_PORT']}"
                if "ppe" in services:
                    if name == "tier0":
                        e["SIMAI_AVATAR_SHARE"] = "1"  # PPE 입력도 아바타 입력 공유 파일이다(아바타가 꺼져 있어도 켠다)
                        e["SIMAI_PPE_RELAY"] = "1"  # PPE outbox 를 Tier 0 I1 로 대신 보낸다
                    if name == "ppe":
                        e["SIMAI_PPE_GPU_MEMORY_FRACTION"] = str(fractions["ppe_fraction"])
                        e["SIMAI_PPE_WEIGHTS"] = str(self.root / ppe_weights["path"])
                        e["SIMAI_PPE_WEIGHTS_SHA256"] = str(ppe_weights["sha256"])
                if name == "worker":
                    e["SIMAI_WORKER_TORCH_MEMORY_FRACTION"] = str(fractions["worker_fraction"])
                    e["SIMAI_VALIDATED_WORKER_FRACTION"] = str(fractions["worker_fraction"])
                    e.setdefault("SIMAI_TIER0_WEIGHTS", str(self.root / "weights" / "yolo" / "yolo26x-pose.pt"))
                    e.setdefault("SIMAI_WM2_MODEL", str(self.setup_wm2_dir))
                    e.setdefault("SIMAI_WORLDMIRROR_MODEL", str(self.setup_wm2_dir))
                    e.setdefault("SIMAI_LAMA_WEIGHTS", str(self.setup_lama_path))
                    e.setdefault("SIMAI_VLLM_PORT", str(self.port))
                    e.setdefault("SIMAI_VLM_MODEL", env.get("SIMAI_VLLM_SERVED_NAME") or "cosmos3-nano")
            cache = cache_root / name
            e["TRITON_CACHE_DIR"], e["TORCHINDUCTOR_CACHE_DIR"] = str(cache / "triton"), str(cache / "inductor")
            e["CUDA_CACHE_PATH"] = str(cache / "cuda")
            if name != "tier0":  # Tier 0 의 CPU 디코딩·단일 스레드 기하 단계를 보호한다(자문 3-4). torch 의 intra-op 스레드도 OMP_NUM_THREADS 를 따른다
                for k in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
                    e[k] = threads
            if name == "vllm":
                e.setdefault("VLLM_USE_DEEP_GEMM", "0")
                # runtime 이미지에 nvcc 가 없어 FlashInfer top-k/top-p 샘플러 JIT 가 메모리 측정 중 실패한다(3차 GPU 세션). PyTorch 샘플러로
                e.setdefault("VLLM_USE_FLASHINFER_SAMPLER", "0")
                e["HF_HUB_OFFLINE"] = "1"  # 모델은 확인한 로컬 폴더만. 토큰 없이 HF 로 나가지 않는다
            argv = {"tier0": tier0_argv, "vllm": self._vllm_argv(worker_env), "worker": self._worker_argv(worker_env),
                    "avatar": self._worker_argv(worker_env, avatar=True),
                    "ppe": [sys.executable, "-m", "services.ppe"]}[name]  # 워커 토큰 필요 없음(I1 은 Tier 0 가 보낸다)
            self.svc[name] = Service(name, argv, e)
        self.phase = "wait_tier0"  # → weights → run | off(워커 쪽 포기)
        self.last_why = ""
        self.stop_signal: int | None = None
        self._weights: threading.Thread | None = None
        self.weights_error: str | None = None

    def _record_pod_marker(self) -> None:
        if self.pod_jsonl is None or not self.marker.is_file():
            return
        try:
            gp = json.loads(self.marker.read_text(encoding="utf-8")).get("gpu_priority", {})
            w = gp.get("written_at")
            if w and w != self._last_marker_written:
                self._last_marker_written = w
                self.pod_jsonl.parent.mkdir(parents=True, exist_ok=True)
                row = {"ts_ms": self.wall_ms(), "gpu_priority": gp}
                with self.pod_jsonl.open("a", encoding="utf-8", newline="\n") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def _vllm_argv(self, env: dict) -> list[str]:
        model = env.get("SIMAI_VLLM_MODEL") or str(self.root / "weights" / "cosmos3-nano")
        extra = shlex.split(env.get("SIMAI_VLLM_EXTRA_ARGS") or "")
        # 모델 기본 262,144 토큰은 한 요청 KV 36 GiB 라 기동이 실패한다(3차 GPU 세션). EXTRA_ARGS 값이 우선
        given = any(a.split("=", 1)[0].replace("_", "-") == "--max-model-len" for a in extra)
        max_len = [] if given else ["--max-model-len", env.get("SIMAI_WORKER_VLLM_MAX_MODEL_LEN") or VLLM_MAX_MODEL_LEN]
        # FP8 온라인 양자화(11-06 GPU PoC: 가중치 16.79 → 10.05 GiB, S15P31A101-525). none·빈 값이면 끄고, EXTRA_ARGS 에 이미 있으면 중복하지 않는다
        raw_quant = (env["SIMAI_VLLM_QUANTIZATION"] if "SIMAI_VLLM_QUANTIZATION" in env else VLLM_QUANTIZATION).strip()
        given_quant = any(a.split("=", 1)[0].replace("_", "-") in ("--quantization", "-q") for a in extra)
        quant = [] if (given_quant or not raw_quant or raw_quant.lower() == "none") else ["--quantization", raw_quant]
        return [*shlex.split(env.get("SIMAI_VLLM_CMD") or "vllm"), "serve", model,
                "--served-model-name", env.get("SIMAI_VLLM_SERVED_NAME") or "cosmos3-nano",
                "--hf-overrides", VLLM_HF_OVERRIDES,
                "--gpu-memory-utilization", env.get("SIMAI_WORKER_VLLM_GPU_MEMORY_UTILIZATION") or VLLM_GPU_MEMORY_UTILIZATION,
                "--max-num-seqs", env.get("SIMAI_WORKER_VLLM_MAX_NUM_SEQS") or "4",
                *max_len,
                *quant,
                "--host", "127.0.0.1", "--port", str(self.port),
                "--mm-encoder-tp-mode", "data", "--async-scheduling",
                *extra]

    def _worker_argv(self, env: dict, avatar: bool = False) -> list[str]:
        cmd = ([sys.executable, "-m", "services.avatar"] if avatar else
               shlex.split(env["SIMAI_WORKER_CMD"]) if env.get("SIMAI_WORKER_CMD") else [sys.executable, "-m", "services.worker"])
        # 설정 자리(S15P31A101-246): 받을 작업 유형(VLM·HMR hmr_refine 등)은 워커 설정 파일의 처리기가 정한다(services.worker --config, 조립은 -227 등).
        # 예: SIMAI_WORKER_ARGS="--config /workspace/simai/worker.json"
        if not avatar:
            cmd += shlex.split(env.get("SIMAI_WORKER_ARGS") or "")
        env_file = env.get("SIMAI_WORKER_ENV_FILE")
        if not env_file and (default := self.root.parent / ".simai-private" / "worker.env").is_file():
            env_file = str(default)
        if env_file:  # 워커 토큰은 워커 환경에만(다른 프로세스·이 감시 프로세스 환경에 넣지 않는다)
            cmd = [*shlex.split(env.get("SIMAI_WAIT_TIER0_CMD") or "simai-wait-tier0"), "--env-file", env_file, "--", *cmd]
        return cmd

    def request_stop(self, signum: int, _frame=None) -> None:
        self.stop_signal = signum

    # -- 단계

    def _tier0_ready(self) -> bool:
        ok, why = self.ready()
        if not ok and why != self.last_why:
            log(f"Tier 0 준비 기다림: {why}")
        self.last_why = why
        return ok

    def _prepare_worker_weights(self) -> None:
        rec: dict = {"bootstrap": "S15P31A101-246", "stage": "worker-weights", "set": self.worker_set,
                     "started_at": time.time_ns() // 1_000_000, "hf_token": "present" if _hf_token else "absent"}
        try:
            entries = load_manifest(self.manifest, self.worker_set) if "vllm" in self.svc else []
            if "avatar" in self.svc:
                entries += load_manifest(self.manifest, self.avatar_set)
            if "ppe" in self.svc:
                entries += load_manifest(self.manifest, self.ppe_set)
            rec["weights"] = ensure_weights(self.root, entries)
            rec["exit"] = EXIT_OK
            log(f"워커 가중치 {self.worker_set} 준비: " + ", ".join(f"{w['name']} {w['status']}" for w in rec["weights"]))
        except Exception as e:  # noqa: BLE001 - 워커 가중치 실패는 Tier 0 를 멈추지 않는다
            self.weights_error = _redact(f"{type(e).__name__}: {e}")
            rec |= {"exit": getattr(e, "code", EXIT_STORAGE), "error": self.weights_error}
        finally:
            rec["finished_at"] = time.time_ns() // 1_000_000
            _write_record(self.root, rec)

    def _prepare_setup_weights(self) -> None:
        """site_setup 의 MapAnything·WorldMirror 2.0·LaMa 가중치(static 묶음, -473·-539). 실패해도 워커·Tier 0 는 계속(경고만)."""
        rec: dict = {"bootstrap": "S15P31A101-473", "stage": "setup-weights", "set": SCENE_SET, "model_dir": str(self.setup_model_dir),
                     "wm2_model_dir": str(self.setup_wm2_dir), "lama_weights": str(self.setup_lama_path),
                     "started_at": time.time_ns() // 1_000_000, "hf_token": "present" if _hf_token else "absent"}
        try:
            all_static = load_manifest(self.manifest, SCENE_SET)
            want_ma = self.setup_model_dir.resolve()
            ma_entries = [w for w in all_static if (self.root / w["path"]).parent.resolve() == want_ma]
            if not ma_entries:
                raise BootstrapError(EXIT_STORAGE, f"가중치 목록에 {self.setup_model_dir} 의 {SCENE_SET} 항목이 없다")
            want_wm2 = self.setup_wm2_dir.resolve()
            want_lama = self.setup_lama_path.resolve()
            entries = [
                w for w in all_static
                if (self.root / w["path"]).parent.resolve() in (want_ma, want_wm2)
                or (self.root / w["path"]).resolve() == want_lama
            ]
            rec["weights"] = ensure_weights(self.root, entries)
            rec["exit"] = EXIT_OK
            log(f"현장 세팅 정적 맵 가중치 준비({self.setup_model_dir}): "
                + ", ".join(f"{w['name']} {w['status']}" for w in rec["weights"]))
        except Exception as e:  # noqa: BLE001 - 세팅 가중치 실패는 Tier 0·워커를 멈추지 않는다
            self.setup_weights_error = _redact(f"{type(e).__name__}: {e}")
            rec |= {"exit": getattr(e, "code", EXIT_STORAGE), "error": self.setup_weights_error}
            log(f"주의: 현장 세팅 MapAnything 가중치({self.setup_model_dir}) 준비 실패 → 이대로 세팅하면 STATIC_MODEL_MISSING 로 실패한다. "
                f"'simai-bootstrap preflight' 로 확인: {self.setup_weights_error}")
        finally:
            rec["finished_at"] = time.time_ns() // 1_000_000
            _write_record(self.root, rec)

    def _start_setup_weights(self) -> None:
        if self._setup_weights_thread is not None or "worker" not in self.svc or not self.setup_weights:
            return
        self._setup_weights_thread = threading.Thread(target=self._prepare_setup_weights, name="setup-weights", daemon=True)
        self._setup_weights_thread.start()

    def step(self) -> None:
        now = self.clock()
        self._record_pod_marker()
        if self.phase == "wait_tier0" and self._tier0_ready():
            log("Tier 0 준비됨(phase=running, reserve OK) → 워커 쪽 준비")
            if "vllm" in self.svc or "avatar" in self.svc or "ppe" in self.svc:
                self.phase = "weights"
                self._weights = threading.Thread(target=self._prepare_worker_weights, name="worker-weights", daemon=True)
                self._weights.start()
            else:
                self.phase = "run"
        if self.phase == "weights" and self._weights is not None and not self._weights.is_alive():
            if self.weights_error:
                log(f"워커 가중치 {self.worker_set} 실패 → vLLM·워커를 띄우지 않는다(Tier 0 는 계속): {self.weights_error}")
                self.phase = "off"
            else:
                self.phase = "run"
        if self.phase == "run":
            self._start_setup_weights()  # 워커 가중치(VLM) 다음에 받아 대역을 다투지 않는다. 워커 시작은 막지 않는다
            for name in ("vllm", "worker", "avatar", "ppe"):
                if name in self.svc:
                    self._tend(self.svc[name], now)

    def _gate_open(self, s: Service) -> bool:
        if s.name == "worker" and "vllm" in self.svc:
            v = self.svc["vllm"]
            return v.ever_healthy or v.given_up  # VLM 이 한 번 뜬 뒤(포기했으면 VLM 없이)
        return True

    def _tend(self, s: Service, now: float) -> None:
        if s.given_up:
            return
        if s.proc is None:
            if now < s.next_at or not self._gate_open(s) or not self._tier0_ready():
                return  # 다시 띄우는 것도 Tier 0 표시 파일이 정상일 때만
            try:
                s.proc = self.spawn(s.name, s.argv, s.env, self.log_dir)
            except OSError as e:
                self._failed(s, now, True, f"띄우지 못함({type(e).__name__}: {e})")
                return
            s.started, s.healthy, s.last_health = now, s.name != "vllm", -1e9
            if s.name != "tier0":
                s.proc.renice(self.nice)
                s.proc.pin(self.cpus)
            log(f"{s.name} 시작 pid {s.proc.pid}: {shlex.join(s.argv)}")
            return
        rc = s.proc.poll()
        if rc is None:
            if not s.healthy:
                if now - s.last_health >= 5:
                    s.last_health = now
                    if self.health():
                        s.healthy = s.ever_healthy = True
                        log(f"{s.name} 준비됨({now - s.started:.0f}s, /health 200)")
                        return
                if now - s.started > self.ready_timeout_s:
                    s.proc.signal(KILL)
                    s.proc.wait(10)
                    self._reap(s, now, f"{self.ready_timeout_s:.0f}s 안에 /health 가 200 이 아님")
            elif s.fails and now - s.started >= self.min_uptime_s:
                s.fails = 0  # 오래 돌았다: 연속 실패를 잊는다
            return
        self._reap(s, now, f"종료 코드 {exit_code(rc)}")

    def _reap(self, s: Service, now: float, why: str) -> None:
        assert s.proc is not None
        s.proc.signal(KILL)  # 그룹에 남은 손자(vLLM EngineCore 등, GPU 메모리를 쥔다)를 정리한다
        fast = not s.healthy or now - s.started < self.min_uptime_s
        s.proc, s.healthy = None, False
        self._failed(s, now, fast, why)

    def _failed(self, s: Service, now: float, fast: bool, why: str) -> None:
        # 오래 돌다 끝났으면 연속 빠른 실패를 0 부터 다시 센다(포기는 그 뒤 빠른 실패 max_fast 번), 백오프는 처음 값
        s.fails = s.fails + 1 if fast else 0
        if s.name == "vllm" and fast and s.fails >= self.max_fast:
            s.given_up = True
            log(f"{s.name} {why}: 빨리 끝남이 {s.fails}번 이어져 다시 띄우지 않는다(Tier 0 는 그대로)")
            return
        delay = min(self.backoff_max_s, self.backoff_s * 2 ** min(30, max(0, s.fails - 1)))
        s.next_at = now + delay
        log(f"{s.name} {why} → {delay:.0f}s 뒤 다시 띄운다(연속 빠른 실패 {s.fails}번, Tier 0 는 그대로)")

    # -- 종료

    def _stop(self, s: Service) -> int | None:
        if s.proc is None:
            return None
        s.proc.signal(signal.SIGTERM)
        rc = s.proc.wait(self.grace_s)
        if rc is None:
            log(f"{s.name} 가 {self.grace_s:.0f}s 안에 끝나지 않아 그룹 SIGKILL")
        s.proc.signal(KILL)  # 리더가 끝났어도 그룹에 남은 손자를 정리한다
        if rc is None:
            rc = s.proc.wait(10)
        s.proc = None
        return rc

    def _stop_sidecars(self) -> None:
        for name in ("ppe", "avatar", "worker", "vllm"):  # 아바타·PPE 도 Tier 0 전에 정리한다
            if name in self.svc:
                self._stop(self.svc[name])

    def start_tier0(self) -> None:
        clear_clean_stop(self.marker)  # 엔진 준비 중을 정상 종료 뒤로 보지 않게(S15P31A101-403)
        t0 = self.svc["tier0"]
        self.t0_since_ms = self.wall_ms()  # 띄우기 전에 잰다: 이 시각보다 먼저 쓴 표시 파일은 이번 Tier 0 의 것이 아니다
        t0.proc = self.spawn("tier0", t0.argv, t0.env, self.log_dir)
        self.t0_pid = t0.proc.pid
        t0.started = self.clock()

    def run(self) -> int:
        t0 = self.svc["tier0"]
        try:
            self.start_tier0()
        except OSError as e:
            log(f"Tier 0 를 띄우지 못함: {type(e).__name__}: {e}")
            return EXIT_NOT_FOUND
        log(f"tier0 시작 pid {t0.proc.pid}: {shlex.join(t0.argv)} (이어서 {', '.join(self.services[1:])})")
        log(self.cpu_note)
        if self.worker_id_note:
            log(self.worker_id_note)
        while True:
            if self.stop_signal is not None:
                log(f"신호 {self.stop_signal} → 워커 → vLLM → Tier 0 순서로 내린다")
                self._stop_sidecars()
                rc = self._stop(t0)
                self._record_pod_marker()
                return exit_code(rc) if rc is not None else 128 + int(self.stop_signal)
            rc = t0.proc.poll()
            if rc is not None:
                self._record_pod_marker()
                log(f"Tier 0 종료(코드 {exit_code(rc)}) → 워커·vLLM 을 내리고 같은 코드로 끝낸다")
                self._stop_sidecars()
                t0.proc.signal(KILL)
                return exit_code(rc)
            self.step()
            self.sleep(self.tick_s)


# ---------------------------------------------------------------- 장면 준비 모드 (S15P31A101-250)


def _open_lock(root: Path, rel: tuple[str, ...]):
    path = root.joinpath(*rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    return open(path, "a+b")  # noqa: SIM115 - 잠금을 쥔 동안 연 채로 둔다(닫으면 풀린다)


def _try_lock(f, shared: bool = False) -> bool:
    """기다리지 않는 잠금. POSIX flock(공유·배타), Windows(노트북 시험) msvcrt 는 배타만(공유 요청도 배타로)."""
    try:
        import fcntl
    except ImportError:
        fcntl = None
    if fcntl is not None:
        try:
            fcntl.flock(f.fileno(), (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False
    import msvcrt

    try:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


_tier0_claim = None  # Tier 0 시작 잠금(공유)을 쥔 파일. exec 를 지나 Tier 0 프로세스가 끝날 때까지 쥔다


def release_tier0_claim() -> None:
    global _tier0_claim
    if _tier0_claim is not None:
        _tier0_claim.close()
        _tier0_claim = None


def claim_tier0(root: Path) -> None:
    """Tier 0 시작 의도(S15P31A101-250 리뷰 R1): GPU 를 잡기 전에 locks/tier0.lock 공유 잠금을 쥐고, exec 해도 풀리지 않게 상속한다.
    장면 준비 모드는 이 잠금이 쥐어져 있으면 시작하지 않고, 실행 중에 쥐어지면 장면 작업을 끝낸다(Tier 0 우선). 쥐지 못해도 Tier 0 는 막지 않는다."""
    global _tier0_claim
    release_tier0_claim()
    try:
        f = _open_lock(root, TIER0_LOCK)
    except OSError as e:
        log(f"Tier 0 시작 잠금을 열지 못함({e}): 장면 준비 모드가 Tier 0 를 못 볼 수 있다(Tier 0 는 그대로 시작)")
        return
    deadline = time.monotonic() + 5  # 장면 쪽 확인은 배타 잠금을 잠깐만 쥔다
    while not _try_lock(f, shared=True):
        if time.monotonic() > deadline:
            log("Tier 0 시작 잠금을 쥐지 못함(다른 Tier 0?): 잠금 없이 시작한다")
            f.close()
            return
        time.sleep(0.05)
    os.set_inheritable(f.fileno(), True)  # simai-tier0 → python services.tier0 로 exec 해도 그 프로세스가 끝날 때까지 쥔다
    _tier0_claim = f


def clean_stop_path(marker: Path) -> Path:
    """Tier 0 정상 종료 기록 경로(simai_ai.gpu.admission.clean_stop_path 와 같다. 이 스크립트는 ai 패키지 없이도 돈다)."""
    return marker.with_name(marker.name + CLEAN_STOP_SUFFIX)


def clear_clean_stop(marker: Path) -> None:
    """Tier 0 시작 경로: GPU 를 잡기 전에 지난 정상 종료 기록을 지운다. 남아 있으면 분석 워커가 엔진 준비 중을 'LIVE 아님'으로 본다."""
    path = clean_stop_path(marker)
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        log(f"Tier 0 정상 종료 기록 {path} 를 지우지 못함({type(e).__name__}): 표시 파일이 생기기 전까지 분석 워커가 LIVE 아님으로 볼 수 있다")


def clean_stop_ok(marker: Path) -> str | None:
    """after-live 를 띄워도 되면 None, 아니면 거부 이유. 표시 파일이 없고 정상 종료 기록이 읽혀야 한다."""
    if marker.exists():
        return f"Tier 0 표시 파일 {marker} 가 있다(관제 중이거나 정상 종료하지 않았다)"
    path = clean_stop_path(marker)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return (f"Tier 0 정상 종료 기록 {path} 이 없다(정상 종료하지 않았거나 이 작업 폴더의 Tier 0 가 아니다). "
                "분석 워커가 LIVE 아님으로 볼 근거가 없어 띄우지 않는다")
    except (OSError, ValueError) as e:
        return f"Tier 0 정상 종료 기록 {path} 를 읽지 못함({type(e).__name__}) → 모름"
    if not isinstance(doc, dict) or doc.get("phase") != CLEAN_STOP_PHASE:
        return f"Tier 0 정상 종료 기록 {path} 형식이 틀렸다 → 모름"
    return None


def wait_scene_released(root: Path) -> None:
    """Tier 0 시작 전: 장면 작업이 돌고 있으면(locks/scene.lock) 그 작업이 선점돼 끝날 때까지 SCENE_PREEMPT_WAIT_S(20초)까지 기다리고,
    남은 그룹을 회수한다(TERM 10초 + 확인 10초, 최악 약 40초). 회수하지 못해도 Tier 0 는 시작한다(Tier 0 가 이긴다, 경고·기록)."""
    try:
        f = _open_lock(root, SCENE_LOCK)
    except OSError:
        return
    with f:  # 잠금 확인 뒤 그룹 회수(아래)는 잠금을 놓고 한다
        deadline = time.monotonic() + SCENE_PREEMPT_WAIT_S
        told = False
        while not _try_lock(f):
            if not told:
                log(f"장면 준비 작업이 돌고 있다 → 그 작업이 끝나기를 최대 {SCENE_PREEMPT_WAIT_S:.0f}s 기다린다(Tier 0 우선, 장면 작업은 스스로 멈춘다)")
                told = True
            if time.monotonic() > deadline:
                log("장면 감시 부모가 시한 안에 끝나지 않았다(멈춤?) → 장면 그룹을 직접 회수한다")
                break
            time.sleep(0.2)
    # 잠금이 풀렸어도(감시 부모 소멸) 시한이 지났어도(감시 부모 정지) 장면 그룹이 살아 있으면 회수한다: 잠금·시한을 VRAM 반환으로 보지 않는다
    reap_scene_group(root)


def _proc_start_token(pid: int) -> str | None:
    """프로세스 시작 표지(pid 재사용 오인 방지): Linux /proc/<pid>/stat 의 starttime, Windows GetProcessTimes 의 생성 시각. 모르면 None."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return None
        try:
            t = [wintypes.FILETIME() for _ in range(4)]
            if not k.GetProcessTimes(h, *(ctypes.byref(x) for x in t)):
                return None
            return f"{t[0].dwHighDateTime}:{t[0].dwLowDateTime}"
        finally:
            k.CloseHandle(h)
    try:
        return Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace").rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def _pid_ns() -> str | None:
    """이 프로세스의 PID namespace 표지(/proc/self/ns/pid 의 링크, 예 'pid:[4026531836]'). Windows(노트북 시험)는 하나뿐이라 'nt'. 모르면 None."""
    if os.name == "nt":
        return "nt"
    try:
        return os.readlink("/proc/self/ns/pid")
    except OSError:
        return None


def _proc_alive(pid: int) -> bool:
    """프로세스가 살아 있는가(직접 조회). 권한이 없어 못 보는 것은 살아 있는 것으로 본다(모르면 끝났다고 하지 않는다)."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k.OpenProcess(0x1000, False, pid)
        if not h:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: 있지만 못 연다. 그 밖(87 등)은 없음
        try:
            code = wintypes.DWORD()
            return not k.GetExitCodeProcess(h, ctypes.byref(code)) or code.value == 259  # STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return _proc_state(pid) != "Z"  # 좀비(부모가 멈춰 거두지 못한 끝난 프로세스)는 GPU·메모리를 쥐지 않는다. 상태를 모르면 살아 있음


def _proc_state(pid: int) -> str | None:
    """Linux /proc/<pid>/stat 의 상태 글자(R·S·Z…). 못 읽으면 None(모름: 부르는 쪽이 살아 있는 것으로 본다)."""
    try:
        return Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace").rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return None


def _group_alive(pgid: int) -> bool:
    """그룹에 끝나지 않은(좀비 아닌) 프로세스가 있는가. Linux 는 /proc 를 훑는다(killpg(0) 은 좀비도 산 것으로 본다).
    항목이 사라짐(ENOENT·ESRCH)만 끝난 것으로 보고, 읽을 권한이 없음(EACCES·EPERM)·그 밖의 오류는 살아 있음(모름)으로 본다.
    /proc 가 없으면 killpg(0). Windows(노트북 시험)는 그 프로세스."""
    if os.name == "nt":
        return _proc_alive(pgid)
    proc = Path("/proc")
    if proc.is_dir():
        try:
            entries = [d for d in proc.iterdir() if d.name.isdigit()]
        except OSError:
            return True
        for d in entries:
            try:
                f = (d / "stat").read_text(encoding="utf-8", errors="replace").rsplit(")", 1)[1].split()
            except (FileNotFoundError, ProcessLookupError):
                continue  # 그새 끝난 프로세스
            except (OSError, IndexError):
                return True  # 못 읽음: 그 프로세스가 이 그룹일 수 있다
            if len(f) > 2 and f[2] == str(pgid) and f[0] != "Z":
                return True
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_group(pgid: int, kill: bool) -> bool:
    """그룹에 신호. 보내지 못하면 False(권한 등). Windows(노트북 시험)는 taskkill /T, 거부되면 그 프로세스만 TerminateProcess."""
    if os.name == "nt":
        if subprocess.run(["taskkill", "/PID", str(pgid), "/T", "/F"], capture_output=True, check=False).returncode == 0:
            return True
        import ctypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k.OpenProcess(0x0001, False, pgid)  # PROCESS_TERMINATE
        if not h:
            return False
        try:
            return bool(k.TerminateProcess(h, 1))
        finally:
            k.CloseHandle(h)
    try:
        os.killpg(pgid, KILL if kill else signal.SIGTERM)
        return True
    except ProcessLookupError:
        return True  # 이미 없다
    except PermissionError:
        return False


def _write_scene_pgid(root: Path, pgid: int) -> bool:
    """장면 그룹 기록(pgid·시작 표지·PID namespace)을 원자적으로 쓴다. 실패하면 False(부르는 쪽이 자식을 시작시키지 않고 끝낸다)."""
    path = root.joinpath(*SCENE_PGID)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"pgid": pgid, "start": _proc_start_token(pgid), "pidns": _pid_ns(),
                                   "written_at": time.time_ns() // 1_000_000}), encoding="utf-8")
        os.replace(tmp, path)  # 원자적으로(반쯤 쓴 파일을 읽지 않게)
        return True
    except OSError as e:
        log(f"장면 그룹 기록 {path} 을 쓰지 못함({e})")
        return False


def _wait_gone(pgid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while _group_alive(pgid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.1)
    return True


def _reap_record(root: Path, pgid: int, ok: bool, why: str | None) -> None:
    now_ms = time.time_ns() // 1_000_000
    _write_record(root, {"bootstrap": "S15P31A101-250", "stage": "tier0-reap-scene", "pgid": pgid, "started_at": now_ms,
                         "finished_at": now_ms, "exit": EXIT_OK if ok else EXIT_BUSY, "error": why})


def reap_scene_group(root: Path) -> None:
    """Tier 0 시작 경로: 장면 그룹 기록이 남아 있고 그 그룹이 살아 있으면(감시 부모 정지·소멸) 직접 SIGTERM → SCENE_STOP_GRACE_S →
    SIGKILL 로 회수하고 사라진 것을 확인한다. 같은 PID namespace 이고 시작 표지가 둘 다 있고 같을 때만 신호를 보낸다(모르면 건드리지 않음).
    회수하지 못하거나 건드리지 않으면 경고와 기록(exit 75)을 남기고 진행한다: Tier 0 가용성이 장면 작업보다 우선이다(의도된 정책,
    docs/design/unified-env.md "3단계"). 통합 이미지의 같은 컨테이너 안 실행이 전제다(다른 컨테이너의 장면 작업은 그 컨테이너가 맡는다)."""
    path = root.joinpath(*SCENE_PGID)
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        pgid, start, ns = int(d["pgid"]), d.get("start"), d.get("pidns")
    except FileNotFoundError:
        return
    except (OSError, ValueError, KeyError, TypeError) as e:
        log(f"경고: 장면 그룹 기록 {path} 을 읽지 못함({type(e).__name__}) → 회수하지 못한다(Tier 0 는 그대로 시작, GPU 를 장면 작업이 쥐고 있을 수 있다)")
        return
    mine = _pid_ns()
    if ns is None or mine is None or ns != mine:  # 다른 컨테이너의 pgid 는 이 /proc·killpg 에서 다른 프로세스다
        why = f"다른 PID namespace(기록 {ns}, 여기 {mine})의 장면 그룹 {pgid}: 신호를 보내지 않는다(기록은 남김)"
        log(f"경고: {why}. 그 컨테이너에서 장면 작업이 GPU 를 쥐고 있을 수 있다 → Tier 0 는 그대로 시작한다(Tier 0 우선)")
        _reap_record(root, pgid, False, why)
        return
    if pgid <= 1 or not _group_alive(pgid):
        path.unlink(missing_ok=True)
        return
    now = _proc_start_token(pgid)
    if start is not None and now is not None and now != start:  # 시작 시각이 알려져 있고 다르다: pid 재사용(원래 그룹은 끝났다)
        log(f"장면 그룹 기록의 pid {pgid} 는 다른 프로세스다(시작 표지 다름): 건드리지 않고 지난 기록을 지운다")
        path.unlink(missing_ok=True)
        return
    if start is None or now is None:  # 리더를 확인할 수 없다(리더만 먼저 끝남·권한·기록 때 조회 실패): 남의 그룹일 수 있다
        why = f"장면 그룹 {pgid} 의 신원을 확인하지 못함(시작 표지 기록 {start}, 지금 {now}): 신호를 보내지 않는다(기록은 남김)"
        log(f"경고: {why} → Tier 0 는 그대로 시작한다(Tier 0 우선, GPU 를 장면 작업이 쥐고 있을 수 있다)")
        _reap_record(root, pgid, False, why)
        return
    log(f"장면 작업 그룹 {pgid} 이 남아 있다(감시 부모 정지·소멸) → Tier 0 가 직접 회수한다(SIGTERM → {SCENE_STOP_GRACE_S:.0f}s → SIGKILL)")
    sent = _signal_group(pgid, kill=False)
    if not _wait_gone(pgid, SCENE_STOP_GRACE_S):
        t = _proc_start_token(pgid)  # KILL 전에 다시 본다. 표지를 못 읽으면(리더가 TERM 으로 끝남·권한) 모르므로 보내지 않는다
        if t is None or t != start:
            log(f"장면 그룹 {pgid} 의 리더를 다시 확인하지 못함(시작 표지 {t}): SIGKILL 을 보내지 않는다")
        else:
            sent = _signal_group(pgid, kill=True) and sent
    gone = _wait_gone(pgid, 10)
    if gone:
        path.unlink(missing_ok=True)
        _reap_record(root, pgid, True, None)
        log(f"장면 작업 그룹 {pgid} 회수함")
    else:
        why = f"장면 그룹 {pgid} 을 회수하지 못함(신호 {'보냄' if sent else '거부됨'})"
        log(f"경고: {why} → Tier 0 는 그대로 시작한다(Tier 0 우선, 의도된 정책). 장면 작업이 GPU 를 쥐고 있을 수 있다: 사람이 확인한다")
        _reap_record(root, pgid, False, why)


def tier0_claimed(root: Path) -> bool | None:
    """Tier 0 시작 잠금을 누가 쥐었는가. 배타 잠금을 잠깐 잡아 본다(잡히면 바로 놓는다). 확인하지 못하면 None(모름)."""
    try:
        f = _open_lock(root, TIER0_LOCK)
    except OSError:
        return None
    with f:
        return not _try_lock(f)  # 닫으면 잡았던 잠금도 풀린다


def gpu_memory_used_mb(smi: list[str]) -> float | None:
    """nvidia-smi 의 사용 중 메모리(MiB, GPU 여러 장이면 가장 큰 값). 읽지 못하면 None(모름)."""
    try:
        r = subprocess.run([*smi, "--query-gpu=memory.used", "--format=csv,noheader,nounits"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60, env=child_env())
        vals = [float(line.strip()) for line in r.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    # nan·inf·음수는 모름(float() 은 'nan' 을 받고, 내장 max 는 NaN 을 비교에서 버린다)
    if r.returncode != 0 or not vals or any(not math.isfinite(v) or v < 0 for v in vals):
        return None
    return max(vals)


def tier0_present(root: Path) -> str | None:
    """Tier 0 가 시작했거나 도는 중인 이유(없으면 None): 시작 잠금, 30초 안의 표시 파일. 모르면 있는 것으로 본다."""
    claimed = tier0_claimed(root)
    if claimed is None:
        return "Tier 0 시작 잠금을 확인하지 못함 → 모름, Tier 0 가 있는 것으로 본다"
    if claimed:
        return f"Tier 0 가 시작했다({root.joinpath(*TIER0_LOCK)} 를 쥐고 있다)"
    marker = Path(os.environ.get("SIMAI_TIER0_MARKER") or root / "state" / "tier0.live")
    try:
        age = time.time() - marker.stat().st_mtime
    except FileNotFoundError:
        return None
    except OSError as e:
        return f"Tier 0 표시 파일 {marker} 를 확인하지 못함({type(e).__name__}) → 모름, LIVE 로 본다"
    if age <= SCENE_MARKER_MAX_AGE_S:  # 미래 시각(음수)도 여기: 시계가 어긋난 새 표시 파일은 LIVE 로 본다
        return f"Tier 0 표시 파일 {marker} 이 {age:.0f}s 전에 갱신됐다(LIVE 서비스가 떠 있다, 상한 {SCENE_MARKER_MAX_AGE_S:.0f}s)"
    return None


def scene_busy(root: Path, smi: list[str] | None, gpu_unknown_ok: bool = False) -> str | None:
    """장면 준비를 시작하면 안 되는 이유(없으면 None). 모르면 거부한다. GPU 조회(최대 60초)를 먼저 하고 Tier 0 확인을 마지막에 한다
    (조회 동안 Tier 0 가 시작했으면 여기서 보이고, 그 뒤에 시작하면 실행 감시가 잡는다).
    gpu_unknown_ok: 준비 전 확인용. 사용 중 메모리를 못 읽으면 여기서 막지 않고 준비의 GPU 단계(없으면 코드 3)에 맡긴다."""
    if smi is not None:
        used = gpu_memory_used_mb(smi)
        if used is None and not gpu_unknown_ok:
            return "GPU 사용 중 메모리를 읽지 못함 → 모름, 시작하지 않는다"
        if used is not None and used > SCENE_MAX_GPU_USED_MB:
            return (f"GPU 사용 중 메모리 {used:.0f} MiB > {SCENE_MAX_GPU_USED_MB} MiB: 다른 프로세스(Tier 0 엔진 빌드·REPLAY·vLLM·워커 등)가 "
                    "GPU 를 쓴다")
    return tier0_present(root)


def _scene_refuse(root: Path, why: str) -> int:
    """거부를 알리고 기록에 75 를 남긴다(준비가 남긴 exit 0 기록 뒤에 추가만, 저장 위치가 있을 때)."""
    log(f"장면 준비 거부: {why} (종료 코드 {EXIT_BUSY})")
    if root.is_dir():
        now = time.time_ns() // 1_000_000
        _write_record(root, {"bootstrap": "S15P31A101-250", "stage": "scene", "set": SCENE_SET, "started_at": now,
                             "finished_at": now, "exit": EXIT_BUSY, "error": _redact(why)})
    return EXIT_BUSY


# 시작 handshake(Codex 3차 R1-d, POSIX): 자식은 표준 입력에서 1바이트('g')를 받은 뒤에만 장면 명령으로 exec 한다(pid·그룹·PDEATHSIG 는 그대로).
# 부모가 그룹 기록을 쓰기 전에 멈추면 자식은 GPU 를 쓰지 않고 기다리고, 부모가 죽으면 EOF 라 시작하지 않고 끝난다.
SCENE_HANDSHAKE = (
    "import os, sys\n"
    "if os.read(0, 1) != b'g':\n"
    "    sys.exit(75)\n"
    "fd = os.open(os.devnull, os.O_RDONLY)\n"
    "os.dup2(fd, 0)\n"
    "os.close(fd)\n"
    "try:\n"
    "    os.execvp(sys.argv[1], sys.argv[1:])\n"
    "except OSError as e:\n"
    "    print(f'simai-bootstrap scene: {sys.argv[1]} 를 실행하지 못함: {e}', file=sys.stderr)\n"
    "    sys.exit(127)\n"
)


def run_scene_child(root: Path, argv: list[str], extra_env: dict[str, str] | None = None) -> int:
    """장면 작업을 새 프로세스 그룹 자식으로 돌리고 SCENE_POLL_S 마다 Tier 0 시작을 본다. Tier 0 가 나타나면 그룹 SIGTERM →
    SCENE_STOP_GRACE_S 뒤 SIGKILL 하고 75(Tier 0 우선). 끝나면 자식 종료 코드 그대로(신호 종료는 128 + 번호). 자식 환경에 HF 토큰 없음."""
    stop: dict = {"sig": None}
    old: dict = {}
    if threading.current_thread() is threading.main_thread():  # 시험은 다른 스레드에서도 부른다(신호 처리는 주 스레드만)
        for sig in (signal.SIGTERM, signal.SIGINT):
            old[sig] = signal.signal(sig, lambda s, _f: stop.update(sig=s))
    env = child_env()
    env.setdefault("SIMAI_ALLOW_WITHOUT_TIER0_MARKER", "1")
    env.setdefault("SIMAI_TIER0_WEIGHTS", str(root / "weights" / "yolo" / "yolo26x-pose.pt"))
    env.setdefault("SIMAI_WM2_MODEL", str(root / "weights" / "hy-world-2.0" / "HY-WorldMirror-2.0"))
    env.setdefault("SIMAI_WORLDMIRROR_MODEL", env["SIMAI_WM2_MODEL"])
    env.setdefault("SIMAI_LAMA_WEIGHTS", str(root / "weights" / "lama" / "big-lama.pt"))
    if extra_env:
        env |= extra_env
    env["SIMAI_ROOT"] = str(root)  # 준비한 가중치 위치는 env 파일보다 우선
    try:
        gated = os.name != "nt"  # Windows(노트북 시험)의 exec 는 pid 를 바꿔 handshake 를 쓰지 않는다(기록을 바로 쓴다)
        try:
            proc = Proc("scene", [sys.executable, "-c", SCENE_HANDSHAKE, *argv] if gated else argv, env, root / "logs",
                        die_with_parent=True, stdin_pipe=gated)
        except OSError as e:
            log(f"장면 작업을 띄우지 못함: {type(e).__name__}: {e}")
            return EXIT_NOT_FOUND
        if not _write_scene_pgid(root, proc.pid):  # 기록이 없으면 감시 부모가 멈췄을 때 Tier 0 가 회수할 수 없다: 시작시키지 않는다
            proc.signal(KILL)
            proc.wait(10)
            return _scene_refuse(root, "장면 그룹 기록을 쓰지 못해 장면 작업을 시작시키지 않았다")
        if gated:
            try:
                proc.p.stdin.write(b"g")
                proc.p.stdin.close()
            except OSError as e:  # 자식이 그새 끝났다(아래 wait 가 종료 코드를 본다)
                log(f"장면 작업 시작 신호를 보내지 못함: {e}")
        log(f"장면 작업 시작 pid {proc.pid}: {shlex.join(argv)} (Tier 0 가 시작하면 멈춘다)")
        while True:
            rc = proc.wait(SCENE_POLL_S)
            if rc is not None:
                proc.signal(KILL)  # 그룹에 남은 손자 정리(GPU 메모리를 남기지 않게)
                return exit_code(rc)
            why = tier0_present(root) or (f"신호 {stop['sig']}" if stop["sig"] is not None else None)
            if why is None:
                continue
            log(f"장면 작업을 멈춘다: {why}")
            proc.signal(signal.SIGTERM)
            if proc.wait(SCENE_STOP_GRACE_S) is None:
                log(f"장면 작업이 {SCENE_STOP_GRACE_S:.0f}s 안에 끝나지 않아 그룹 SIGKILL")
            proc.signal(KILL)
            proc.wait(10)
            if stop["sig"] is not None:
                return 128 + int(stop["sig"])
            return _scene_refuse(root, f"실행 중 Tier 0 시작으로 선점됨: {why}")
    finally:
        root.joinpath(*SCENE_PGID).unlink(missing_ok=True)  # 그룹을 정리한 뒤(위 반환 경로는 모두 그룹 KILL 뒤다)
        for sig, h in old.items():
            signal.signal(sig, h)


def _parse_scene_args(rest: list[str]) -> tuple[dict[str, str], list[str]]:
    """scene 모드 앞쪽의 선택 인자(--worker-token-file, --env-file)를 분리하고 비밀 값을 자식 환경 사전에 담는다."""
    extra_env: dict[str, str] = {}
    token_file = os.environ.get("SIMAI_WORKER_TOKEN_FILE")
    env_file = os.environ.get("SIMAI_WORKER_ENV_FILE")
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg == "--":
            i += 1
            break
        if arg == "--worker-token-file":
            if i + 1 >= len(rest):
                raise BootstrapError(EXIT_USAGE, "--worker-token-file 뒤에 경로가 필요하다")
            token_file = rest[i + 1]
            i += 2
            continue
        if arg.startswith("--worker-token-file="):
            token_file = arg.split("=", 1)[1]
            i += 1
            continue
        if arg == "--env-file":
            if i + 1 >= len(rest):
                raise BootstrapError(EXIT_USAGE, "--env-file 뒤에 경로가 필요하다")
            env_file = rest[i + 1]
            i += 2
            continue
        if arg.startswith("--env-file="):
            env_file = arg.split("=", 1)[1]
            i += 1
            continue
        break
    cmd = rest[i:]
    if env_file:
        try:
            raw_lines = Path(env_file).read_text(encoding="utf-8").splitlines()
        except OSError as e:
            raise BootstrapError(EXIT_STORAGE, f"env 파일 {env_file} 를 읽지 못함 ({type(e).__name__})") from e
        for line in raw_lines:
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            key = k.strip().removeprefix("export ").strip()
            val = v.strip().strip("'\"")
            extra_env[key] = val
            if "TOKEN" in key and val:
                _extra_secrets.add(val)
    if token_file:
        extra_env["SIMAI_WORKER_TOKEN"] = read_secret_file(Path(token_file), "SIMAI_WORKER_TOKEN")
    return extra_env, cmd


def scene(rest: list[str], *, worker_id: str | None = None) -> int:
    """장면 준비 모드: 장면 잠금 → Tier 0·GPU 확인 → static 가중치 → 재확인 → 자식으로 실행하며 Tier 0 시작을 감시한다
    (종료 코드는 자식의 것, 끝나면 VRAM 을 돌려준다, Tier 0 가 시작하면 장면 작업을 끝내고 75)."""
    try:
        extra_env, cmd = _parse_scene_args(rest)
    except BootstrapError as e:
        log(f"{e} → 시작하지 않는다 (종료 코드 {e.code})")
        return e.code
    if worker_id:
        extra_env[WORKER_ID_ENV] = worker_id
    if not cmd:
        log("사용법: simai-bootstrap scene [--worker-token-file <경로>] [--env-file <경로>] [--] <명령...>")
        return EXIT_USAGE
    root, _source = choose_root()
    smi = shlex.split(os.environ.get("SIMAI_NVIDIA_SMI") or "nvidia-smi")
    try:
        lock = _open_lock(root, SCENE_LOCK)
    except OSError as e:
        log(f"장면 잠금을 열지 못함({e}) → 시작하지 않는다 (종료 코드 {EXIT_STORAGE})")
        return EXIT_STORAGE
    with lock:  # 실행 내내 쥔다: Tier 0 시작 경로가 이 잠금으로 장면 작업이 끝나기를 기다린다
        if not _try_lock(lock):
            return _scene_refuse(root, "다른 장면 준비 작업이 돌고 있다")
        # 받기 전에: Tier 0 와 GPU 사용 중 메모리(엔진 빌드·다른 컨테이너 중에 수 GB 를 받지 않는다)
        if why := scene_busy(root, smi, gpu_unknown_ok=True):
            return _scene_refuse(root, why)
        try:
            prepare(SCENE_SET)
        except BootstrapError as e:
            log(f"{e} → 시작하지 않는다 (종료 코드 {e.code})")
            return e.code
        root = Path(os.environ["SIMAI_ROOT"])
        if why := scene_busy(root, smi):
            return _scene_refuse(root, why)
        log(f"장면 준비 모드: 가중치 {SCENE_SET} 확인, Tier 0·LIVE 없음 → {shlex.join(cmd)}")
        return run_scene_child(root, cmd, extra_env=extra_env)


# ---------------------------------------------------------------- 기동 전 점검 (check / preflight, S15P31A101-382)


def ptrace_capable(status: Path = Path("/proc/self/status")) -> bool | None:  # 시험에서 바꿔 끼운다
    """유효 권한(CapEff)에 CAP_SYS_PTRACE 가 있는가. 못 읽으면(Linux 아님) None."""
    try:
        m = re.search(r"^CapEff:\s*([0-9a-fA-F]+)\s*$", status.read_text(encoding="ascii", errors="replace"), re.MULTILINE)
    except OSError:
        return None
    return None if m is None else bool(int(m.group(1), 16) >> CAP_SYS_PTRACE & 1)


def py_spy_status(which=None, capable=None) -> dict:
    """py-spy 를 쓸 수 있는지 안내한다(S15P31A101-414). RunPod 컨테이너는 SYS_PTRACE 를 줄 수 없어 부착이 Permission Denied 다(10차 세션).
    그건 점검 실패가 아니다: Tier 0 내장 계측(-383 인지 단계 wall/thread_time)으로 GIL·스케줄 대기와 계산을 가른다."""
    path = (which or shutil.which)("py-spy")
    fallback = "내장 계측(-383 인지 단계 wall/thread_time)을 쓴다"
    if not path:
        return {"status": "absent", "note": f"py-spy 가 없다(-389 이전 이미지) → {fallback}"}
    cap = (capable or ptrace_capable)()
    if cap:
        return {"status": "ok", "path": path, "ptrace": True}
    return {"status": "unavailable", "path": path, "ptrace": cap,
            "note": f"컨테이너에 SYS_PTRACE 가 없어 부착할 수 없다(RunPod 는 권한을 줄 수 없음, 실패 아님) → {fallback}"}


def run_check(rest: list[str], *, preflight: bool = False) -> int:
    """가중치·GPU 예산·런타임 의존성을 점검한다. 인자 없는 check 는 기존과 같고, --preflight / --sets 는 하나라도 실패하면 비-0 으로 끝난다."""
    if not rest and not preflight:
        want_set = os.environ.get(SET_ENV) or "tier0"
        try:
            rec = prepare(want_set)
        except BootstrapError as e:
            log(f"{e} → 시작하지 않는다 (종료 코드 {e.code})")
            return e.code
        print(json.dumps(rec, ensure_ascii=False, indent=2))
        return EXIT_OK

    ap = argparse.ArgumentParser(prog="simai-bootstrap check", add_help=False)
    ap.add_argument("--preflight", action="store_true", default=preflight)
    ap.add_argument("--sets", default=None)
    ap.add_argument("--services", default=None)
    ap.add_argument("--require-bins", default=None)
    ap.add_argument("--require-modules", default=None)
    try:
        args, unknown = ap.parse_known_args(rest)
        if unknown:
            raise BootstrapError(EXIT_USAGE, f"check 알 수 없는 인자: {' '.join(unknown)}")
    except BootstrapError as e:
        log(f"{e} → 시작하지 않는다 (종료 코드 {e.code})")
        return e.code

    default_sets = ",".join(PREFLIGHT_SETS) if args.preflight else (os.environ.get(SET_ENV) or "tier0")
    sets = [s.strip() for s in (args.sets if args.sets is not None else default_sets).split(",") if s.strip()]
    if sets == ["all"]:
        sets = list(PREFLIGHT_SETS)
    if not sets:
        log(f"check: 가중치 묶음 목록이 비었다 (종료 코드 {EXIT_USAGE})")
        return EXIT_USAGE
    # preflight 는 lobby 기동 전 점검이라 lobby 와 같은 기본(아바타 포함)으로 예산을 본다. SIMAI_SERVICES(이미지 기본 tier0,vllm,worker)는 보지 않는다(-414)
    default_services = LOBBY_SERVICES if args.preflight else (os.environ.get(SERVICES_ENV) or "tier0")
    services_str = args.services if args.services is not None else default_services
    bins_raw = args.require_bins if args.require_bins is not None else (",".join(PREFLIGHT_BINS) if args.preflight else "")
    mods_raw = args.require_modules if args.require_modules is not None else (",".join(PREFLIGHT_MODULES) if args.preflight else "")
    bins = [b.strip() for b in bins_raw.split(",") if b.strip()]
    modules = [m.strip() for m in mods_raw.split(",") if m.strip()]

    root, source = choose_root()
    min_free = float(os.environ.get("SIMAI_MIN_FREE_GB") or 20)
    manifest = Path(os.environ.get("SIMAI_WEIGHTS_MANIFEST") or DEFAULT_MANIFEST)
    smi = shlex.split(os.environ.get("SIMAI_NVIDIA_SMI") or "nvidia-smi")
    rec: dict = {
        "bootstrap": "S15P31A101-382",
        "stage": "preflight" if args.preflight else "check",
        "set": ",".join(sets),
        "started_at": time.time_ns() // 1_000_000,
        "hf_token": "present" if _hf_token else "absent",
        "sets": {},
    }
    errors: list[str] = []
    code = EXIT_OK
    storage_ok = False
    try:
        try:
            rec["storage"] = prepare_storage(root, source, min_free)
            storage_ok = True
        except BootstrapError as e:
            errors.append(str(e))
            code = code or e.code
        try:
            rec["gpu"] = detect_gpu(smi)
        except BootstrapError as e:
            errors.append(str(e))
            code = code or e.code
        if storage_ok:
            all_weights: list[dict] = []
            for s in sets:
                try:
                    w = ensure_weights(root, load_manifest(manifest, s))
                    rec["sets"][s] = {"status": "ok", "weights": w}
                    all_weights.extend(w)
                except BootstrapError as e:
                    rec["sets"][s] = {"status": "error", "exit": e.code, "error": _redact(str(e))}
                    errors.append(f"weights[{s}]: {e}")
                    code = code or e.code
            rec["weights"] = all_weights
        try:
            svc_list = parse_services(services_str)
            if any(s in svc_list for s in ("vllm", "worker", "avatar", "ppe")):
                rec["gpu_budget"] = compute_gpu_fractions(svc_list, dict(os.environ))
        except BootstrapError as e:
            rec["gpu_budget"] = {"status": "error", "exit": e.code, "error": _redact(str(e))}
            errors.append(f"gpu_budget: {e}")
            code = code or e.code
        if bins or modules:
            bin_status = {b: shutil.which(b) for b in bins}
            mod_status: dict[str, bool] = {}
            for m in modules:
                try:
                    mod_status[m] = importlib.util.find_spec(m) is not None
                except (ImportError, ValueError):
                    mod_status[m] = False
            rec["runtime"] = {"bins": bin_status, "modules": mod_status}
            missing_bins = [b for b, p in bin_status.items() if not p]
            missing_mods = [m for m, ok in mod_status.items() if not ok]
            if missing_bins:
                errors.append(f"필수 실행 파일 없음: {', '.join(missing_bins)}")
                code = code or EXIT_STORAGE
            if missing_mods:
                errors.append(f"필수 파이썬 모듈 없음: {', '.join(missing_mods)}")
                code = code or EXIT_STORAGE
        wm2_repo = os.environ.get("SIMAI_WM2_REPO")
        if "static" in sets and wm2_repo:
            wm2_dir = Path(wm2_repo) / "hyworld2" / "worldrecon"
            wm2_ok = wm2_dir.is_dir()
            rec.setdefault("runtime", {})["wm2_repo"] = {"path": wm2_repo, "ok": wm2_ok}
            if not wm2_ok:
                errors.append(f"SIMAI_WM2_REPO 경로 없음: {wm2_dir}")
                code = code or EXIT_STORAGE
        if args.preflight:  # 진단 도구 안내(실패로 세지 않는다, -414)
            spy = py_spy_status()
            rec["diagnostics"] = {"py_spy": spy}
            log(f"py-spy: {spy['status']}" + (f" — {spy['note']}" if spy.get("note") else ""))
        rec["exit"] = code
        if errors:
            rec["error"] = _redact("; ".join(errors))
    finally:
        rec["finished_at"] = time.time_ns() // 1_000_000
        if storage_ok or root.is_dir():
            _write_record(root, rec)
    if storage_ok and "gpu" in rec:
        os.environ["SIMAI_ROOT"] = str(root)
        os.environ["SIMAI_GPU_ARCH"] = rec["gpu"]["arch"]
    print(json.dumps(rec, ensure_ascii=False, indent=2))
    if code != EXIT_OK:
        log(f"점검 실패: {rec.get('error', '')} (종료 코드 {code})")
    return code


# ---------------------------------------------------------------- 로비 LIVE 통합 기동 (lobby, S15P31A101-382)

_PEM_CERT_RE = re.compile(rb"-----BEGIN CERTIFICATE-----(.+?)-----END CERTIFICATE-----", re.S)


def _der_tlv(buf: bytes, i: int) -> tuple[int, int, int]:
    if i + 2 > len(buf):
        raise ValueError("DER 가 잘렸다")
    tag, length = buf[i], buf[i + 1]
    i += 2
    if length & 0x80:
        n = length & 0x7F
        if n == 0 or n > 4 or i + n > len(buf):
            raise ValueError("DER 길이가 올바르지 않다")
        length = int.from_bytes(buf[i : i + n], "big")
        i += n
    end = i + length
    if end > len(buf):
        raise ValueError("DER 가 잘렸다")
    return tag, i, end


def spki_sha256_from_pem_file(cert_path: Path) -> str:
    """인증서(PEM)에서 SubjectPublicKeyInfo(DER) SHA-256 지문(`sha256:<64 hex>`)을 계산한다. 비밀 키 파일은 거부한다."""
    try:
        pem = cert_path.read_bytes()
    except OSError as e:
        raise BootstrapError(EXIT_STORAGE, f"TLS 인증서 {cert_path} 를 읽지 못함 ({type(e).__name__})") from e
    if b"PRIVATE KEY" in pem:
        raise BootstrapError(EXIT_USAGE, f"인증서 파일 {cert_path} 에 비밀 키가 섞여 있다")
    m = _PEM_CERT_RE.search(pem)
    if m is None:
        raise BootstrapError(EXIT_STORAGE, f"인증서 파일 {cert_path} 에 PEM BEGIN CERTIFICATE 가 없다")
    try:
        cert_der = base64.b64decode(b"".join(m.group(1).split()), validate=True)
        tag, start, end = _der_tlv(cert_der, 0)
        if tag != 0x30 or end != len(cert_der):
            raise ValueError("X.509 인증서가 아니다")
        tag, i, _ = _der_tlv(cert_der, start)
        if tag != 0x30:
            raise ValueError("tbsCertificate 가 없다")
        tag, _, field_end = _der_tlv(cert_der, i)
        if tag == 0xA0:
            i = field_end
        for name in ("serialNumber", "signature", "issuer", "validity", "subject"):
            tag, _, field_end = _der_tlv(cert_der, i)
            if (name == "serialNumber" and tag != 0x02) or (name != "serialNumber" and tag != 0x30):
                raise ValueError(f"tbsCertificate.{name} 형식이 아니다")
            i = field_end
        tag, spki_start, spki_end = _der_tlv(cert_der, i)
        if tag != 0x30:
            raise ValueError("subjectPublicKeyInfo 가 없다")
        alg_tag, _, alg_end = _der_tlv(cert_der, spki_start)
        key_tag, _, key_end = _der_tlv(cert_der, alg_end)
        if alg_tag != 0x30 or key_tag != 0x03 or key_end != spki_end:
            raise ValueError("subjectPublicKeyInfo 형식이 아니다")
        spki_der = cert_der[i:spki_end]
    except (binascii.Error, ValueError) as e:
        raise BootstrapError(EXIT_STORAGE, f"인증서 {cert_path} SPKI 파싱 실패: {e}") from e
    return "sha256:" + hashlib.sha256(spki_der).hexdigest()


def _parse_measured_heights(raw: str) -> dict[str, float]:
    heights: dict[str, float] = {}
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise BootstrapError(EXIT_USAGE, f"--measured-heights 항목 형식 오류(cam=높이): {item!r}")
        k, v = item.split("=", 1)
        try:
            h = float(v.strip())
        except ValueError as e:
            raise BootstrapError(EXIT_USAGE, f"--measured-heights 숫자 오류: {item!r}") from e
        if not k.strip() or not math.isfinite(h) or h <= 0:
            raise BootstrapError(EXIT_USAGE, f"--measured-heights 값 오류: {item!r}")
        heights[k.strip()] = h
    return heights


def prepare_lobby_work(
    root: Path,
    work: Path,
    bundle_dir: Path,
    *,
    services: list[str],
    public_host: str,
    ec2_base_url: str,
    tier0_token_file: Path,
    worker_token_file: Path | None = None,
    worker_env_file: Path | None = None,
    ca_file: Path | None = None,
    scene_version: str = "lobby-s7-v1",
    config_version: str = "cfg-lobby-s9-1",
    gateway_id: str = "tier0-lobby-s9",
    camera_ids: str = "cam1,cam2,cam3",
    measured_heights: str = "cam1=2.49,cam2=2.76,cam3=2.39",
    control_port: int = 19443,
    video_ports: str = "cam1:19444,cam2:19445,cam3:19447",
    avatar_port: int = 19446,
    avatar_channel: str | None = None,
    live_cal_interval_ms: int = 60_000,
    live_cal_buffer_ms: int = 120_000,
    live_cal_timeout_s: float = 120.0,
    no_live_calibration: bool = False,
    clean_background: bool = True,
    base_env: dict[str, str] | None = None,
) -> tuple[Path, dict[str, str]]:
    """로비 LIVE 작업 폴더(endpoint.json, tier0-live.json, calibration.json)와 Supervisor 환경을 조립한다."""
    env = dict(os.environ if base_env is None else base_env)
    if not public_host:
        raise BootstrapError(EXIT_USAGE, "lobby: --public-host 또는 SIMAI_PUBLIC_HOST/RUNPOD_PUBLIC_IP 가 필요하다")
    if not ec2_base_url:
        raise BootstrapError(EXIT_USAGE, "lobby: --ec2-base-url 또는 SIMAI_EC2_BASE_URL 이 필요하다")
    if not tier0_token_file:
        raise BootstrapError(EXIT_USAGE, "lobby: --tier0-token-file 또는 SIMAI_TIER0_TOKEN_FILE 이 필요하다")
    read_secret_file(tier0_token_file, "SIMAI_TIER0_TOKEN")  # 시작 전 존재·형식 검증 + _extra_secrets 등록

    cal_src = bundle_dir / "calibration.json"
    cert = bundle_dir / "tls" / "tier0.crt"
    key = bundle_dir / "tls" / "tier0.key"
    laptop_hash_file = bundle_dir / "laptop_token.sha256"
    for req in (cal_src, cert, key, laptop_hash_file):
        if not req.is_file():
            raise BootstrapError(EXIT_STORAGE, f"lobby 번들 파일이 없다: {req}")
    laptop_hash = laptop_hash_file.read_text(encoding="utf-8").strip()
    if not laptop_hash:
        raise BootstrapError(EXIT_STORAGE, f"lobby 기기 토큰 해시가 비었다: {laptop_hash_file}")
    spki = spki_sha256_from_pem_file(cert)

    cams = [c.strip() for c in camera_ids.split(",") if c.strip()]
    if not cams:
        raise BootstrapError(EXIT_USAGE, "lobby: --camera-ids 가 비었다")
    heights = _parse_measured_heights(measured_heights)

    for sub in ("state", "bootstrap", "logs"):
        (work / sub).mkdir(parents=True, exist_ok=True)
    cal_dst = work / "calibration.json"
    shutil.copyfile(cal_src, cal_dst)

    endpoint_doc = {
        "tls_public_key_sha256": spki,
        "public_host": public_host,
        "control_port": control_port,
        "video_ports": video_ports,
        "avatar_port": avatar_port,
        "scene_version": scene_version,
        "config_version": config_version,
    }
    (work / "endpoint.json").write_text(json.dumps(endpoint_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")

    cfg = {
        "gateway_id": gateway_id,
        "state_dir": str(work / "state"),
        "scene_version": scene_version,
        "calibration_file": str(cal_dst),
        "input": {"mode": "live", "camera_ids": cams, "decoder": "cpu", "max_queue_wait_ms": 150, "queue_depth": 1},
        "bootstrap": {"archive": str(work / "bootstrap"), "config_version": config_version},
        "tick_ms": 100,
        "live_calibration": {
            "enabled": not no_live_calibration,
            "interval_ms": live_cal_interval_ms,
            "timeout_s": live_cal_timeout_s,
            "buffer_duration_ms": live_cal_buffer_ms,
            "measured_camera_heights": heights,
            "safety_enabled": True,
        },
        "clean_background": {
            "enabled": bool(clean_background),
        },
    }
    cfg_path = work / "tier0-live.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")

    resolved_worker_env: str | None = None
    if worker_env_file is not None:
        if not worker_env_file.is_file():
            raise BootstrapError(EXIT_STORAGE, f"워커 env 파일이 없다: {worker_env_file}")
        resolved_worker_env = str(worker_env_file)
    elif worker_token_file is not None:
        resolved_worker_env = str(ensure_worker_env_file(worker_token_file, work / ".private" / "worker.env"))

    env["SIMAI_ROOT"] = str(root)
    env["SIMAI_TIER0_ID"] = gateway_id
    env["SIMAI_TIER0_MARKER"] = str(work / "state" / "tier0.live")
    # Tier 0 상태 폴더(tier0-live.json state_dir 와 같다). 워커가 이 값이 없으면 $SIMAI_ROOT/state 를 봐서 site_setup 배경 묶음·
    # 활성 보정·scene_regenerate 요청이 Tier 0 와 어긋났다(after-live 는 이미 넣는다, S15P31A101-473)
    env["SIMAI_STATE_DIR"] = str(work / "state")
    env["SIMAI_TIER0_TLS_CERT_FILE"] = str(cert)
    env["SIMAI_TIER0_TLS_KEY_FILE"] = str(key)
    env["SIMAI_LAPTOP_TOKEN_SHA256"] = laptop_hash
    env["SIMAI_PUBLIC_HOST"] = public_host
    env["SIMAI_TIER0_CONTROL_PORT"] = str(control_port)
    env.setdefault(f"SIMAI_PUBLIC_PORT_{control_port}", str(control_port))
    env["SIMAI_TIER0_VIDEO_PORTS"] = video_ports
    for item in video_ports.split(","):
        if ":" in item:
            _, p_str = item.rsplit(":", 1)
            p_str = p_str.strip()
            if p_str.isdigit():
                env.setdefault(f"SIMAI_PUBLIC_PORT_{p_str}", p_str)
    if "avatar" in services:
        env["SIMAI_AVATAR_PORT"] = str(avatar_port)
        env.setdefault(f"SIMAI_PUBLIC_PORT_{avatar_port}", str(avatar_port))
        env["SIMAI_AVATAR_CHANNEL"] = avatar_channel or f"wss://127.0.0.1:{avatar_port}"
    env["SIMAI_EC2_BASE_URL"] = ec2_base_url
    env["SIMAI_TIER0_TOKEN_FILE"] = str(tier0_token_file)
    if resolved_worker_env:
        env["SIMAI_WORKER_ENV_FILE"] = resolved_worker_env
    if ca_file:
        if not ca_file.is_file():
            raise BootstrapError(EXIT_STORAGE, f"CA 인증서 파일이 없다: {ca_file}")
        env["SIMAI_EC2_CA_FILE"] = str(ca_file)
        env["SSL_CERT_FILE"] = str(ca_file)
    env.setdefault("SIMAI_TIER0_ENGINE", str(root / "engines" / "yolo26x-pose-fp16-1280.engine"))
    env.setdefault("SIMAI_TIER0_WEIGHTS", str(root / "weights" / "yolo" / "yolo26x-pose.pt"))
    return cfg_path, env


def _warn_lobby_services(given: str | None, env_value: str | None, services: list[str]) -> None:
    """lobby 는 SIMAI_SERVICES 를 따르지 않는다(S15P31A101-414). 통합 이미지 ENV 기본값이 아바타 없는 tier0,vllm,worker 라
    템플릿이 일부러 뺀 것과 구분할 수 없고, 10차 세션에서 그 값이 lobby 기본값을 덮어 시연 필수인 아바타가 빠졌다. 빼려면 --services 로 명시한다."""
    if given is None and env_value and parse_services_quiet(env_value) != services:
        log(f"주의: {SERVICES_ENV}={env_value} 는 lobby 에서 쓰지 않는다(tier0 감시 모드 전용) → {','.join(services)} 를 띄운다. "
            f"줄이려면 --services 로 명시한다")
    if "avatar" not in services:
        log(f"주의: 아바타를 띄우지 않는다(--services {given}). 시연에는 아바타가 필요하다")


def parse_services_quiet(text: str) -> list[str] | None:
    try:
        return parse_services(text)
    except BootstrapError:
        return None


def lobby(rest: list[str]) -> int:
    """로비 LIVE 통합 기동: Tier 0·vLLM·worker·avatar 를 단일 Supervisor 로 띄우고 Tier 0 종료 코드를 그대로 돌려준다."""
    ap = argparse.ArgumentParser(prog="simai-bootstrap lobby", description="로비 LIVE 통합 기동 (Tier 0 + vLLM + worker + avatar)")
    ap.add_argument("--work", type=Path, default=None)
    ap.add_argument("--bundle-dir", type=Path, required=True)
    ap.add_argument("--public-host", default=None)
    ap.add_argument("--ec2-base-url", default=None)
    ap.add_argument("--tier0-token-file", type=Path, default=None)
    ap.add_argument("--worker-token-file", type=Path, default=None)
    ap.add_argument("--worker-env-file", type=Path, default=None)
    ap.add_argument("--ca-file", type=Path, default=None)
    ap.add_argument("--scene-version", default="lobby-s7-v1")
    ap.add_argument("--config-version", default="cfg-lobby-s9-1")
    ap.add_argument("--gateway-id", default=None)
    ap.add_argument("--camera-ids", default="cam1,cam2,cam3")
    ap.add_argument("--measured-heights", default="cam1=2.49,cam2=2.76,cam3=2.39")
    ap.add_argument("--control-port", type=int, default=None)
    ap.add_argument("--video-ports", default=None)
    ap.add_argument("--avatar-port", type=int, default=None)
    ap.add_argument("--avatar-channel", default=None)
    ap.add_argument("--live-cal-interval-ms", type=int, default=60_000)
    ap.add_argument("--live-cal-buffer-ms", type=int, default=120_000)
    ap.add_argument("--live-cal-timeout-s", type=float, default=120.0)
    ap.add_argument("--no-live-calibration", action="store_true")
    clean_default = (os.environ.get("SIMAI_CLEAN_BACKGROUND") or "1").strip().lower() not in ("0", "false", "no", "off")
    ap.add_argument("--clean-background", dest="clean_background", action="store_true", default=clean_default)
    ap.add_argument("--no-clean-background", dest="clean_background", action="store_false")
    ap.add_argument("--services", default=None,
                    help=f"띄울 프로세스(기본 {LOBBY_SERVICES}). 환경 변수 {SERVICES_ENV} 는 보지 않는다(tier0 감시 모드 전용)")
    ap.add_argument("--worker-id", default=None,
                    help=f"EC2 에 등록한 워커 ID. 없으면 {WORKER_ID_ENV} → 토큰 파일 안의 {WORKER_ID_ENV} → 토큰 파일 이름(worker-s10.token → worker-s10)")
    try:
        args = ap.parse_args(rest)
    except SystemExit as e:
        return EXIT_USAGE if e.code else EXIT_OK

    try:
        services = parse_services(args.services or LOBBY_SERVICES)
        if args.worker_id:  # 형식 오류는 가중치를 받기 전에
            resolve_worker_id(args.worker_id, {})
    except BootstrapError as e:
        log(f"{e} → 시작하지 않는다 (종료 코드 {e.code})")
        return e.code
    _warn_lobby_services(args.services, os.environ.get(SERVICES_ENV), services)

    claim_root = choose_root()[0]
    claim_tier0(claim_root)
    clear_clean_stop((args.work or (claim_root / "runs" / "lobby-live")) / "state" / "tier0.live")
    try:
        rec = prepare("tier0")
    except BootstrapError as e:
        log(f"{e} → 시작하지 않는다 (종료 코드 {e.code})")
        return e.code

    root = Path(os.environ["SIMAI_ROOT"])
    if len(services) > 1 and rec["gpu"]["memory_total_mb"] < MIN_WORKER_VRAM_MB:
        log(f"GPU VRAM {rec['gpu']['memory_total_mb']:.0f} MiB < 48GB급({MIN_WORKER_VRAM_MB} MiB): 분석 부하를 같은 GPU 에 올리지 않는다 "
            f"→ Tier 0 단독 ({','.join(services)} 무시, 명세 3.12)")
        services = ["tier0"]
    if len(services) > 1 and rec["gpu"]["memory_total_mb"] < 76000:
        log("주의: 80GB 미만 GPU 의 동시 VLM·MapAnything 예산은 검증되지 않았다; OOM 가능")
    wait_scene_released(root)

    work = args.work or (root / "runs" / "lobby-live")
    public_host = args.public_host or os.environ.get("SIMAI_PUBLIC_HOST") or os.environ.get("RUNPOD_PUBLIC_IP") or ""
    ec2_base_url = args.ec2_base_url or os.environ.get("SIMAI_EC2_BASE_URL") or ""
    tier0_token_file = args.tier0_token_file or (Path(os.environ["SIMAI_TIER0_TOKEN_FILE"]) if os.environ.get("SIMAI_TIER0_TOKEN_FILE") else None)
    worker_token_file = args.worker_token_file or (Path(os.environ["SIMAI_WORKER_TOKEN_FILE"]) if os.environ.get("SIMAI_WORKER_TOKEN_FILE") else None)
    worker_env_file = args.worker_env_file or (Path(os.environ["SIMAI_WORKER_ENV_FILE"]) if os.environ.get("SIMAI_WORKER_ENV_FILE") else None)
    ca_file = args.ca_file or (Path(os.environ["SIMAI_EC2_CA_FILE"]) if os.environ.get("SIMAI_EC2_CA_FILE") else None)
    gateway_id = args.gateway_id or os.environ.get("SIMAI_TIER0_ID") or "tier0-lobby-s9"
    control_port = args.control_port if args.control_port is not None else int(os.environ.get("SIMAI_TIER0_CONTROL_PORT") or 19443)
    video_ports = args.video_ports or os.environ.get("SIMAI_TIER0_VIDEO_PORTS") or "cam1:19444,cam2:19445,cam3:19447"
    avatar_port = args.avatar_port if args.avatar_port is not None else int(os.environ.get("SIMAI_AVATAR_PORT") or 19446)
    avatar_channel = args.avatar_channel or os.environ.get("SIMAI_AVATAR_CHANNEL")

    try:
        worker_id = resolve_worker_id(args.worker_id, dict(os.environ), worker_token_file, worker_env_file)
        cfg_path, lobby_env = prepare_lobby_work(
            root,
            work,
            args.bundle_dir,
            services=services,
            public_host=public_host,
            ec2_base_url=ec2_base_url,
            tier0_token_file=tier0_token_file,  # type: ignore[arg-type]
            worker_token_file=worker_token_file,
            worker_env_file=worker_env_file,
            ca_file=ca_file,
            scene_version=args.scene_version,
            config_version=args.config_version,
            gateway_id=gateway_id,
            camera_ids=args.camera_ids,
            measured_heights=args.measured_heights,
            control_port=control_port,
            video_ports=video_ports,
            avatar_port=avatar_port,
            avatar_channel=avatar_channel,
            live_cal_interval_ms=args.live_cal_interval_ms,
            live_cal_buffer_ms=args.live_cal_buffer_ms,
            live_cal_timeout_s=args.live_cal_timeout_s,
            no_live_calibration=args.no_live_calibration,
            clean_background=args.clean_background,
        )
        tier0_argv = [*shlex.split(lobby_env.get("SIMAI_TIER0_CMD") or TIER0_CMD), "--config", str(cfg_path)]
        sup = Supervisor(root, services, tier0_argv, env=lobby_env, pod_jsonl=work / "pod.jsonl", worker_id=worker_id)
    except BootstrapError as e:
        log(f"{e} → 시작하지 않는다 (종료 코드 {e.code})")
        return e.code
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, sup.request_stop)
    return sup.run()


def after_live(rest: list[str]) -> int:
    """관제 뒤 배경 복원(S15P31A101-403): 로비 작업 폴더의 Tier 0 정상 종료 기록을 확인하고 장면 준비 모드로 분석 워커만 띄운다."""
    ap = argparse.ArgumentParser(prog="simai-bootstrap after-live",
                                 description="관제 뒤 사람 지운 배경 묶음 static_reconstruct 자동 제출·처리(분석 워커만)")
    ap.add_argument("--work", type=Path, default=None, help="로비 작업 폴더(기본 $SIMAI_ROOT/runs/lobby-live)")
    ap.add_argument("--worker-token-file", default=None)
    ap.add_argument("--worker-id", default=None)
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--ec2-base-url", default=None)
    if "--" in rest:
        i = rest.index("--")
        own, worker_args = rest[:i], rest[i + 1:]
    else:
        own, worker_args = rest, []
    try:
        args = ap.parse_args(own)
    except SystemExit as e:
        return EXIT_USAGE if e.code else EXIT_OK
    if "--once" in worker_args:
        log("after-live 의 워커는 --once 없이 돈다(관제 뒤 대기 시간을 채운다) (종료 코드 64)")
        return EXIT_USAGE
    root = choose_root()[0]
    work = args.work or (root / "runs" / "lobby-live")
    state = work / "state"
    marker = state / "tier0.live"
    if why := clean_stop_ok(marker):
        return _scene_refuse(root, f"after-live: {why}")
    ec2 = args.ec2_base_url or os.environ.get("SIMAI_EC2_BASE_URL") or ""
    if not ec2:
        log("after-live: --ec2-base-url 또는 SIMAI_EC2_BASE_URL 이 필요하다 (종료 코드 64)")
        return EXIT_USAGE
    token_file = args.worker_token_file or os.environ.get("SIMAI_WORKER_TOKEN_FILE")
    env_file = args.env_file or os.environ.get("SIMAI_WORKER_ENV_FILE")
    try:
        worker_id, source = resolve_worker_id(args.worker_id, dict(os.environ),
                                              Path(token_file) if token_file else None, Path(env_file) if env_file else None)
    except BootstrapError as e:
        log(f"{e} → 시작하지 않는다 (종료 코드 {e.code})")
        return e.code
    if worker_id:
        os.environ[WORKER_ID_ENV] = worker_id
        log(f"워커 ID {worker_id} ({source}) → worker 환경 {WORKER_ID_ENV}")
    else:
        log(f"주의: 워커 ID 를 정하지 못함({source}) → 워커 기본 worker-01, EC2 ID 와 다르면 401")
    # 장면 모드의 Tier 0 확인(tier0_present)과 자식 환경이 같은 표시 파일·상태 폴더를 보게 한다
    os.environ.update({"SIMAI_TIER0_MARKER": str(marker), "SIMAI_STATE_DIR": str(state), "SIMAI_EC2_BASE_URL": ec2,
                       "SIMAI_AUTO_STATIC_RECONSTRUCT": "1", "SIMAI_AUTO_STATIC_EXIT_WHEN_DONE": "1"})
    os.environ.pop("SIMAI_WORKER_DEV_NO_LIVE", None)  # 개발 플래그가 아니라 정상 종료 기록으로 LIVE 아님을 본다
    os.environ.pop("SIMAI_WORKER_TORCH_MEMORY_FRACTION", None)  # vLLM 이 없으니 분석 워커 예산 전체(기본 0.70)
    scene_args: list[str] = []
    if args.worker_token_file:
        scene_args += ["--worker-token-file", args.worker_token_file]
    if args.env_file:
        scene_args += ["--env-file", args.env_file]
    log(f"after-live: {work} 의 Tier 0 정상 종료 확인 → 분석 워커만 띄운다(배경 묶음 자동 제출·처리 뒤 끝남)")
    return scene([*scene_args, "--", sys.executable, "-m", "services.worker", *worker_args], worker_id=worker_id)


def supervise(rec: dict, services: list[str], rest: list[str]) -> int:
    root = Path(os.environ["SIMAI_ROOT"])
    tier0 = [*shlex.split(os.environ.get("SIMAI_TIER0_CMD") or TIER0_CMD), *rest]
    try:
        sup = Supervisor(root, services, tier0)
    except BootstrapError as e:
        log(f"{e} → 시작하지 않는다 (종료 코드 {e.code})")
        return e.code
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, sup.request_stop)
    return sup.run()


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    take_token()  # 어떤 분기·자식 실행보다 먼저
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] in (["-h"], ["--help"]):
        print(__doc__, file=sys.stderr)
        execvp([TIER0_CMD, "--help"])
        return EXIT_OK
    if not args:
        # 인자 없음은 기동하지 않는다(S15P31A101-414): 10차 세션에서 SSH 셸의 맨 `simai-bootstrap` 이 예전 교실 기본값으로 Tier 0 를 띄웠다.
        # 이미지 기본은 CMD ["tier0"] 로 명시하고 RunPod 시작 명령은 `sleep infinity` 등을 넘기므로 컨테이너 시작 경로는 그대로다
        print(__doc__.split("\n\n", 1)[0], file=sys.stderr)
        log(f"인자가 없어 아무것도 띄우지 않는다. Tier 0 는 'simai-bootstrap tier0', 로비 LIVE 는 'simai-bootstrap lobby --help', 전체 설명은 --help (종료 코드 {EXIT_USAGE})")
        return EXIT_USAGE
    if args[0].startswith("-"):
        args = ["tier0", *args]
    mode, rest = args[0], args[1:]
    if mode == "scene":
        return scene(rest)
    if mode == "lobby":
        return lobby(rest)
    if mode == "after-live":
        return after_live(rest)
    if mode == "check":
        return run_check(rest, preflight=False)
    if mode == "preflight":
        return run_check(rest, preflight=True)
    if mode not in ("tier0", "queue", "run"):
        execvp(args)  # 준비 없이 그대로 (sleep infinity 로 띄운 개발 Pod, 셸, 점검 도구)
        return EXIT_OK
    if mode == "run" and not rest:
        log("사용법: simai-bootstrap run <명령...>")
        return EXIT_USAGE
    services = ["tier0"]
    if mode == "tier0":
        try:  # 가중치를 받기 전에 설정 오타를 잡는다
            services = parse_services(os.environ.get(SERVICES_ENV))
        except BootstrapError as e:
            log(f"{e} → 시작하지 않는다 (종료 코드 {e.code})")
            return e.code
    want_set = "tier0" if mode == "tier0" else (os.environ.get(SET_ENV) or "tier0")
    if mode == "tier0":  # GPU 를 잡기 전에 시작 의도를 남긴다: 장면 준비 작업은 이것을 보고 시작하지 않거나 스스로 멈춘다(Tier 0 우선)
        claim_root = choose_root()[0]
        claim_tier0(claim_root)
        clear_clean_stop(Path(os.environ.get("SIMAI_TIER0_MARKER") or claim_root / "state" / "tier0.live"))
    try:
        rec = prepare(want_set)
    except BootstrapError as e:
        log(f"{e} → 시작하지 않는다 (종료 코드 {e.code})")
        return e.code
    if mode == "queue":
        repo = Path(os.environ.get("SIMAI_SESSION_REPO") or Path(os.environ["SIMAI_ROOT"]) / "setup" / "session1" / "repo")
        script = repo / "deploy" / "scripts" / "gpu_session1.py"
        if not script.is_file():
            log(f"{script} 가 없다. 노트북에서 gpu_session1.py prepare 로 저장소 사본을 올린다 (docs/design/runpod-environment.md)")
            return EXIT_STORAGE
        execvp([sys.executable, str(script), "run", *rest])
        return EXIT_OK
    if mode == "run":
        execvp(rest)
        return EXIT_OK
    if len(services) > 1 and rec["gpu"]["memory_total_mb"] < MIN_WORKER_VRAM_MB:
        log(f"GPU VRAM {rec['gpu']['memory_total_mb']:.0f} MiB < 48GB급({MIN_WORKER_VRAM_MB} MiB): 분석 부하를 같은 GPU 에 올리지 않는다 "
            f"→ Tier 0 단독 ({SERVICES_ENV}={','.join(services)} 무시, 명세 3.12)")
        services = ["tier0"]
    if len(services) > 1 and rec["gpu"]["memory_total_mb"] < 76000:
        log("주의: 80GB 미만 GPU 의 동시 VLM·MapAnything 예산은 검증되지 않았다; OOM 가능")
    wait_scene_released(Path(os.environ["SIMAI_ROOT"]))  # 장면 작업이 멈춰 VRAM 을 돌려줄 때까지(상한 있음)
    if services == ["tier0"]:
        execvp([TIER0_CMD, *rest])  # Tier 0 단독: 감시 프로세스 없이 지금까지와 같다
        return EXIT_OK
    return supervise(rec, services, rest)


if __name__ == "__main__":
    sys.exit(main())
