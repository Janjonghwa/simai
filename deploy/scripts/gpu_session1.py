#!/usr/bin/env python3
"""GPU 1차 세션 검증 큐 (S15P31A101-183). ai/experiments/gpu_queue(S15P31A101-36)를 그대로 써서 항목을 차례로 돌린다.

항목(순서, docs/design/runpod-environment.md "GPU 1차 세션 큐"):
  a  -31  Pod 점검(GPU·드라이버·CUDA·마운트·용량·쓰기·fsync)            deploy/scripts/pod_check.py
  b  -32  Tier 0 이미지 기동·TensorRT 엔진 생성                           deploy/scripts/session1_engine.sh
  t       시험 도구(pytest·cryptography)를 컨테이너 디스크에 설치          Pod 만 (이미지에는 시험 의존성이 없다)
  c  -182 LIVE 입력: vCPU·CPU 디코딩 처리량(NVDEC 참고)·LIVE 기동·10명    deploy/scripts/session1_live.py (-182 머지 필요)
  d  -60  Tier 0 GPU 우선 보호 측정 P0~P7                                  deploy/scripts/session1_gpu_priority.py (-60 머지 필요)
  e  -92  Network Volume 강제 종료 복구                                    제외: 엘리스 블록 스토리지 세션으로 옮김 (S15P31A101-205)
  f  -45·-175 Linux TLS 시험 반복                                         제외: GPU 가 필요 없어 GPU 세션에서 뺌 (Linux CPU 에서 repeat_pytest.py)
  g0~g3 -207 EXP-02 이미지(HMR) 점검·E-GATE-0 재현(H6·H0·H1)               exp02 이미지 Pod 에서만 (S15P31A101-207)
  g4  -238 HMR 보정 K 입력 경로(교실 사진 20장 + 교실 보정 기록, 판정 check_calib_k)  exp02 이미지 Pod 에서만 (S15P31A101-243)
  m0~m2 -207 MapAnything 이미지 점검·E-GATE-0 재현(C1·C2)                    mapanything 이미지 Pod 에서만
  u1    -246 통합 이미지 GPU 점검(CUDA·HMR 잠금 밖 휠 없이·EGL·vLLM 플러그인)      unified 이미지 Pod 에서만. 이 Pod 는 a·b·t·c·d 도 돈다
  u2    -246 통합 이미지 HMR 보정 K 경로(렌더 없음, g4 와 같은 판정)               unified 이미지 Pod, SIMAI_WEIGHTS_SET=exp02 simai-bootstrap queue
  v1~v3 -246 VLM 단독 적재·G7 지연 모양·Tier 0 + VLM 동시                     자리만(사람이 돈다, docs/design/unified-env.md)
  (--exp-jobs: 추가 EXP Job 파일(예 -87 다시점 집계)을 LIVE 보호를 켠 별도 큐로 마지막에 돈다)

항목마다 돌 이미지가 정해져 있다(Pod 하나 = 이미지 하나). 지금 이미지(--image, 기본 이미지 ENV SIMAI_IMAGE_KIND, 없으면 tier0)의
항목만 큐에 넣고 나머지는 '다른 이미지' 로 표에 남긴다. 2차 세션은 Tier 0 Pod → exp02 Pod → mapanything Pod 순서로 같은 명령을 돈다.
실험 이미지의 큐는 LIVE 보호를 켠다(on_live=stop: 관제 중이면 멈춤). 데이터(hmr_poc14·room4)는 저장소에 없어 Pod 에 따로 올린다.
g4 입력(교실 사진·보정 기록)은 사람이 찍혀 있어 저장소에 넣지 않고, prepare 가 노트북 recordings 에서 저장소 밖 묶음(g4-inputs.tar)으로 만든다.
실험 Job 은 exp_job.py 로 감싸 지표를 Run 기록에 넘기고, 가중치·입력·결과는 저장 위치 절대 경로로 기록한다. Run 기록 검증기가 없으면
(실험 이미지 venv) 시작 전에 컨테이너 디스크에 깔고, 그래도 없으면 시작하지 않는다(종료 코드 5).

GPU·지역은 그때그때 남은 것(RunPod·Vast.ai, 볼륨 없음)을 쓴다(S15P31A101-205). Pod 모드 저장 위치는 simai-bootstrap 과 같다:
SIMAI_ROOT → /workspace/simai → 컨테이너 디스크 /srv/simai. 볼륨이 없으면(마운트 지점 아님) a 의 볼륨 측정은 '볼륨 없음' SKIP 이고,
결과 묶음은 컨테이너 디스크에 있으므로 Pod 를 지우기(Terminate) 전에 scp 로 가져온다.

한 항목이 실패해도 다음 항목으로 간다(continue_on_failure). 항목마다 제한 시간과 Run 기록(run_record 계약)은 gpu_queue 가 맡는다.
전체 시간 상한(--max-total-s)이 지나면 도는 항목을 끝내고(ABORTED 기록) 남은 항목은 돌리지 않는다. Ctrl-C·SIGTERM 도 같다.
끝나면(실패·중단·큐 오류여도) 결과 묶음(tar.gz, 비밀 파일 제외)을 만들고 노트북으로 가져올 scp 명령을 출력한다.

    # 노트북: 저장소 사본과 git 정보 준비 → Pod 로 올린다 (출력되는 명령을 따른다)
    python deploy/scripts/gpu_session1.py prepare --out <폴더>
    # Pod (Tier 0 이미지): 가중치·GPU 준비(simai-bootstrap) 뒤 이 큐를 돈다
    simai-bootstrap queue
    # 실험 이미지 Pod: 같은 명령. 가중치 묶음(exp02·static)은 이미지가 정하고, 큐는 그 이미지 항목만 돈다
    simai-bootstrap queue --session <이름>
    # 노트북 드라이런: 가짜 nvidia-smi·로컬 디렉터리
    uv run --project ai python deploy/scripts/gpu_session1.py run --mode dryrun --root <로컬 폴더>

종료 코드: 0 모든 항목 성공, 1 실패한 항목 있음, 2 다른 큐가 잠금을 쥐고 있음, 3 LIVE 관제 중이라 EXP 큐(g)를 멈춤,
4 Job 정의 오류, 5 사전 점검 실패(쓰기·용량), 6 큐 오류(예외), 7 결과 묶음을 만들지 못함, 8 전체 시간 상한, 130 Ctrl-C·SIGTERM.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import importlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
import simai_bootstrap  # noqa: E402 - 같은 폴더(저장 위치 규칙을 한 곳에서)
QUEUE_DIR = REPO / "ai" / "experiments" / "gpu_queue"
FAKE_SMI = REPO / "ai" / "tests" / "experiments" / "gpu_queue" / "fake_nvidia_smi.py"
STAMP_FILE = "git-stamp.json"  # prepare 가 저장소 사본 최상위에 둔다(Pod 사본에는 .git 이 없다)
OWNER = "S15P31A101-183 GPU 1차 세션 큐"

EXIT_OK, EXIT_FAILED, EXIT_LOCKED, EXIT_LIVE, EXIT_BAD_JOBS = 0, 1, 2, 3, 4  # gpu_queue 와 같은 값
EXIT_PREFLIGHT, EXIT_CRASH, EXIT_BUNDLE, EXIT_DEADLINE, EXIT_SIGNAL = 5, 6, 7, 8, 130

# 결과 묶음에 넣지 않는 것: 이름(비밀·키·토큰·환경 파일·대형 산출물)과 내용(개인 키·HF 토큰·토큰 할당)
SECRET_NAMES = ("hf.env", "*.env", ".env", ".env.*", "*.key", "*.pem", "*.p12", "*.crt", "id_rsa*", "id_ed25519*",
                "authorized_keys", "*token*", "*secret*", "*.engine", "*.pt", "*.pth", "*.onnx", "*.safetensors", "*.ckpt")
SECRET_RE = re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----|hf_[A-Za-z0-9]{30,}|(?:HF_TOKEN|HUGGING_FACE_HUB_TOKEN|RUNPOD_API_KEY)"
                       rb"\s*=\s*[^\s\"']{8,}")
SECRET_ENV_RE = re.compile(r"TOKEN|SECRET|PASSWORD|API_KEY|PRIVATE", re.I)
PRIVATE_DIR = ".simai-private"


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}Z] session1: {msg}", flush=True)


# ---------------------------------------------------------------- 항목 정의


def lock_pins(want: tuple[str, ...] = ("pytest", "cryptography")) -> dict[str, str]:
    """ai/uv.lock 의 도구 버전(Pod 의 이미지 venv 에는 dev 의존성이 없어 따로 설치한다)."""
    doc = tomllib.loads((REPO / "ai" / "uv.lock").read_text(encoding="utf-8"))
    return {p["name"]: p["version"] for p in doc.get("package", []) if p.get("name") in want}


VALIDATOR_PKGS = ("jsonschema", "referencing")  # Run 기록 스키마 검증기(gpu_queue.run_record.load_validator)


def validator_ready(contracts: Path | None = None) -> bool:
    """Run 기록 스키마 검증기를 만들 수 있는가(jsonschema·referencing import + 스키마 파일)."""
    rr = load_gpu_queue().load_validator  # gpu_queue 가 run_record 에서 가져온 것
    return rr(contracts or REPO / "contracts")[0] is not None


def ensure_validator(lay: Layout) -> str | None:
    """실험 이미지 venv 에는 jsonschema 가 없어 gpu_queue 가 Run 기록 검증을 조용히 건너뛴다(S15P31A101-207 리뷰 R1).
    없으면 ai/uv.lock 의 판으로 컨테이너 디스크(lay.tools)에 설치해 이 프로세스 경로에 더한다. 그래도 없으면 이유(시작하지 않음)."""
    if validator_ready():
        return None
    if lay.mode != "pod":
        return "jsonschema·referencing 이 없다(uv run --project ai 로 실행한다)"
    pins = lock_pins(VALIDATOR_PKGS)
    cmd = ["uv", "pip", "install", "-q", "--python", lay.py, "--target", str(lay.tools), *(f"{k}=={v}" for k, v in sorted(pins.items()))]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        detail = (r.stdout + r.stderr).strip()[-300:]
    except (OSError, subprocess.SubprocessError) as e:
        detail = f"{type(e).__name__}: {e}"
    if str(lay.tools) not in sys.path:
        sys.path.insert(0, str(lay.tools))
    importlib.invalidate_caches()
    return None if validator_ready() else f"Run 기록 검증기를 설치하지 못함({' '.join(cmd[:4])} …): {detail}"


IMAGES = ("tier0", "exp02", "mapanything", "unified")  # 이미지 ENV SIMAI_IMAGE_KIND 값. unified 는 tier0 항목도 돈다(S15P31A101-246)
# 이미지별 env_hash 잠금 파일(이미지 다이제스트를 모를 때만 쓰인다, deploy/README "env_hash")
LOCK_FILES = {"tier0": "ai/uv.lock", "exp02": "deploy/exp02/requirements.lock.txt",
              "mapanything": "deploy/mapanything/requirements.lock.txt", "unified": "ai/uv.lock"}


def runs_on(it: dict, image: str) -> bool:
    """이 이미지 Pod 에서 도는 항목인가. 통합 이미지는 Tier 0 이미지와 같은 Tier 0 층을 가져 tier0 항목(b·c·d)도 돈다."""
    want = it.get("image", "tier0")
    return want in ("*", image) or (image == "unified" and want == "tier0")


class Layout:
    """모드별 경로와 명령. pod: /workspace 볼륨·이미지 Python, dryrun: 로컬 폴더·가짜 nvidia-smi."""

    def __init__(self, mode: str, root: Path | None, session: str, python: str | None, image: str = "tier0") -> None:
        self.mode = mode
        self.session = session
        self.image = image
        if mode == "pod":
            if root is None:  # simai-bootstrap 과 같은 규칙. SIMAI_ROOT 의 부모가 볼륨 루트(.simai-private 위치)다
                self.simai, _source = simai_bootstrap.choose_root()
                self.root = self.simai.parent
            else:
                self.root, self.simai = root, root / "simai"
            # 볼륨(RunPod Network·Pod 볼륨, docker -v 블록 스토리지)은 마운트 지점이다. 아니면 컨테이너 디스크
            self.has_volume = os.path.ismount(self.root)
        else:
            if root is None:
                raise SystemExit("드라이런은 --root <로컬 폴더> 가 필요하다")
            # resolve() 는 Windows subst 드라이브를 긴 실제 경로로 바꿔 경로 길이 한도에 걸리게 하므로 absolute() 만
            self.root = Path(os.path.abspath(root))
            self.simai = self.root / "simai"
            self.has_volume = True  # 드라이런은 볼륨 측정 경로를 작게 돌려 본다(--allow-not-mount)
        self.py = Path(python or sys.executable).as_posix()  # bash(b 항목)에서도 쓰므로 / 구분자
        self.queue_state = self.simai / "queue"
        self.session_dir = self.simai / "runs" / "gpu-session1" / session
        self.tools = Path(tempfile.gettempdir()) / "simai-session1-testtools"  # 컨테이너 디스크(볼륨 아님)
        self.smi = ["nvidia-smi"] if mode == "pod" else [self.py, FAKE_SMI.as_posix()]
        # 실험 이미지는 큐 이름을 나눈다(같은 저장 위치를 다시 붙여도 Tier 0 세션의 끝난 항목 상태와 섞이지 않게)
        self.queue_name = f"session1-{session}" if image == "tier0" else f"session1-{session}-{image}"

    def pythonpath(self) -> str:
        parts = [str(REPO / "ai" / "src"), str(REPO / "ai")]  # 저장소 사본 코드가 이미지에 설치된 simai_ai 보다 앞선다
        if self.mode == "pod":
            parts.append(str(self.tools))
        return os.pathsep.join(parts)


def find_bash() -> str:
    """드라이런용 bash. Windows 는 PATH 보다 System32 를 먼저 찾아 WSL bash(Windows 환경 변수를 못 받음)가 잡히므로
    Git for Windows 의 bash 를 찾는다."""
    if os.name != "nt":
        return "bash"
    git = shutil.which("git")
    candidates = []
    if git:
        g = Path(git).resolve()
        candidates += [g.parents[1] / "bin" / "bash.exe", g.parents[1] / "usr" / "bin" / "bash.exe"]
    candidates.append(Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git" / "bin" / "bash.exe")
    for c in candidates:
        if c.is_file():
            return c.as_posix()
    raise SystemExit("드라이런에 Git Bash 가 필요하다(WSL bash 는 Windows 환경 변수를 받지 못한다)")


PROVENANCE_NONE = {"frames": {"source": "SIM", "dataset_id": "none:infrastructure-check"}, "is_upper_bound": True}
PROVENANCE_SIM = {"frames": {"source": "SIM", "dataset_id": "sim/tier0-e2e-walker"}, "is_upper_bound": True}
# E-GATE-0 과 같은 입력 사진(공개 영상 프레임 14장, 라운지 사진 4장). 정답 데이터가 아니다
PROVENANCE_HMR = {"frames": {"source": "FILE", "dataset_id": "hmr_poc14"}, "is_upper_bound": False}
PROVENANCE_ROOM4 = {"frames": {"source": "FILE", "dataset_id": "room4"}, "is_upper_bound": False}
# g4 입력(S15P31A101-243): 교실 사진(cam_A~D × 5장, 2304×1296)과 교실 보정 기록(AnyCalib, PROVISIONAL). 노트북 원본은 커밋하지 않는 recordings
G4_STILLS, G4_IMAGES = "classroom-stills-20261021", 20
G4_CALIB = "intrinsics-20261021-022940z.json"
G4_LOCAL_STILLS = REPO / "ai" / "experiments" / "recordings" / G4_STILLS
G4_LOCAL_CALIB = REPO / "ai" / "experiments" / "recordings" / "intrinsics" / G4_CALIB


def calibration_provenance(path: Path) -> dict | None:
    """보정 기록 파일 → Run 기록 input_provenance.calibration. run_image 의 k_input.input_provenance(fs3db_common.k_summary)와 같은 규칙:
    무마커 추정 기록(simai.intrinsics_estimate)은 G1_AUTO, 판은 '<파일 이름>@<sha256>'. 읽지 못하거나 다른 형식이면 모른다(None, 지어내지 않는다)."""
    try:
        data = path.read_bytes()
        doc = json.loads(data)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("format") != "simai.intrinsics_estimate":
        return None
    return {"source": "G1_AUTO", "calibration_version": f"{path.name}@sha256:{hashlib.sha256(data).hexdigest()}"}


def items(lay: Layout) -> list[dict]:
    """세션 항목. command 가 None 이면 자리만(큐에 넣지 않는다). expected_s 는 H100 예상(2차 세션 실측 기준, 근거는
    docs/design/runpod-environment.md "예상 시간 근거"), timeout_s 는 처음 정한 넉넉한 값 그대로."""
    dry = lay.mode == "dryrun"
    py = lay.py
    smi_text = json.dumps(lay.smi)
    pins = lock_pins()
    pod_check = [py, "deploy/scripts/pod_check.py", "--root", str(lay.root), "--nvidia-smi", smi_text]
    data = lay.simai / "datasets"
    w = lay.simai / "weights"  # 가중치·입력·결과는 저장 위치의 절대 경로로 적는다(큐 workdir 는 저장소 사본이라 상대 경로면 못 찾는다)
    hmr = "ai/experiments/hmr"
    egate = {"experiment_state": "BENCHMARK"}  # E-GATE-0 재현(새 이미지 환경 확인). 판정 기준은 docs/design/exp-images.md
    # HMR official·opt 프리셋이 실제로 읽는 것: YOLO26x-pose 검출, sam-3d-body(fastcfg = 같은 ckpt·mhr + Fast config), MoGe2-S FOV
    hmr_w = [str(w / "yolo" / "yolo26x-pose.pt"), str(w / "sam-3d-body-dinov3" / "model.ckpt"),
             str(w / "sam-3d-body-dinov3" / "assets" / "mhr_model.pt"), str(w / "sam-3d-body-dinov3-fastcfg" / "model_config.yaml"),
             str(w / "moge" / "moge-2-vits-normal" / "model.pt")]

    def exp(key: str, name: str, title: str, image: str, run: str, cmd: list[str], out: Path, *, weights: list[str],
            dataset: list[str], results: list[tuple[str, str]], experiment_id: str, provenance: dict, model_version: str,
            expected_s: int, timeout_s: int, jira: str = "S15P31A101-207", check: list[str] | None = None) -> dict:
        """실험 Job: exp_job.py 가 결과 폴더를 덮어쓰지 않게 막고, 실험 지표를 $SIMAI_RUN_DIR/metrics.json 으로 넘긴다.
        Run id 에 세션 이름을 붙여 세션마다 결과 폴더가 다르다(E-GATE-0·다른 세션 결과를 덮어쓰지 않는다).
        check 를 주면 exp_job 이 명령 성공 뒤 그 판정(exp_job --check)으로 Job 성공·실패를 정한다."""
        return {"key": key, "name": name, "jira": jira, "title": title, "image": image, "run_id": run,
                "command": [py, "deploy/scripts/exp_job.py", "--out", str(out), *(check or []), "--", *cmd],
                "expected_s": expected_s, "timeout_s": timeout_s, "model_version": model_version, "provenance": provenance,
                "experiment_id": experiment_id, **egate, "weights": weights, "dataset": dataset,
                "results": [{"kind": k, "path": str(out / rel) if rel else str(out)} for k, rel in results]}

    sfx = re.sub(r"[^A-Za-z0-9_.-]", "_", lay.session)
    h6, h0, h1, h7, h7u = (f"{r}_img_{sfx}" for r in ("H6_det_yolo26x", "H0_smoke", "H1_official_opt", "H7_calib_k", "H7u_calib_k_unified"))
    stills, calib = data / G4_STILLS, data / "classroom-k" / G4_CALIB
    # 보정 출처는 큐를 만들 때(Pod 에 입력이 올라간 뒤) 파일에서 읽는다. 파일이 없으면 비우고, 판정이 g4 를 실패로 둔다
    g4_prov = {"frames": {"source": "FILE", "dataset_id": G4_STILLS}, "is_upper_bound": False}
    if (cal := calibration_provenance(calib)) is not None:
        g4_prov["calibration"] = cal
    out02 = lay.simai / "runs" / "exp02"  # run_image·det_bench 의 OUT = $SIMAI_ROOT/runs/exp02/<run id>
    out01 = lay.simai / "runs" / "exp01"
    hmr_results = [("REPORT", "metrics.json"), ("HUMAN_MESH_OUTPUT", "meshes"), ("RENDER", "renders")]
    if dry:
        pod_check += ["--allow-not-mount", "--write-mb", "16", "--fsync-count", "50", "--min-free-gb", "1"]
    elif not lay.has_volume:
        pod_check += ["--no-volume"]
    return [
        {"key": "a", "name": "a_pod_check", "jira": "S15P31A101-31", "title": "Pod 점검", "command": pod_check, "image": "*",
         "expected_s": 60, "timeout_s": 900, "model_version": "n/a (인프라 점검)", "provenance": PROVENANCE_NONE},
        {"key": "b", "name": "b_tier0_engine", "jira": "S15P31A101-32", "title": "Tier 0 이미지 기동·TensorRT 엔진 생성",
         "command": ["bash" if lay.mode == "pod" else find_bash(), "deploy/scripts/session1_engine.sh"], "expected_s": 720, "timeout_s": 2700,
         "model_version": "YOLO26x-pose TensorRT FP16 (e2e, imgsz 1280, batch 4)", "provenance": PROVENANCE_NONE,
         "weights": ["weights/yolo/yolo26x-pose.pt"]},
        {"key": "t", "name": "t_test_tools", "jira": "S15P31A101-183", "title": "시험 도구 설치(컨테이너 디스크)",
         "command": None if dry else ["uv", "pip", "install", "--python", py, "--target", str(lay.tools),
                                      *(f"{k}=={v}" for k, v in sorted(pins.items()))],
         "skip_reason": "드라이런은 ai 개발 환경에 이미 있다", "expected_s": 60, "timeout_s": 600,
         "model_version": "n/a (시험 도구)", "provenance": PROVENANCE_NONE},
        # c·d 는 -182·-60 이 머지된 뒤의 경로를 부른다(머지 전이면 각 단계가 '머지 필요'로 실패하고 다음 항목으로 간다)
        {"key": "c", "name": "c_tier0_live", "jira": "S15P31A101-182", "title": "LIVE 입력 Pod 확인(vCPU·CPU 디코딩 처리량·LIVE 기동·10명)",
         "command": [py, "deploy/scripts/session1_live.py", "all"], "expected_s": 300, "timeout_s": 2400,
         "model_version": "YOLO26x-pose TensorRT FP16 + PyAV CPU 디코딩 (LIVE 하네스)", "provenance": PROVENANCE_NONE},
        {"key": "d", "name": "d_tier0_gpu_priority", "jira": "S15P31A101-60", "title": "Tier 0 GPU 우선 보호 측정 P0~P7",
         "command": [py, "deploy/scripts/session1_gpu_priority.py"], "expected_s": 600, "timeout_s": 2700,
         "model_version": "Tier 0 LIVE 하네스 + tier0_priority_probe", "provenance": PROVENANCE_NONE},
        {"key": "e", "name": "e_crash_recovery", "jira": "S15P31A101-92", "title": "Network Volume 강제 종료 복구",
         "command": [py, "deploy/scripts/crash_recovery_g3.py", "--volume-dir", str(lay.simai / "runs" / "crash92")],
         "expected_s": 300, "timeout_s": 1800, "model_version": "Tier 0 SIM 입력 (services.tier0)",
         "provenance": PROVENANCE_SIM, "excluded": "엘리스 블록 스토리지 세션으로 옮김(볼륨 강제 종료 복구는 그 저장소에서 잰다)"},
        {"key": "f", "name": "f_tls_linux", "jira": "S15P31A101-175", "title": "Linux TLS 시험 반복 (-45·-175)",
         "command": [py, "deploy/scripts/repeat_pytest.py", "--count", "1" if dry else "5", "--cwd", "ai", "--",
                     "tests/services/tier0/test_i1_tls.py", "tests/services/tier0/test_serial_tls.py"],
         "expected_s": 600, "timeout_s": 2400, "model_version": "n/a (TLS 시험)", "provenance": PROVENANCE_NONE,
         "excluded": "GPU 가 필요 없어 GPU 세션에서 뺌(Linux CPU 에서 repeat_pytest.py 로)"},
        # 2차 세션 실험 이미지 항목(S15P31A101-207)
        {"key": "g0", "name": "g0_exp02_image_check", "jira": "S15P31A101-207", "title": "EXP-02 이미지 GPU 점검",
         "image": "exp02", "command": [py, "deploy/scripts/exp_image_check.py"], "expected_s": 60, "timeout_s": 600,
         "model_version": "n/a (이미지 점검)", "provenance": PROVENANCE_NONE},
        exp("g1", "g1_exp02_h6_det", "EXP-02 재현 H6 검출 시간", "exp02", h6,
            ["simai-exp02", py, f"{hmr}/det_bench.py", "--run-id", h6, "--half"], out02 / h6,
            weights=[str(w / "yolo" / "yolo26x-pose.pt")], dataset=[str(data / "hmr_poc14" / "t3_multi_f050.jpg")],
            results=[("REPORT", "metrics.json")], experiment_id="EXP-02", provenance=PROVENANCE_HMR,
            model_version="YOLO26x-pose (ultralytics, FP16)", expected_s=60, timeout_s=1200),
        exp("g2", "g2_exp02_h0_smoke", "EXP-02 재현 H0 스모크 1장", "exp02", h0,
            ["simai-exp02", py, f"{hmr}/run_image.py", "--run-id", h0, "--images", str(data / "hmr_poc14" / "t1_front_taichi.jpg"),
             "--mode", "official", "--preset", "opt"], out02 / h0,
            weights=hmr_w, dataset=[str(data / "hmr_poc14" / "t1_front_taichi.jpg")], results=hmr_results,
            experiment_id="EXP-02", provenance=PROVENANCE_HMR, model_version="Fast-SAM-3D-Body@d72aa36 dinov3 (opt)",
            expected_s=60, timeout_s=1200),
        exp("g3", "g3_exp02_h1_official", "EXP-02 재현 H1 공식 경로 14장", "exp02", h1,
            ["simai-exp02", py, f"{hmr}/run_image.py", "--run-id", h1, "--images", str(data / "hmr_poc14"),
             "--mode", "official", "--preset", "opt"], out02 / h1,
            weights=hmr_w, dataset=[str(data / "hmr_poc14")], results=hmr_results,
            experiment_id="EXP-02", provenance=PROVENANCE_HMR, model_version="Fast-SAM-3D-Body@d72aa36 dinov3 (opt)",
            expected_s=120, timeout_s=2700),
        # g4: 알려진 K(보정)를 펴서 넣는 경로. MoGe 를 띄우지 않으므로 MoGe 가중치는 기록하지 않고, 보정 기록 파일이 입력이다
        exp("g4", "g4_exp02_h7_calib_k", "EXP-02 보정 K 입력 경로 교실 사진 20장", "exp02", h7,
            ["simai-exp02", py, f"{hmr}/run_image.py", "--run-id", h7, "--images", str(stills),
             "--mode", "official", "--preset", "opt", "--calibration", str(calib)], out02 / h7,
            weights=[p for p in hmr_w if "/moge/" not in Path(p).as_posix()], dataset=[str(stills), str(calib)],
            results=hmr_results, experiment_id="EXP-02", provenance=g4_prov,
            model_version="Fast-SAM-3D-Body@d72aa36 dinov3 (opt) + 보정 K(펴짐)", expected_s=180, timeout_s=1800,
            jira="S15P31A101-238",
            check=["--check", f"{hmr}/check_calib_k.py", f"--check-arg=--images={G4_IMAGES}",  # = 형식: '--' 로 시작하는 값
                   f"--check-arg=--provenance={json.dumps(g4_prov, ensure_ascii=False)}"]),
        # 3차 세션 통합 이미지 항목(S15P31A101-246). u1 은 큐, v1~v3 는 vLLM 서버를 띄우고 재는 사람 항목(docs/design/unified-env.md "3차 GPU 세션 큐")
        {"key": "u1", "name": "u1_unified_image_check", "jira": "S15P31A101-246", "title": "통합 이미지 GPU 점검(CUDA·HMR 휠 없이·EGL·vLLM 플러그인)",
         "image": "unified", "command": [py, "deploy/scripts/exp_image_check.py"], "expected_s": 120, "timeout_s": 900,
         "model_version": "n/a (이미지 점검)", "provenance": PROVENANCE_NONE},
        # u2: 시연 HMR 경로(보정 K → sam-3d-body → 메시·관절, 렌더 없음)를 잠금 밖 휠 없는 통합 이미지에서. 가중치는 SIMAI_WEIGHTS_SET=exp02 로 받는다
        exp("u2", "u2_unified_hmr_calib_k", "통합 이미지 HMR 보정 K 경로(렌더 없음) 교실 사진 20장", "unified", h7u,
            ["simai-exp02", py, f"{hmr}/run_image.py", "--run-id", h7u, "--images", str(stills),
             "--mode", "official", "--preset", "opt", "--calibration", str(calib), "--no-render"], out02 / h7u,
            weights=[p for p in hmr_w if "/moge/" not in Path(p).as_posix()], dataset=[str(stills), str(calib)],
            results=[("REPORT", "metrics.json"), ("HUMAN_MESH_OUTPUT", "meshes")], experiment_id="EXP-02", provenance=g4_prov,
            model_version="Fast-SAM-3D-Body@d72aa36 dinov3 (opt) + 보정 K(펴짐), 통합 이미지", expected_s=180, timeout_s=1800,
            jira="S15P31A101-246",
            check=["--check", f"{hmr}/check_calib_k.py", f"--check-arg=--images={G4_IMAGES}",
                   f"--check-arg=--provenance={json.dumps(g4_prov, ensure_ascii=False)}"]),
        *({"key": key, "name": name, "jira": "S15P31A101-246", "title": title, "image": "unified", "command": None,
           "placeholder": f"사람이 돈다(docs/design/unified-env.md 3차 GPU 세션 큐 {key})", "expected_s": exp_s, "timeout_s": exp_s * 2,
           "model_version": "Cosmos3-Nano Reasoner (vLLM 0.21.0 + vllm-cosmos3)", "provenance": PROVENANCE_NONE}
          for key, name, title, exp_s in (("v1", "v1_vlm_load", "VLM 단독 적재·VRAM", 1500),
                                          ("v2", "v2_vlm_latency", "VLM G7 지연 모양", 300),
                                          ("v3", "v3_tier0_with_vlm", "Tier 0 + VLM 동시(G0·CPU 경합)", 900))),
        {"key": "m0", "name": "m0_mapanything_image_check", "jira": "S15P31A101-207", "title": "MapAnything 이미지 GPU 점검",
         "image": "mapanything", "command": [py, "deploy/scripts/exp_image_check.py"], "expected_s": 60, "timeout_s": 600,
         "model_version": "n/a (이미지 점검)", "provenance": PROVENANCE_NONE},
        *(exp(key, f"{key}_mapanything_{base[:2].lower()}", f"MapAnything 재현 {base} ({model})", "mapanything", f"{base}_img_{sfx}",
              [py, "ai/experiments/static_scene/run_mapanything.py", "--run-id", f"{base}_img_{sfx}", "--images", str(data / "room4"),
               "--model", str(w / model), "--runs-dir", str(out01)], out01 / f"{base}_img_{sfx}",
              weights=[str(w / model / "model.safetensors"), str(w / model / "config.json")], dataset=[str(data / "room4")],
              results=[("REPORT", "metrics.json"), ("SCENE_OUTPUT", "cameras.json"), ("SCENE_OUTPUT", "fused.ply"), ("RENDER", "panels")],
              experiment_id="EXP-01", provenance=PROVENANCE_ROOM4, model_version=f"map-anything@3d10cf7 + {model}",
              expected_s=120, timeout_s=1200)
          for key, base, model in (("m1", "C1_room4_bf16", "map-anything"), ("m2", "C2_room4_apache_bf16", "map-anything-apache"))),
    ]


def _sh(text: str) -> str:
    return text if re.fullmatch(r"[A-Za-z0-9_./:\\-]+", text) else "'" + text.replace("'", "'\\''") + "'"


def job_env(lay: Layout) -> dict[str, str]:
    # SIMAI_CONTRACTS_DIR: 이미지 환경 값(/opt/simai/contracts)은 이미지를 만들 때의 옛 계약이다. 사본 코드는 사본 계약으로 검증한다
    return {"SIMAI_ROOT": str(lay.simai), "SIMAI_PY": lay.py, "SIMAI_NVIDIA_SMI": " ".join(_sh(c) for c in lay.smi),
            "PYTHONPATH": lay.pythonpath(), "PYTHONUTF8": "1", "SIMAI_CONTRACTS_DIR": str(REPO / "contracts")}


def build_jobs(lay: Layout, git: dict | None) -> tuple[dict, list[dict]]:
    """gpu_queue Job 파일과 자리만 있는 항목 목록."""
    # provider·datacenter 는 run_record 가 RUNPOD_POD_ID·RUNPOD_DC_ID 로 채운다(그때그때 남은 지역).
    # provider 는 사이트 신호가 없으면 LOCAL(run_record.site_provider). 사이트 미정 클라우드는 Job 의 gpu.provider 에 OTHER 를 적는다
    gpu = {"shared_with_tier0": False} if lay.mode == "pod" else {"provider": "LOCAL"}
    env_cfg: dict = {"lock_files": [LOCK_FILES[lay.image]]}
    if os.environ.get("SIMAI_IMAGE_DIGEST"):
        env_cfg["image_digest"] = os.environ["SIMAI_IMAGE_DIGEST"]
    doc: dict = {
        "queue": {
            "name": lay.queue_name,
            "workdir": str(REPO),
            "continue_on_failure": True,
            "poll_s": 1,
            "kill_grace_s": 10,
            "vram_wait_s": 60,
            "gpu_query_cmd": lay.smi,
            "lock": {"heartbeat_s": 15, "ttl_s": 120},
            # Tier 0 검증 큐는 Tier 0 를 스스로 띄우고 죽인다(b·e). EXP Job 이 아니므로 LIVE 보호를 끈다.
            # 관제(LIVE) 시간에는 이 세션을 돌리지 않는다(사람이 확인). 실험 이미지 큐(g·m)와 --exp-jobs 는 LIVE 보호를 켠다.
            "live_guard": {"enabled": False} if lay.image in ("tier0", "unified") else live_guard(lay),
        },
        "record": {
            "experiment_id": "VAL-01",  # 운영 경로 검증 Run(run_record 2.1.0). 실험 채택 근거가 아니라 e_gate 없음
            "owner": OWNER,
            "experiment_state": "BENCHMARK",
            "gpu": gpu,
            "env": env_cfg,
        },
        "jobs": [],
    }
    if lay.mode == "pod" and lay.has_volume and os.environ.get("RUNPOD_DC_ID"):
        doc["queue"]["volume"] = {"root": str(lay.root), "datacenter": os.environ["RUNPOD_DC_ID"]}
    if git:
        doc["git"] = git
    held: list[dict] = []
    env = job_env(lay)
    for it in items(lay):
        if it["command"] is None or it.get("excluded") or not runs_on(it, lay.image):  # *: 모든 이미지 Pod
            held.append(it)
            continue
        extra = {k: it[k] for k in ("experiment_id", "experiment_state", "weights", "dataset", "results") if it.get(k)}
        doc["jobs"].append({
            "name": it["name"], "command": it["command"], "expected_s": it["expected_s"], "timeout_s": it["timeout_s"],
            "vram_mb": 0, "env": env,
            "record": {"jira_key": it["jira"], "model_version": it["model_version"], "input_provenance": it["provenance"], **extra},
        })
    return doc, held


def live_guard(lay: Layout) -> dict:
    """EXP 큐의 LIVE 보호: 관제 중이면 멈춘다(on_live=stop). 죽은 Tier 0 의 표시 파일은 30 s 뒤 무시.
    표시 파일 경로는 이 세션의 저장 위치 기준으로 적는다(gpu_queue 기본값은 import 때의 SIMAI_ROOT 라 드라이런과 다를 수 있다)."""
    return {"enabled": True, "on_live": "stop", "marker_max_age_s": 30, "marker_files": [str(lay.simai / "state" / "tier0.live")]}


def _held_why(it: dict) -> str:
    if it.get("excluded"):
        return f"제외: {it['excluded']}"
    if it["command"] is None:
        return f"자리만: {it.get('placeholder') or it.get('skip_reason')}"
    return f"다른 이미지: {it.get('image', 'tier0')} Pod 에서"


def plan_table(lay: Layout, doc: dict, held: list[dict]) -> str:
    rows = [f"{'항목':<4} {'이름':<24} {'Jira':<16} {'예상':>7} {'제한':>7}  상태"]
    queued = {j["name"] for j in doc["jobs"]}
    total = 0
    for it in items(lay):
        state = "큐" if it["name"] in queued else _held_why(it)
        if it["name"] in queued:
            total += it["expected_s"]
        rows.append(f"{it['key']:<4} {it['name']:<24} {it['jira']:<16} {it['expected_s'] / 60:>5.0f}분 {it['timeout_s'] / 60:>5.0f}분  {state}")
    future = sum(it["expected_s"] for it in held if it.get("placeholder"))
    other = sum(it["expected_s"] for it in held if it["command"] is not None and not it.get("excluded"))
    rows.append(f"지금 큐({lay.image} 이미지) 예상 {total / 60:.0f}분, 다른 이미지 Pod 항목 +{other / 60:.0f}분"
                + (f", 자리만 항목을 채우면 +{future / 60:.0f}분" if future else ""))
    if lay.mode == "pod":
        rows.append(f"저장 {lay.simai} ({'볼륨' if lay.has_volume else '볼륨 없음: 컨테이너 디스크, Pod 를 지우기 전에 결과를 scp'})")
    return "\n".join(rows)


# ---------------------------------------------------------------- git 정보


def git_stamp() -> dict | None:
    """저장소면 git 에서, Pod 사본이면 prepare 가 둔 git-stamp.json 에서."""
    def git(*a: str) -> str | None:
        try:
            r = subprocess.run(["git", "-C", str(REPO), *a], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout.strip() if r.returncode == 0 else None

    commit = git("rev-parse", "HEAD") if (REPO / ".git").exists() else None
    if commit:
        return {"commit": commit, "branch": git("rev-parse", "--abbrev-ref", "HEAD") or "",
                "dirty_worktree": bool(git("status", "--porcelain"))}
    stamp = REPO / STAMP_FILE
    if stamp.is_file():
        return json.loads(stamp.read_text(encoding="utf-8"))
    return None


# ---------------------------------------------------------------- 사전 점검·환경


def preflight(paths: list[Path], min_free_mb: float) -> list[str]:
    """결과를 쓸 폴더마다: 만들 수 있는지, 쓰고 fsync 하고 지울 수 있는지, 남은 용량이 하한 이상인지."""
    errs = []
    for p in paths:
        try:
            p.mkdir(parents=True, exist_ok=True)
            probe = p / f".session1-probe-{os.getpid()}"
            with open(probe, "wb") as f:
                f.write(b"x" * 4096)
                f.flush()
                os.fsync(f.fileno())
            probe.unlink()
        except OSError as e:
            errs.append(f"{p} 에 쓸 수 없음: {type(e).__name__}: {e}")
            continue
        free_mb = shutil.disk_usage(p).free / 1e6
        if free_mb < min_free_mb:
            errs.append(f"{p} 남은 용량 {free_mb:.0f} MB < 하한 {min_free_mb:.0f} MB")
    return errs


def scrub_env() -> list[str]:
    """Job 에 비밀 환경 변수가 넘어가지 않게 지운다(로그에 찍혀 결과 묶음에 섞이는 길을 막는다)."""
    gone = [k for k in os.environ if SECRET_ENV_RE.search(k)]
    for k in gone:
        os.environ.pop(k, None)
    return sorted(gone)


def sweep_leftovers(queue_name: str) -> list[int]:
    """큐가 예외로 끝나 Job 정리를 못 했을 때: SIMAI_QUEUE_NAME 이 이 큐인 프로세스를 끝낸다(Linux /proc)."""
    proc = Path("/proc")
    if not proc.is_dir():
        return []
    needle = f"SIMAI_QUEUE_NAME={queue_name}".encode()
    killed = []
    for d in proc.iterdir():
        if not d.name.isdigit() or int(d.name) == os.getpid():
            continue
        try:
            if needle in (d / "environ").read_bytes().split(b"\0"):
                os.kill(int(d.name), signal.SIGKILL)
                killed.append(int(d.name))
        except (OSError, ProcessLookupError):
            continue
    return killed


# ---------------------------------------------------------------- 큐 실행


def load_gpu_queue():
    if str(QUEUE_DIR) not in sys.path:
        sys.path.insert(0, str(QUEUE_DIR))
    import gpu_queue  # noqa: PLC0415 - ai/experiments/gpu_queue (S15P31A101-36)

    return gpu_queue


def run_queue(gq, doc: dict, job_file: Path, state_root: Path, deadline: float, outcome: dict) -> int:
    """gpu_queue.Queue 를 이 프로세스에서 돌린다. 전체 시간 상한이 되면 queue.stop 을 세워 우아하게 멈춘다."""
    try:
        gq.load_job_file(job_file)
    except gq.JobFileError as e:
        log(f"Job 파일 오류: {e}")
        return EXIT_BAD_JOBS
    repo = REPO if (REPO / ".git").exists() else None
    queue = gq.Queue(doc, job_file, state_root, repo, REPO / "contracts")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        outcome["deadline_hit"] = True
        log("전체 시간 상한이 이미 지나 큐를 시작하지 않음")
        return EXIT_DEADLINE

    def on_deadline() -> None:
        outcome["deadline_hit"] = True
        log("전체 시간 상한 도달 → 도는 항목을 끝내고 멈춘다")
        queue.stop.set()

    timer = threading.Timer(remaining, on_deadline)
    timer.daemon = True
    timer.start()
    try:
        code = queue.run([], False)
    finally:
        timer.cancel()
    if code == EXIT_SIGNAL and outcome.get("deadline_hit"):
        return EXIT_DEADLINE
    return code


def live_reason(lay: Layout) -> str | None:
    """지금 LIVE 관제 중인가: 이 볼륨의 Tier 0 표시 파일(30 s 안에 갱신)이나 services.tier0 프로세스(gpu_queue.LiveGuard)."""
    gq = load_gpu_queue()
    guard = gq.LiveGuard({"marker_files": [str(lay.simai / "state" / "tier0.live")], "marker_max_age_s": 30},
                         REPO, lambda _msg: None)
    return guard.reason()


def exp_job_doc(path: Path, lay: Layout) -> dict:
    """g 항목: 사용자가 준 EXP Job 파일. LIVE 보호를 강제로 켜고(on_live=stop), 죽은 Tier 0 의 표시 파일은 30 s 뒤 무시.
    표시 파일 경로는 이 세션의 볼륨 기준으로 적는다(gpu_queue 기본값은 import 때의 SIMAI_ROOT 라 드라이런과 다를 수 있다)."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    guard = doc.setdefault("queue", {}).setdefault("live_guard", {})
    want = live_guard(lay)
    guard["enabled"], guard["on_live"] = want["enabled"], want["on_live"]
    guard.setdefault("marker_max_age_s", want["marker_max_age_s"])
    guard.setdefault("marker_files", want["marker_files"])
    return doc


