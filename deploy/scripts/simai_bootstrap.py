#!/usr/bin/env python3
"""simai-bootstrap: 어느 GPU·지역·사이트(RunPod·Vast.ai·엘리스 GPU VM)에서든 같은 순서로 이미지를 준비한다 (S15P31A101-205·-207·-246·-250).

    simai-bootstrap [tier0] [Tier 0 인자...]   준비 → simai-tier0 (엔진 준비 engine_cache ensure → python -m services.tier0)
                                                SIMAI_SERVICES 에 vllm·worker 가 있으면 감시 프로세스로 Tier 0 → vLLM 서버 → 분석 워커 (아래)
    simai-bootstrap queue [gpu_session1 run 인자...]   준비 → GPU 1차 세션 큐 (저장소 사본 $SIMAI_SESSION_REPO, 기본 $SIMAI_ROOT/setup/session1/repo)
    simai-bootstrap check                       준비만 하고 결과 JSON 을 출력한다
    simai-bootstrap run <명령...>               준비 → 그 명령 (실험 이미지: simai-bootstrap run simai-exp02 python ...)
    simai-bootstrap scene [--] <명령...>        장면 준비 모드(일회성, 통합 이미지의 MapAnything): Tier 0·GPU 확인 → static 가중치 → 재확인 → 그 명령을
                                                자식으로 실행하며 Tier 0 시작을 감시(나타나면 장면 작업을 끝내고 75). 명령의 종료 코드로 끝난다(아래 "장면 준비 모드")
    simai-bootstrap --help                      이 설명 + Tier 0 도움말 (준비 없음)
    simai-bootstrap <다른 명령...>              준비 없이 그대로 실행 (sleep infinity, bash, simai-wait-tier0 등)

준비 순서:
  1) 저장 위치: SIMAI_ROOT 가 있으면 그것, 없으면 /workspace 가 있고 쓸 수 있으면 /workspace/simai(RunPod 볼륨·Pod 볼륨·docker -v),
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
  - vLLM·워커가 죽으면 그 프로세스 그룹을 SIGKILL 로 정리하고(vLLM EngineCore 같은 손자 포함), 지수 백오프 뒤 Tier 0 가 준비 상태일 때만
    다시 띄운다. 빨리 죽는 일이 연달아 SIMAI_SUPERVISE_MAX_FAST_FAILURES(기본 5)번이면 그 프로세스는 포기한다. Tier 0 는 건드리지 않는다.
  - Tier 0 가 끝나면 워커 → vLLM 을 내리고 Tier 0 종료 코드 그대로 끝낸다. SIGTERM·SIGINT 를 받으면 워커 → vLLM → Tier 0 순서로
    그룹에 SIGTERM, SIMAI_STOP_GRACE_S(기본 15)초 안에 안 끝나면 그룹 SIGKILL. docker stop 은 -t 60 이상으로 준다.
  - 출력은 줄마다 [tier0]·[vllm]·[worker] 접두로 표준 오류에, 원문은 $SIMAI_ROOT/logs/<이름>.log 에 덧붙인다.
  - vLLM·워커는 스레드 제한(OMP·MKL·OpenBLAS·NumExpr = SIMAI_SIDECAR_THREADS, 기본 1. torch intra-op 도 OMP 를 따른다)·nice(SIMAI_SIDECAR_NICE, 기본 10)·
    CPU 고정(SIMAI_SIDECAR_CPUS, 기본 auto = vCPU 8개 이상이면 뒤쪽 절반, none·'4-7,10'), 세 프로세스 모두 다른
    TRITON_CACHE_DIR·TORCHINDUCTOR_CACHE_DIR·CUDA_CACHE_PATH(SIMAI_CACHE_ROOT, 기본 /tmp/simai-cache/<이름>/)로 띄운다.
    vLLM 은 표시 파일이 phase=running·reserve OK 일 때만 띄우므로 Tier 0 의 TensorRT 엔진 빌드·적재와 GPU 선확보가 끝난 뒤다(진입점이 엔진을 준비한 뒤에 서비스가 표시 파일을 쓴다). 워커 토큰 파일(SIMAI_WORKER_ENV_FILE, 없으면
    $SIMAI_ROOT/../.simai-private/worker.env 가 있을 때)은 simai-wait-tier0 --env-file 로 워커 환경에만 넣는다.
    워커 인자는 SIMAI_WORKER_ARGS(예: --config <워커 설정>: 받을 작업 유형 VLM·hmr_refine 등의 처리기)를 덧붙인다.

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

HF 토큰: 시작하자마자 HF_TOKEN 을 환경에서 꺼내 이 프로세스 안에만 들고(가중치 받기에만 쓴다), HUGGING_FACE_HUB_TOKEN 은 읽지 않고 지우기만 한다.
그래서 nvidia-smi 조회·도움말·그대로 넘기는 명령·Tier 0·vLLM·워커·큐 등 어떤 자식 프로세스에도 토큰이 상속되지 않는다(S15P31A101-205 리뷰 R1).

종료 코드(simai-tier0·engine_cache 와 같은 규칙): 0 정상, 2 저장 위치·용량·가중치(없음·해시 불일치·받기 실패·목록 오류·묶음 이름 오류),
3 GPU 없음·최소 사양 미만, 64 사용법 오류(run·scene 뒤에 명령 없음, SIMAI_SERVICES 오류), 75 장면 준비 거부(LIVE 서비스·GPU 사용 중·모름, sysexits EX_TEMPFAIL),
127 Tier 0 명령을 띄우지 못함.
감시 프로세스 모드는 Tier 0 의 종료 코드를 그대로 돌려준다(신호로 끝났으면 128 + 신호 번호).
"""

