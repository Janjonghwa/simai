#!/usr/bin/env python3
"""simai-bootstrap: 어느 GPU·지역·사이트(RunPod·Vast.ai·엘리스 GPU VM)에서든 같은 순서로 이미지를 준비한다 (S15P31A101-205·-207).

    simai-bootstrap [tier0] [Tier 0 인자...]   준비 → simai-tier0 (엔진 준비 engine_cache ensure → python -m services.tier0)
    simai-bootstrap queue [gpu_session1 run 인자...]   준비 → GPU 1차 세션 큐 (저장소 사본 $SIMAI_SESSION_REPO, 기본 $SIMAI_ROOT/setup/session1/repo)
    simai-bootstrap check                       준비만 하고 결과 JSON 을 출력한다
    simai-bootstrap run <명령...>               준비 → 그 명령 (실험 이미지: simai-bootstrap run simai-exp02 python ...)
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
  4) 기록 $SIMAI_ROOT/runs/bootstrap/<UTC>-<pid>.json (추가만), 그다음 실행할 명령으로 exec 한다.
     queue 는 엔진 준비를 하지 않는다: 세션 b 항목이 엔진 첫 빌드 시간을 재기 때문이다.

HF 토큰: 시작하자마자 HF_TOKEN 을 환경에서 꺼내 이 프로세스 안에만 들고(가중치 받기에만 쓴다), HUGGING_FACE_HUB_TOKEN 은 읽지 않고 지우기만 한다.
그래서 nvidia-smi 조회·도움말·그대로 넘기는 명령·Tier 0·큐 등 어떤 자식 프로세스에도 토큰이 상속되지 않는다(S15P31A101-205 리뷰 R1).

종료 코드(simai-tier0·engine_cache 와 같은 규칙): 0 정상, 2 저장 위치·용량·가중치(없음·해시 불일치·받기 실패·목록 오류·묶음 이름 오류),
3 GPU 없음·최소 사양 미만, 64 사용법 오류(run 뒤에 명령 없음).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

EXIT_OK, EXIT_STORAGE, EXIT_GPU, EXIT_USAGE = 0, 2, 3, 64
WORKSPACE = Path("/workspace")
CONTAINER_ROOT = Path("/srv/simai")  # 볼륨이 없을 때. Pod 를 지우면 같이 사라진다(결과는 지우기 전에 scp)
DEFAULT_MANIFEST = Path(__file__).resolve().parents[1] / "weights.json"  # 저장소. 이미지는 SIMAI_WEIGHTS_MANIFEST 로 준다
MIN_CC = (8, 0)  # Ampere 이상
MIN_VRAM_MB = 22_500  # 24GB급(A10·L4 는 nvidia-smi 에서 약 23,000 MiB 로 보인다)
TOKEN_ENVS = ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN")  # 자식 환경에서 지울 이름. 읽는 것은 HF_TOKEN 하나
_hf_token: str | None = None  # take_token() 이 환경에서 꺼내 둔 값. _request·_redact 만 쓴다
TIER0_CMD = "simai-tier0"
SET_ENV = "SIMAI_WEIGHTS_SET"  # check·queue·run 이 받을 가중치 묶음(이미지 ENV). 없으면 tier0


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


def fetch(entry: dict, target: Path) -> str:
    """target 옆 임시 파일에 받고 해시를 맞춘 뒤에만 이름을 바꾼다. 받은 파일의 sha256."""
    part = target.with_name(f"{target.name}.part-{os.getpid()}")
    h = hashlib.sha256()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with urlopen(_request(entry["source"])) as resp, open(part, "wb") as f:
            for chunk in iter(lambda: resp.read(1 << 20), b""):
                h.update(chunk)
                f.write(chunk)
            f.flush()
            os.fsync(f.fileno())
        got = h.hexdigest()
        if got != entry["sha256"]:
            raise BootstrapError(EXIT_STORAGE, f"{entry['name']} 받은 파일 해시 불일치 {got[:12]}… ≠ 목록 {entry['sha256'][:12]}… "
                                               "(버림, 시작하지 않음)")
        os.replace(part, target)
        return got
    except BootstrapError:
        raise
    except Exception as e:  # noqa: BLE001 - 받기 실패는 모두 가중치 문제(코드 2)로. 메시지는 _redact 를 거쳐 나간다
        raise BootstrapError(EXIT_STORAGE, f"{entry['name']} 받기 실패: {type(e).__name__}: {e}") from e
    finally:
        part.unlink(missing_ok=True)


def ensure_weights(root: Path, entries: list[dict]) -> list[dict]:
    # 해시를 모르는 항목이 하나라도 있으면 아무것도 받기 전에 멈춘다(앞 항목 수 GB 를 받고 나서 실패하지 않게, S15P31A101-207)
    for w in entries:
        if not re.fullmatch(r"[0-9a-f]{64}", w.get("sha256") or ""):
            raise BootstrapError(EXIT_STORAGE, f"{w['name']}: 목록에 sha256 이 없어 확인할 수 없다(모르면 시작하지 않는다)")
    out = []
    for w in entries:
        target = root / w["path"]
        want = w["sha256"]
        rec = {"name": w["name"], "path": str(target), "license": w.get("license", "")}
        if target.is_file():
            got = sha256_file(target)
            if got != want:
                raise BootstrapError(EXIT_STORAGE, f"{w['name']} 가중치 해시 불일치: {target} {got[:12]}… ≠ 목록 {want[:12]}… "
                                                   "(지우지 않음, 시작하지 않음)")
            rec["status"] = "present"
        else:
            log(f"{w['name']} 받는 중 → {target}")
            fetch(w, target)
            rec["status"] = "downloaded"
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
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(_redact(json.dumps(rec, ensure_ascii=False, indent=2)) + "\n", encoding="utf-8")
        rec["record"] = str(d / name)
    except OSError as e:
        log(f"기록을 {d} 에 쓰지 못함: {e}")


def execvp(argv: list[str]) -> None:  # 시험에서 바꿔 끼운다
    os.execvpe(argv[0], argv, child_env())


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
    if mode not in ("tier0", "queue", "check", "run"):
        execvp(args)  # 준비 없이 그대로 (sleep infinity 로 띄운 개발 Pod, 셸, 점검 도구)
        return EXIT_OK
    if mode == "run" and not rest:
        log("사용법: simai-bootstrap run <명령...>")
        return EXIT_USAGE
    want_set = "tier0" if mode == "tier0" else (os.environ.get(SET_ENV) or "tier0")
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
    execvp([TIER0_CMD, *rest])
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