# ---------------------------------------------------------------- 결과 묶음


def is_secret(path: Path, rel: str) -> str | None:
    parts = Path(rel).parts
    if PRIVATE_DIR in parts:
        return "비공개 폴더"
    name = path.name.lower()
    for pat in SECRET_NAMES:
        if fnmatch.fnmatch(name, pat):
            return f"이름 {pat}"
    try:
        with open(path, "rb") as f:
            head = f.read(8 << 20)
    except OSError as e:
        return f"읽지 못함 {type(e).__name__}"
    if SECRET_RE.search(head):
        return "내용에 비밀로 보이는 값"
    return None


def collect(sources: list[tuple[Path, str]], max_mb: float) -> tuple[list[tuple[Path, str]], list[dict]]:
    """묶을 파일과 뺀 파일(이유). 심볼릭 링크는 따라가지 않고 뺀다(비공개 폴더로 가는 링크 차단)."""
    keep, dropped = [], []
    for base, prefix in sources:
        if not base.exists():
            continue
        files = [base] if base.is_file() else sorted(base.rglob("*"))
        for p in files:
            rel = f"{prefix}/{p.relative_to(base).as_posix()}" if base.is_dir() else prefix
            if p.is_symlink():
                dropped.append({"path": rel, "reason": "심볼릭 링크"})
                continue
            if not p.is_file():
                continue
            if PRIVATE_DIR in p.resolve().parts:
                dropped.append({"path": rel, "reason": "비공개 폴더"})
                continue
            why = is_secret(p, rel)
            if why is None and p.stat().st_size > max_mb * 1e6:
                why = f"{max_mb:.0f} MB 초과"
            if why:
                dropped.append({"path": rel, "reason": why})
                continue
            keep.append((p, rel))
    return keep, dropped


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def make_bundle(lay: Layout, summary: dict, max_mb: float) -> tuple[Path | None, list[str]]:
    """결과 묶음. 볼륨에 못 쓰면 컨테이너 임시 폴더로 물러선다. (경로, 경고)."""
    warnings: list[str] = []
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    sources = [(lay.queue_state / lay.queue_name, "queue"), (lay.session_dir / "jobs.json", "jobs.json")]
    exp = lay.queue_state / f"{lay.queue_name}-exp"
    if exp.is_dir():
        sources.append((exp, "queue-exp"))
    # 실험 이미지 항목의 결과(metrics.json·메시·패널). 큰 파일은 max_mb 로 빠진다
    for it in items(lay):
        if runs_on(it, lay.image) and it.get("image", "tier0") != "*" and it.get("run_id"):
            out = Path(it["command"][it["command"].index("--out") + 1])
            sources.append((out, f"runs/{out.parent.name}/{out.name}"))
    keep, dropped = collect(sources, max_mb)
    manifest = {"session": lay.session, "created_at": now_ms(), "files": [], "excluded": dropped}
    for p, rel in keep:
        try:
            manifest["files"].append({"path": rel, "bytes": p.stat().st_size, "sha256": sha256_file(p)})
        except OSError as e:
            dropped.append({"path": rel, "reason": f"읽지 못함 {type(e).__name__}"})
    keep = [(p, rel) for p, rel in keep if any(f["path"] == rel for f in manifest["files"])]
    summary_bytes = (json.dumps(summary, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    for target_dir in (lay.session_dir, Path(tempfile.gettempdir()) / "simai-session1"):
        out = target_dir / f"session1-{lay.session}-{stamp}.tar.gz"
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            with tarfile.open(out, "w:gz") as tar:
                for name, data in (("session.json", summary_bytes), ("bundle-manifest.json", manifest_bytes)):
                    info = tarfile.TarInfo(name)
                    info.size, info.mtime = len(data), int(time.time())
                    tar.addfile(info, io.BytesIO(data))
                for p, rel in keep:
                    tar.add(p, arcname=rel, recursive=False)
            (target_dir / "session.json").write_bytes(summary_bytes)
            (target_dir / "bundle-manifest.json").write_bytes(manifest_bytes)
            if target_dir != lay.session_dir:
                warnings.append(f"볼륨({lay.session_dir})에 묶음을 쓰지 못해 컨테이너 임시 폴더에 썼다")
            return out, warnings
        except OSError as e:
            warnings.append(f"묶음을 {out} 에 쓰지 못함: {type(e).__name__}: {e}")
            try:
                out.unlink(missing_ok=True)
            except OSError:
                pass
    return None, warnings


def scp_command(bundle: Path) -> list[str]:
    ip = os.environ.get("RUNPOD_PUBLIC_IP") or os.environ.get("PUBLIC_IPADDR") or "<Pod 공개 IP>"  # RunPod / Vast.ai
    port = os.environ.get("RUNPOD_TCP_PORT_22") or os.environ.get("VAST_TCP_PORT_22") or "<22 매핑 포트>"
    dest = f"ai/experiments/runs/gpu-session1/{bundle.parent.name}/"
    return [
        f"mkdir -p {dest}",
        f"scp -P {port} -i ~/.ssh/<키> root@{ip}:{bundle.as_posix()} {dest}",
        f"tar -xzf {dest}{bundle.name} -C {dest}",
    ]


# ---------------------------------------------------------------- 명령


def cmd_plan(args, lay: Layout) -> int:
    doc, held = build_jobs(lay, git_stamp())
    print(plan_table(lay, doc, held))
    if args.out:
        args.out.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Job 파일: {args.out}")
    return EXIT_OK


def msys_converted(value: str, env: dict[str, str] | None = None) -> bool:
    """Git Bash(MSYS)가 Pod 경로 인자를 Windows 경로로 바꿨는가. /srv/simai → C:/Program Files/Git/srv/simai,
    /tmp/simai → %TEMP%/simai, / → C:/Program Files/Git/, /c/simai → C:/simai 처럼 대상이 여럿이라 설치 폴더만으로는 못 잡는다.
    (1) Git Bash 안(MSYSTEM)에서 변환을 끄지 않았는데 드라이브 경로면 바뀐 것이다(Pod 경로는 POSIX).
    (2) 신호가 없어도 MSYS 루트(usr/bin/bash.exe 가 있는 폴더)나 그 아래면 바뀐 것이다.
    로컬 폴더로 Pod 를 흉내 낼 때는 MSYS_NO_PATHCONV=1 로 변환이 없음을 밝힌다."""
    env = os.environ if env is None else env
    no_conv = env.get("MSYS_NO_PATHCONV") or env.get("MSYS2_ARG_CONV_EXCL") == "*"
    if env.get("MSYSTEM") and not no_conv and re.match(r"[A-Za-z]:", value):
        return True
    p = Path(value)
    return p.is_absolute() and any((a / "usr" / "bin" / "bash.exe").is_file() for a in (p, *p.parents))


def g4_inputs(out: Path, stills: Path, calib: Path) -> tuple[Path | None, str]:
    """g4 입력 묶음(교실 사진 20장 + 보정 기록). 사람이 찍힌 사진이라 저장소 안에는 만들지 않는다(커밋 사고 방지). (경로, 알림).
    폴더는 실제 경로(resolve: 상대 경로·폴더 링크)로 보고, 최종 파일은 이미 있으면(파일·링크·하드링크 모두) 덮어쓰지 않는다.
    새 임시 파일(O_EXCL)에 쓴 뒤 하드링크로 최종 이름을 만든다(이미 있으면 실패 = 원자적 '없을 때만 생성')."""
    repo = REPO.resolve()
    real_out = out.resolve()
    if real_out.is_relative_to(repo):
        return None, f"g4 입력을 넣지 않음: --out {out} 이 저장소 안이다(사람이 찍힌 사진). 저장소 밖 폴더로 다시 prepare 한다"
    tar = real_out / "g4-inputs.tar"
    if os.path.lexists(tar):  # 남은 파일이 저장소 안 파일의 하드링크·링크일 수 있다 → 따라가 덮어쓰지 않는다
        return None, f"g4 입력을 넣지 않음: {tar} 가 이미 있다(덮어쓰지 않는다). 지우거나 새 --out 폴더로 다시 prepare 한다"
    photos = sorted(stills.glob("*.jpg")) if stills.is_dir() else []
    if len(photos) != G4_IMAGES or not calib.is_file():
        return None, (f"g4 입력 없음: 사진 {len(photos)}/{G4_IMAGES}장({stills}), 보정 기록 {'있음' if calib.is_file() else '없음'}({calib})"
                      " → Pod 의 g4 는 판정 실패로 남는다")
    fd, tmp_name = tempfile.mkstemp(prefix=".g4-inputs-", suffix=".tmp", dir=real_out)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f, tarfile.open(fileobj=f, mode="w") as t:
            for p in photos:
                t.add(p, arcname=f"{G4_STILLS}/{p.name}", recursive=False)
            t.add(calib, arcname=f"classroom-k/{calib.name}", recursive=False)
        os.link(tmp, tar)  # 최종 이름이 그새 생겼으면 FileExistsError(덮어쓰지 않음)
    except OSError as e:
        return None, f"g4 입력을 넣지 않음: {tar} 를 만들지 못함({type(e).__name__}: {e}). 사진 묶음은 남기지 않았다"
    finally:
        tmp.unlink(missing_ok=True)
    return tar, f"g4 입력 {tar} (사진 {len(photos)}장 + {calib.name}). 사람이 찍혀 있어 내부 확인용이다(공유·커밋하지 않는다)"


def cmd_prepare(args) -> int:
    if msys_converted(args.pod_simai):  # 바뀐 값으로 틀린 Pod 명령을 출력하지 않는다
        print(f"--pod-simai 가 Git Bash 경로 변환으로 바뀌었다: {args.pod_simai!r}. 변환을 끄고 다시 실행한다: "
              "MSYS_NO_PATHCONV=1 python deploy/scripts/gpu_session1.py prepare ...", file=sys.stderr)
        return EXIT_BAD_JOBS
    git = git_stamp()
    if not git or not (REPO / ".git").exists():
        print("prepare 는 노트북 저장소에서 실행한다", file=sys.stderr)
        return EXIT_BAD_JOBS
    if git["dirty_worktree"]:
        print("경고: 커밋되지 않은 변경은 사본에 들어가지 않는다(git archive HEAD). Run 기록 git_commit 은 HEAD 다", file=sys.stderr)
        git["dirty_worktree"] = False  # 사본은 HEAD 그대로다
    args.out.mkdir(parents=True, exist_ok=True)
    tar = args.out / "simai-repo.tar"
    r = subprocess.run(["git", "-C", str(REPO), "archive", "--format=tar", "-o", str(tar), "HEAD"])
    if r.returncode != 0:
        return EXIT_BAD_JOBS
    (args.out / STAMP_FILE).write_text(json.dumps(git, ensure_ascii=False) + "\n", encoding="utf-8")
    up = f"{args.pod_simai.rstrip('/')}/setup/session1"
    dest = f"{up}/repo"
    print(f"사본 {tar} ({tar.stat().st_size / 1e6:.1f} MB), git {git['commit'][:12]} ({git['branch']})")
    print(f"Pod 로 올리기 (Pod 저장 위치 {args.pod_simai}. 볼륨 없는 Pod 는 --pod-simai /srv/simai, 포트·IP 는 RunPod Connect·Vast 인스턴스 정보):")
    print(f"  ssh -p <포트> root@<IP> 'mkdir -p {up}'   # 새 디스크에는 업로드 폴더가 없다(scp 는 부모 폴더를 만들지 않는다)")
    print(f"  scp -P <포트> {tar.as_posix()} {(args.out / STAMP_FILE).as_posix()} root@<IP>:{up}/")
    print(f"  ssh -p <포트> root@<IP> 'rm -rf {dest} && mkdir -p {dest} && tar -xf {up}/simai-repo.tar"
          f" -C {dest} && cp {up}/{STAMP_FILE} {dest}/'")
    g4, note = g4_inputs(args.out, args.g4_stills, args.g4_calibration)
    print(note, file=sys.stdout if g4 else sys.stderr)
    if g4:  # exp02 Pod 만: $SIMAI_ROOT/datasets/{classroom-stills-20261021, classroom-k} 로 푼다
        print(f"  scp -P <포트> {g4.as_posix()} root@<IP>:{up}/   # exp02 Pod")
        print(f"  ssh -p <포트> root@<IP> 'mkdir -p {args.pod_simai.rstrip('/')}/datasets && tar -xf {up}/{g4.name}"
              f" -C {args.pod_simai.rstrip('/')}/datasets'")
    print(f"  ssh -p <포트> root@<IP> 'nohup simai-bootstrap queue > {up}/session1.log 2>&1 &'"
          "   # 가중치·GPU 준비 뒤 큐 (SIMAI_SESSION_REPO 기본값이 위 사본)")
    return EXIT_OK


def cmd_run(args, lay: Layout) -> int:
    started = now_ms()
    t0 = time.monotonic()
    deadline = t0 + args.max_total_s
    outcome: dict = {"deadline_hit": False}
    summary: dict = {"session": lay.session, "mode": lay.mode, "root": str(lay.root), "queue": lay.queue_name,
                     "started_at": started, "max_total_s": args.max_total_s, "jira": "S15P31A101-183"}
    gone = scrub_env()
    if gone:
        log(f"Job 에 넘기지 않는 비밀 환경 변수: {', '.join(gone)}")
    summary["scrubbed_env"] = gone
    code = EXIT_OK
    try:
        errs = preflight([lay.queue_state, lay.session_dir], args.min_free_mb)
        if not errs:
            why = ensure_validator(lay)  # 기록 검증 없이 돌지 않는다(형식이 틀린 Run 기록을 성공으로 남기지 않게)
            if why:
                errs.append(why)
        live = None if args.allow_live else live_reason(lay)
        if errs:
            for e in errs:
                log(f"사전 점검 실패: {e}")
            summary["preflight_errors"] = errs
            code = EXIT_PREFLIGHT
        elif live:
            # 세션 항목은 GPU 를 크게 쓰고(엔진 빌드) 시험용 Tier 0 를 띄웠다 죽인다. LIVE 관제 중이면 시작하지 않는다(Tier 0 우선)
            log(f"LIVE 관제 중이라 세션을 시작하지 않음: {live}")
            summary["live"] = live
            code = EXIT_LIVE
        else:
            git = git_stamp()
            doc, held = build_jobs(lay, git)
            summary["placeholders"] = [{"key": h["key"], "name": h["name"], "jira": h["jira"],
                                        "why": _held_why(h)} for h in held]
            job_file = lay.session_dir / "jobs.json"
            job_file.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(plan_table(lay, doc, held), flush=True)
            gq = load_gpu_queue()
            code = run_queue(gq, doc, job_file, lay.queue_state, deadline, outcome)
            summary["queue_exit"] = code
            if args.exp_jobs and code in (EXIT_OK, EXIT_FAILED):
                exp_doc = exp_job_doc(args.exp_jobs, lay)
                exp_doc["queue"]["name"] = f"{lay.queue_name}-exp"
                if git:
                    exp_doc.setdefault("git", git)
                exp_file = lay.session_dir / "exp-jobs.json"
                exp_file.write_text(json.dumps(exp_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                log(f"g: EXP Job 파일 {args.exp_jobs} 를 LIVE 보호를 켠 큐로 돈다")
                exp_code = run_queue(gq, exp_doc, exp_file, lay.queue_state, deadline, outcome)
                summary["exp_queue_exit"] = exp_code
                if exp_code != EXIT_OK and code == EXIT_OK:
                    code = exp_code  # 검증 큐가 실패했으면 그 코드(1)를 앞세운다
            elif args.exp_jobs:
                summary["exp_queue_exit"] = "skipped (검증 큐가 끝까지 돌지 않음)"
    except KeyboardInterrupt:
        log("Ctrl-C (큐 시작 전·후) → 결과 묶음만 만든다")
        code = EXIT_SIGNAL
    except Exception as e:  # noqa: BLE001 - 큐 오류여도 결과 묶음과 scp 명령은 남긴다
        log(f"큐 오류: {type(e).__name__}: {e}")
        summary["crash"] = f"{type(e).__name__}: {e}"
        code = EXIT_CRASH
    finally:
        killed = sweep_leftovers(lay.queue_name) + sweep_leftovers(f"{lay.queue_name}-exp")
        if killed:
            log(f"남은 Job 프로세스 정리: {killed}")
            summary["swept_pids"] = killed
    summary |= {"finished_at": now_ms(), "elapsed_s": round(time.monotonic() - t0, 1),
                "deadline_hit": outcome["deadline_hit"], "exit": code, "jobs": job_states(lay)}
    bundle, warnings = make_bundle(lay, summary, args.max_file_mb)
    for w in warnings:
        log(f"경고: {w}")
    if bundle is None:
        log("결과 묶음을 만들지 못했다. 큐 폴더를 직접 가져온다: " + str(lay.queue_state / lay.queue_name))
        return code if code != EXIT_OK else EXIT_BUNDLE
    log(f"결과 묶음: {bundle}")
    print("\n노트북에서 가져오기:\n  " + "\n  ".join(scp_command(bundle)), flush=True)
    if lay.mode == "pod" and not lay.has_volume:
        print("볼륨 없음: 결과 묶음은 컨테이너 디스크에 있다. Pod 를 지우기(Terminate) 전에 위 scp 로 가져온다.", flush=True)
    print("끝나면 RunPod 콘솔에서 Pod 를 Stop 한다(GPU 과금). LIVE 관제 중이면 Stop 하지 않는다.", flush=True)
    return code


def job_states(lay: Layout) -> dict:
    out = {}
    for name in (lay.queue_name, f"{lay.queue_name}-exp"):
        try:
            state = json.loads((lay.queue_state / name / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for job, st in state.get("jobs", {}).items():
            out[job] = {k: st.get(k) for k in ("status", "attempts", "exit_code", "reason", "run_id")}
    return out


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):  # pytest 캡처 스트림에는 없다
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="GPU 1차 세션 검증 큐 (S15P31A101-183)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--mode", choices=("pod", "dryrun"), default="pod")
        p.add_argument("--root", type=Path, default=None,
                   help="볼륨 루트 (pod 기본: SIMAI_ROOT 의 부모 → /workspace → /srv, 드라이런은 로컬 폴더)")
        p.add_argument("--session", default=datetime.now(timezone.utc).strftime("%Y%m%d"),
                       help="세션 이름 (기본 UTC 날짜). 같은 이름으로 다시 돌리면 끝난 항목은 건너뛴다")
        p.add_argument("--python", default=None, help="Job 이 쓸 Python (기본: 지금 Python)")
        p.add_argument("--image", choices=IMAGES, default=None,
                       help="이 Pod 의 이미지(이 이미지 항목만 큐에 넣는다). 기본: 이미지 ENV SIMAI_IMAGE_KIND, 없으면 tier0")

    p_plan = sub.add_parser("plan", help="Job 파일·예상 시간 표")
    common(p_plan)
    p_plan.add_argument("-o", "--out", type=Path)
    p_run = sub.add_parser("run", help="큐 실행 → 결과 묶음 → scp 명령")
    common(p_run)
    p_run.add_argument("--max-total-s", type=float, default=None, help="전체 시간 상한 (기본 pod 9000 s, 드라이런 1200 s)")
    p_run.add_argument("--min-free-mb", type=float, default=None, help="결과 폴더 남은 용량 하한 (기본 pod 2048, 드라이런 50)")
    p_run.add_argument("--max-file-mb", type=float, default=50, help="묶음에 넣을 파일 하나의 최대 크기")
    p_run.add_argument("--allow-live", action="store_true", help="LIVE 표시 파일·Tier 0 프로세스가 있어도 시작한다(쓰지 않는다)")
    p_run.add_argument("--exp-jobs", type=Path, default=None, help="g 항목: EXP Job 파일(gpu_queue 형식). LIVE 보호를 켜고 마지막에")
    p_prep = sub.add_parser("prepare", help="노트북: 저장소 사본(git archive)과 git 정보를 만든다")
    p_prep.add_argument("--out", type=Path, required=True)
    p_prep.add_argument("--pod-simai", default="/workspace/simai",
                        help="Pod 의 SIMAI_ROOT (볼륨 있음 /workspace/simai, 볼륨 없음 /srv/simai: simai-bootstrap check 의 storage.root)")
    p_prep.add_argument("--g4-stills", type=Path, default=G4_LOCAL_STILLS, help="g4 교실 사진 폴더(노트북, 커밋하지 않음)")
    p_prep.add_argument("--g4-calibration", type=Path, default=G4_LOCAL_CALIB, help="g4 교실 보정 기록(노트북)")
    args = ap.parse_args(argv)
    if args.cmd == "prepare":
        return cmd_prepare(args)
    image = args.image or os.environ.get("SIMAI_IMAGE_KIND") or "tier0"
    if image not in IMAGES:  # 모르는 이미지면 다른 이미지의 항목을 돌리지 않는다
        print(f"알 수 없는 이미지 {image!r} (SIMAI_IMAGE_KIND). {', '.join(IMAGES)} 중 하나를 --image 로 준다", file=sys.stderr)
        return EXIT_BAD_JOBS
    lay = Layout(args.mode, args.root, args.session, args.python, image)
    if args.cmd == "plan":
        return cmd_plan(args, lay)
    if args.max_total_s is None:
        args.max_total_s = 9000 if lay.mode == "pod" else 1200
    if args.min_free_mb is None:
        args.min_free_mb = 2048 if lay.mode == "pod" else 50
    return cmd_run(args, lay)


if __name__ == "__main__":
    sys.exit(main())