from __future__ import annotations

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
_hf_token: str | None = None  # take_token() 이 환경에서 꺼내 둔 값. _request·_redact 만 쓴다
TIER0_CMD = "simai-tier0"
SET_ENV = "SIMAI_WEIGHTS_SET"  # check·queue·run 이 받을 가중치 묶음(이미지 ENV). 없으면 tier0
SERVICES_ENV = "SIMAI_SERVICES"  # tier0 명령이 띄울 프로세스. 없으면 tier0(단독)
WORKER_SET_ENV = "SIMAI_WORKER_WEIGHTS_SET"  # 감시 모드에서 Tier 0 준비 뒤 받는 묶음. 없으면 vlm
SERVICES = ("tier0", "vllm", "worker")  # 기동 순서. 종료는 거꾸로
SCENE_SET = "static"  # 장면 준비 모드의 가중치 묶음(MapAnything 기본·apache)
SCENE_MARKER_MAX_AGE_S = 30.0  # 이 안에 갱신된 Tier 0 표시 파일 = LIVE (gpu_session1 live_guard 의 marker_max_age_s 와 같음)
SCENE_POLL_S = 0.5  # 장면 작업 실행 중 Tier 0 시작 확인 주기
SCENE_STOP_GRACE_S = 10.0  # Tier 0 가 시작하면 장면 그룹 SIGTERM 뒤 SIGKILL 까지
SCENE_PREEMPT_WAIT_S = 20.0  # Tier 0 시작 경로가 장면 잠금 해제를 기다리는 상한(감시 주기 + 시한 + 여유). 그 뒤 남은 그룹 회수(TERM 10초 + 확인 10초)까지
#                               최악 약 40초 늦게 시작한다. 지나면 그대로 시작
TIER0_LOCK = ("locks", "tier0.lock")  # Tier 0 시작 의도(공유 잠금, Tier 0 프로세스가 끝날 때까지). flock 은 커널이 프로세스 종료 때 푼다
SCENE_LOCK = ("locks", "scene.lock")  # 장면 작업 실행 중(배타)
SCENE_PGID = ("locks", "scene.pgid")  # 실행 중인 장면 그룹(pgid·시작 표지). 감시 부모가 멈추거나 죽어도 Tier 0 가 이것으로 회수한다
SCENE_MAX_GPU_USED_MB = 1024  # 넘으면 다른 프로세스가 GPU 를 쓴다. 빈 GPU 는 0~수 MiB, 다른 프로세스의 빈 CUDA 문맥은 약 0.5 GiB, Tier 0·vLLM 은 수 GiB 이상
FETCH_ATTEMPTS = 5
RETRY_HTTP = {408, 425, 429, 500, 502, 503, 504}
LOCK_DIR = ("weights", ".locks")  # 항목마다 지우지 않는 잠금 파일(Codex 리뷰 R1)
STAMP_DIR = ("weights", ".verified")  # 모델 폴더 밖(vLLM 이 읽는 weights/cosmos3-nano/ 에 섞이지 않게), Tier 0 state/ 밖
VLLM_HF_OVERRIDES = '{"architectures": ["Cosmos3ReasonerForConditionalGeneration"]}'  # Reasoner 탑만 싣는다(docs/design/unified-env.md)
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
    return {k: v for k, v in os.environ.items() if k not in TOKEN_ENVS}


def _redact(text: str) -> str:
    for tok in {_hf_token, *(os.environ.get(k) for k in TOKEN_ENVS)}:
        if tok:
            text = text.replace(tok, "***")
    return re.sub(r"hf_[A-Za-z0-9]{20,}", "hf_***", text)


def log(msg: str) -> None:
    with _out_lock:
        print(f"simai-bootstrap: {_redact(msg)}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 저장 위치


def choose_root(env: dict | None = None, workspace: Path | None = None, container: Path | None = None) -> tuple[Path, str]:
    """(SIMAI_ROOT, 고른 이유 env|workspace|container)."""
    env = os.environ if env is None else env
    workspace = WORKSPACE if workspace is None else workspace
    container = CONTAINER_ROOT if container is None else container
    if env.get("SIMAI_ROOT"):
        return Path(env["SIMAI_ROOT"]), "env"
    if workspace.is_dir() and os.access(workspace, os.W_OK):
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
        raise BootstrapError(EXIT_USAGE, f"{SERVICES_ENV}={text!r} 오류: tier0 를 넣고 tier0·vllm·worker 중에서 쉼표로 고른다{hint}")
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


def sidecar_cpus(spec: str | None, avail: list[int]) -> set[int] | None:
    """vLLM·워커를 묶을 CPU. auto(기본): 쓸 수 있는 vCPU 가 8개 이상이면 뒤쪽 절반(앞쪽은 Tier 0 의 CPU 디코딩·기하 단계 몫),
    8개 미만이면 묶지 않는다(나누면 둘 다 모자란다). none: 묶지 않음. '4-7,10': 그 목록(쓸 수 있는 것과 겹치는 것만). Tier 0 는 묶지 않는다."""
    spec = (spec or "auto").strip().lower()
    if spec in ("none", "off"):
        return None
    if spec == "auto":
        return set(avail[len(avail) // 2:]) if len(avail) >= 8 else None
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
                 spawn=Proc, ready=None, health=None, clock=time.monotonic, sleep=time.sleep, wall_ms=None) -> None:
        env = dict(os.environ) if env is None else env
        self.root = root
        self.services = services
        self.spawn, self.clock, self.sleep = spawn, clock, sleep
        self.wall_ms = wall_ms or (lambda: time.time_ns() // 1_000_000)
        self.t0_since_ms: int | None = None  # 이번에 띄운 Tier 0 의 기동 시각·pid(표시 파일이 그 뒤의 것인지 본다)
        self.t0_pid: int | None = None
        self.log_dir = root / "logs"
        self.marker = Path(env.get("SIMAI_TIER0_MARKER") or root / "state" / "tier0.live")
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
        self.manifest = Path(env.get("SIMAI_WEIGHTS_MANIFEST") or DEFAULT_MANIFEST)
        if ready is None:
            waiter = _load_waiter()
            ready = lambda: tier0_marker_ready(self.marker, waiter, self.t0_since_ms, self.t0_pid)  # noqa: E731
        self.ready = ready
        self.health = health or (lambda: http_ok(f"http://127.0.0.1:{self.port}/health"))
        base = {k: v for k, v in env.items() if k not in TOKEN_ENVS}
        threads = env.get("SIMAI_SIDECAR_THREADS") or "1"
        self.cpus = sidecar_cpus(env.get("SIMAI_SIDECAR_CPUS"), usable_cpus())
        # 프로세스마다 JIT·커널 캐시를 나눈다(같은 판이어도 동시에 쓰는 경합을 피한다). 컨테이너 디스크(/tmp): 저장 위치에 남길 산출물이 아니다
        cache_root = Path(env.get("SIMAI_CACHE_ROOT") or ("/tmp/simai-cache" if os.name != "nt" else Path(tempfile.gettempdir()) / "simai-cache"))
        self.svc: dict[str, Service] = {}
        for name in services:
            e = dict(base)
            cache = cache_root / name
            e["TRITON_CACHE_DIR"], e["TORCHINDUCTOR_CACHE_DIR"] = str(cache / "triton"), str(cache / "inductor")
            e["CUDA_CACHE_PATH"] = str(cache / "cuda")
            if name != "tier0":  # Tier 0 의 CPU 디코딩·단일 스레드 기하 단계를 보호한다(자문 3-4). torch 의 intra-op 스레드도 OMP_NUM_THREADS 를 따른다
                for k in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
                    e[k] = threads
            if name == "vllm":
                e.setdefault("VLLM_USE_DEEP_GEMM", "0")
                e["HF_HUB_OFFLINE"] = "1"  # 모델은 확인한 로컬 폴더만. 토큰 없이 HF 로 나가지 않는다
            argv = {"tier0": tier0_argv, "vllm": self._vllm_argv(env), "worker": self._worker_argv(env)}[name]
            self.svc[name] = Service(name, argv, e)
        self.phase = "wait_tier0"  # → weights → run | off(워커 쪽 포기)
        self.last_why = ""
        self.stop_signal: int | None = None
        self._weights: threading.Thread | None = None
        self.weights_error: str | None = None

    def _vllm_argv(self, env: dict) -> list[str]:
        model = env.get("SIMAI_VLLM_MODEL") or str(self.root / "weights" / "cosmos3-nano")
        return [*shlex.split(env.get("SIMAI_VLLM_CMD") or "vllm"), "serve", model,
                "--served-model-name", env.get("SIMAI_VLLM_SERVED_NAME") or "cosmos3-nano",
                "--hf-overrides", VLLM_HF_OVERRIDES,
                "--gpu-memory-utilization", env.get("SIMAI_WORKER_VLLM_GPU_MEMORY_UTILIZATION") or "0.55",
                "--max-num-seqs", env.get("SIMAI_WORKER_VLLM_MAX_NUM_SEQS") or "4",
                "--host", "127.0.0.1", "--port", str(self.port),
                "--mm-encoder-tp-mode", "data", "--async-scheduling",
                *shlex.split(env.get("SIMAI_VLLM_EXTRA_ARGS") or "")]

    def _worker_argv(self, env: dict) -> list[str]:
        cmd = shlex.split(env["SIMAI_WORKER_CMD"]) if env.get("SIMAI_WORKER_CMD") else [sys.executable, "-m", "services.worker"]
        # 설정 자리(S15P31A101-246): 받을 작업 유형(VLM·HMR hmr_refine 등)은 워커 설정 파일의 처리기가 정한다(services.worker --config, 조립은 -227 등).
        # 예: SIMAI_WORKER_ARGS="--config /workspace/simai/worker.json"
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
            rec["weights"] = ensure_weights(self.root, load_manifest(self.manifest, self.worker_set))
            rec["exit"] = EXIT_OK
            log(f"워커 가중치 {self.worker_set} 준비: " + ", ".join(f"{w['name']} {w['status']}" for w in rec["weights"]))
        except Exception as e:  # noqa: BLE001 - 워커 가중치 실패는 Tier 0 를 멈추지 않는다
            self.weights_error = _redact(f"{type(e).__name__}: {e}")
            rec |= {"exit": getattr(e, "code", EXIT_STORAGE), "error": self.weights_error}
        finally:
            rec["finished_at"] = time.time_ns() // 1_000_000
            _write_record(self.root, rec)

    def step(self) -> None:
        now = self.clock()
        if self.phase == "wait_tier0" and self._tier0_ready():
            log("Tier 0 준비됨(phase=running, reserve OK) → 워커 쪽 준비")
            if "vllm" in self.svc:
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
            for name in ("vllm", "worker"):
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
        if fast and s.fails >= self.max_fast:
            s.given_up = True
            log(f"{s.name} {why}: 빨리 끝남이 {s.fails}번 이어져 다시 띄우지 않는다(Tier 0 는 그대로)")
            return
        delay = min(self.backoff_max_s, self.backoff_s * 2 ** max(0, s.fails - 1))
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
        for name in ("worker", "vllm"):  # 워커가 vLLM 을 쓰므로 워커 먼저
            if name in self.svc:
                self._stop(self.svc[name])

    def start_tier0(self) -> None:
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
        while True:
            if self.stop_signal is not None:
                log(f"신호 {self.stop_signal} → 워커 → vLLM → Tier 0 순서로 내린다")
                self._stop_sidecars()
                rc = self._stop(t0)
                return exit_code(rc) if rc is not None else 128 + int(self.stop_signal)
            rc = t0.proc.poll()
            if rc is not None:
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


def run_scene_child(root: Path, argv: list[str]) -> int:
    """장면 작업을 새 프로세스 그룹 자식으로 돌리고 SCENE_POLL_S 마다 Tier 0 시작을 본다. Tier 0 가 나타나면 그룹 SIGTERM →
    SCENE_STOP_GRACE_S 뒤 SIGKILL 하고 75(Tier 0 우선). 끝나면 자식 종료 코드 그대로(신호 종료는 128 + 번호). 자식 환경에 HF 토큰 없음."""
    stop: dict = {"sig": None}
    old: dict = {}
    if threading.current_thread() is threading.main_thread():  # 시험은 다른 스레드에서도 부른다(신호 처리는 주 스레드만)
        for sig in (signal.SIGTERM, signal.SIGINT):
            old[sig] = signal.signal(sig, lambda s, _f: stop.update(sig=s))
    try:
        gated = os.name != "nt"  # Windows(노트북 시험)의 exec 는 pid 를 바꿔 handshake 를 쓰지 않는다(기록을 바로 쓴다)
        try:
            proc = Proc("scene", [sys.executable, "-c", SCENE_HANDSHAKE, *argv] if gated else argv, child_env(), root / "logs",
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


def scene(rest: list[str]) -> int:
    """장면 준비 모드: 장면 잠금 → Tier 0·GPU 확인 → static 가중치 → 재확인 → 자식으로 실행하며 Tier 0 시작을 감시한다
    (종료 코드는 자식의 것, 끝나면 VRAM 을 돌려준다, Tier 0 가 시작하면 장면 작업을 끝내고 75)."""
    if rest[:1] == ["--"]:
        rest = rest[1:]
    if not rest:
        log("사용법: simai-bootstrap scene [--] <명령...>")
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
        log(f"장면 준비 모드: 가중치 {SCENE_SET} 확인, Tier 0·LIVE 없음 → {shlex.join(rest)}")
        return run_scene_child(root, rest)


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
    if not args or args[0].startswith("-"):
        args = ["tier0", *args]
    mode, rest = args[0], args[1:]
    if mode == "scene":
        return scene(rest)
    if mode not in ("tier0", "queue", "check", "run"):
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
        claim_tier0(choose_root()[0])
    try:
        rec = prepare(want_set)
    except BootstrapError as e:
        log(f"{e} → 시작하지 않는다 (종료 코드 {e.code})")
        return e.code
    if mode == "check":
        print(json.dumps(rec, ensure_ascii=False, indent=2))
        return EXIT_OK
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
    wait_scene_released(Path(os.environ["SIMAI_ROOT"]))  # 장면 작업이 멈춰 VRAM 을 돌려줄 때까지(상한 있음)
    if services == ["tier0"]:
        execvp([TIER0_CMD, *rest])  # Tier 0 단독: 감시 프로세스 없이 지금까지와 같다
        return EXIT_OK
    return supervise(rec, services, rest)


if __name__ == "__main__":
    sys.exit(main())
