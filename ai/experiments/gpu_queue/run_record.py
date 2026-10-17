"""GPU 큐 Job 한 번의 Run 기록(contracts/experiments/run_record.schema.json)을 만든다 (명세 20.5 재현).

표준 라이브러리만 쓴다. 스키마 검증만 jsonschema·referencing 이 있을 때 한다(없으면 검증을 건너뛰었다고 알린다).
모르는 값은 지어내지 않는다: nvidia-smi 가 없으면 GPU 칸은 빈 문자열, 해시할 파일이 없으면 해시 칸을 뺀다.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

SCHEMA_VERSION = "2.0.0"  # run_record 계약 버전 (2.0.0: input_provenance.calibration.source 에서 v1.3 제외 보정 출처 삭제)
RUN_ID_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
METRIC_TYPES = (int, float, str, bool, type(None))


def now_ms() -> int:
    """UTC epoch 밀리초."""
    return time.time_ns() // 1_000_000


def safe_id(text: str) -> str:
    return RUN_ID_UNSAFE.sub("-", text).strip("-") or "x"


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def run_text(cmd: list[str], cwd: Path | None = None, timeout: float = 60) -> str | None:
    """명령 표준출력. 명령이 없거나 실패하면 None."""
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


class HashCache:
    """큰 가중치·데이터를 Job 마다 다시 읽지 않도록 (경로, 크기, 수정 시각)으로 해시를 기억한다."""

    def __init__(self, path: Path | None):
        self.path = path
        self.data: dict[str, str] = {}
        if path and path.is_file():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self.data = {}

    def file(self, path: Path) -> str:
        st = path.stat()
        key = f"{path.resolve()}|{st.st_size}|{st.st_mtime_ns}"
        if key not in self.data:
            self.data[key] = sha256_file(path)
            if self.path:
                write_json_atomic(self.path, self.data)
        return self.data[key]


def hash_paths(paths: list[str], base: Path, cache: HashCache, notes: list[str], label: str) -> str | None:
    """파일 하나면 그 파일 해시, 여럿이거나 폴더면 (상대 경로, 파일 해시) 목록의 해시. 하나라도 없으면 None."""
    if not paths:
        return None
    if len(paths) == 1 and resolve(base, paths[0]).is_file():
        return cache.file(resolve(base, paths[0]))
    entries: list[tuple[str, str]] = []
    for raw in paths:
        p = resolve(base, raw)
        if p.is_file():
            entries.append((raw, cache.file(p)))
        elif p.is_dir():
            files = sorted(q for q in p.rglob("*") if q.is_file())
            entries += [(f"{raw}/{q.relative_to(p).as_posix()}", cache.file(q)) for q in files]
        else:
            notes.append(f"{label} 경로 없음: {raw} → 해시 비움")
            return None
    return sha256_bytes(canonical(entries))


def resolve(base: Path, raw: str) -> Path:
    p = Path(raw)
    return p if p.is_absolute() else base / p


def write_json_atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for i in range(50):  # Windows 에서 읽는 쪽이 파일을 잡고 있으면 잠깐 실패한다
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.02 * (i + 1))
    os.replace(tmp, path)


# ---------------------------------------------------------------- git·GPU·환경


def git_info(repo_dir: Path | None, stamp: dict | None) -> dict:
    """저장소에서 직접 읽고, 저장소가 아니면(Pod 에 파일만 복사한 경우) 노트북에서 찍은 stamp 를 쓴다."""
    if repo_dir is not None:
        commit = run_text(["git", "rev-parse", "HEAD"], cwd=repo_dir)
        if commit:
            branch = run_text(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_dir) or ""
            status = run_text(["git", "status", "--porcelain"], cwd=repo_dir)
            return {
                "git_commit": commit.strip(),
                "branch": branch.strip(),
                "dirty_worktree": bool(status and status.strip()),
                "source": "repo",
            }
    if stamp and stamp.get("commit"):
        out = {"git_commit": stamp["commit"], "branch": stamp.get("branch", ""), "source": "stamp"}
        if "dirty_worktree" in stamp:
            out["dirty_worktree"] = bool(stamp["dirty_worktree"])
        return out
    return {"git_commit": "", "branch": "", "source": "none"}


def gpu_info(query_cmd: list[str]) -> dict:
    """nvidia-smi 로 GPU 모델명·드라이버·CUDA·메모리를 읽는다. 없으면 빈 값."""
    info: dict[str, Any] = {"model": "", "driver": "", "cuda": "", "memory_total_mb": None, "memory_used_mb": None}
    row = run_text([*query_cmd, "--query-gpu=name,driver_version,memory.total,memory.used", "--format=csv,noheader,nounits"], timeout=30)
    if row and row.strip():
        first = [v.strip() for v in row.strip().splitlines()[0].split(",")]
        if len(first) >= 4:
            info["model"], info["driver"] = first[0], first[1]
            try:
                info["memory_total_mb"], info["memory_used_mb"] = float(first[2]), float(first[3])
            except ValueError:
                pass
    head = run_text(list(query_cmd), timeout=30)
    if head:
        m = re.search(r"CUDA Version:\s*([0-9.]+)", head)
        if m:
            info["cuda"] = m.group(1)
    return info


def env_hash(env_cfg: dict, base: Path, cache: HashCache, notes: list[str]) -> str:
    """환경 해시: 이미지 다이제스트, 잠금 파일, freeze 출력을 묶는다. 이미지 다이제스트만 있으면 그 값 그대로."""
    parts: list[tuple[str, str]] = []
    digest = env_cfg.get("image_digest")
    if digest:
        parts.append(("image", digest))
    for raw in env_cfg.get("lock_files") or []:
        p = resolve(base, raw)
        if p.is_file():
            parts.append((raw, cache.file(p)))
        else:
            notes.append(f"env 잠금 파일 없음: {raw}")
    freeze_cmd = env_cfg.get("freeze_cmd")
    if freeze_cmd:
        out = run_text([str(c) for c in freeze_cmd], cwd=base, timeout=120)
        if out is None:
            notes.append("env freeze 명령 실패")
        else:
            parts.append(("freeze", sha256_bytes(out.encode("utf-8"))))
    if not parts:
        return ""
    notes.append("env_hash 구성: " + ", ".join(f"{k}={v}" for k, v in parts))
    if parts == [("image", digest)]:
        return digest
    return sha256_bytes(canonical(parts))


def to_uri(path: Path, volume: dict | None, datacenter: str) -> str:
    """Network Volume 아래 경로는 volume://<datacenter>/<상대 경로>, 그 밖은 절대 경로."""
    p = path.resolve()
    if volume and volume.get("root") and (datacenter or volume.get("datacenter")):
        root = Path(volume["root"]).resolve()
        try:
            rel = p.relative_to(root)
        except ValueError:
            pass
        else:
            return f"volume://{volume.get('datacenter') or datacenter}/{rel.as_posix()}"
    return p.as_posix()


def repo_relative(path: Path, repo_dir: Path | None) -> str | None:
    if repo_dir is None:
        return None
    try:
        return path.resolve().relative_to(repo_dir.resolve()).as_posix()
    except ValueError:
        return None


# ---------------------------------------------------------------- 기록 조립


def build_record(
    *,
    run_id: str,
    queue_name: str,
    job: dict,
    rec_cfg: dict,
    attempt: int,
    started_at: int,
    finished_at: int | None,
    status: str,
    failure_reason: str | None,
    exit_code: int | None,
    run_dir: Path,
    log_path: Path | None,
    workdir: Path,
    repo_dir: Path | None,
    job_file: Path,
    git: dict,
    gpu: dict,
    volume: dict | None,
    cache: HashCache,
    extra_notes: list[str] | None = None,
) -> dict:
    """Job 정의(rec_cfg 는 큐 기본값과 Job 값을 합친 것)와 실행 결과로 Run 기록을 만든다."""
    notes: list[str] = list(extra_notes or [])
    gpu_cfg = dict(rec_cfg.get("gpu") or {})
    provider = gpu_cfg.get("provider") or ("RUNPOD" if os.environ.get("RUNPOD_POD_ID") else "LOCAL")
    datacenter = gpu_cfg.get("datacenter") or os.environ.get("RUNPOD_DC_ID") or ""

    gpu_rec: dict[str, Any] = {"model": gpu["model"], "driver": gpu["driver"], "cuda": gpu["cuda"], "provider": provider}
    for key in ("region", "shared_with_tier0"):
        if key in gpu_cfg:
            gpu_rec[key] = gpu_cfg[key]
    if datacenter:
        gpu_rec["datacenter"] = datacenter
    if not gpu["model"]:
        notes.append("nvidia-smi 없음 → GPU 모델·드라이버·CUDA 비움")

    record: dict[str, Any] = {
        "schema_version": rec_cfg.get("schema_version", SCHEMA_VERSION),
        "run_id": run_id,
        "experiment_id": rec_cfg.get("experiment_id"),
    }
    if rec_cfg.get("e_gate"):
        record["e_gate"] = rec_cfg["e_gate"]
    record |= {
        "jira_key": rec_cfg.get("jira_key"),
        "owner": rec_cfg.get("owner"),
        "git_commit": git["git_commit"],
        "branch": git["branch"],
    }
    if "dirty_worktree" in git:
        record["dirty_worktree"] = git["dirty_worktree"]
    if git["source"] == "none":
        notes.append("git 정보 없음(저장소도 stamp 도 없음) → git_commit 비움")
    record["env_hash"] = env_hash(rec_cfg.get("env") or {}, workdir, cache, notes)
    record["model_version"] = rec_cfg.get("model_version", "")
    weights = hash_paths(list(rec_cfg.get("weights") or []), workdir, cache, notes, "가중치")
    if weights:
        record["weights_hash"] = weights
    record["config"] = _config(rec_cfg.get("config"), job, workdir, repo_dir, job_file, notes)

    provenance = json.loads(json.dumps(rec_cfg.get("input_provenance")))
    dataset = hash_paths(list(rec_cfg.get("dataset") or []), workdir, cache, notes, "데이터")
    if dataset and isinstance(provenance, dict) and isinstance(provenance.get("frames"), dict):
        provenance["frames"].setdefault("dataset_hash", dataset)
    record["input_provenance"] = provenance
    record["gpu"] = gpu_rec
    record["started_at"] = started_at
    if finished_at is not None:
        record["finished_at"] = finished_at
    record["status"] = status
    if failure_reason:
        record["failure_reason"] = failure_reason
    record["experiment_state"] = rec_cfg.get("experiment_state")
    record["metrics"] = _metrics(run_dir / "metrics.json", notes)
    performance = _read_obj(run_dir / "performance.json", notes)
    if performance:
        record["performance"] = performance

    results: list[dict] = []
    if log_path is not None and log_path.is_file():
        results.append({"kind": "LOG", "uri": to_uri(log_path, volume, datacenter), "sha256": sha256_file(log_path)})
    for item in rec_cfg.get("results") or []:
        p = resolve(workdir, item["path"])
        if p.is_file():
            results.append({"kind": item.get("kind", "OTHER"), "uri": to_uri(p, volume, datacenter), "sha256": cache.file(p)})
        elif p.exists():
            results.append({"kind": item.get("kind", "OTHER"), "uri": to_uri(p, volume, datacenter)})
        else:
            notes.append(f"결과 없음: {item['path']}")
    record["results"] = results

    if provider == "RUNPOD" and finished_at is not None:
        record["cost"] = {"gpu_hours": round((finished_at - started_at) / 3_600_000, 4)}
    record["review"] = None
    command = job["command"] if isinstance(job["command"], str) else " ".join(map(str, job["command"]))
    head = f"queue={queue_name} job={job['name']} attempt={attempt} exit_code={exit_code} command={command}"
    record["notes"] = "; ".join([head, *notes])
    return record


def _config(cfg: dict | None, job: dict, workdir: Path, repo_dir: Path | None, job_file: Path, notes: list[str]) -> dict:
    """config.path 는 저장소 상대 경로. 실제 파일 위치가 다르면(Pod 에 복사본) config.file 로 준다.
    config 가 없으면 Job 정의 파일 자체를 설정으로 기록한다."""
    if not cfg:
        rel = repo_relative(job_file, repo_dir) or job_file.name
        return {"path": rel, "sha256": sha256_file(job_file), "overrides": {"queue_job": job["name"]}}
    file = resolve(workdir, cfg.get("file") or cfg["path"])
    if not file.is_file() and repo_dir is not None:
        file = repo_dir / cfg["path"]
    out: dict[str, Any] = {"path": cfg["path"]}
    if file.is_file():
        out["sha256"] = sha256_file(file)
    else:
        notes.append(f"설정 파일 없음: {cfg['path']} → 해시 비움(기록 검증 실패)")
        out["sha256"] = ""
    if cfg.get("overrides"):
        out["overrides"] = cfg["overrides"]
    return out


def _read_obj(path: Path, notes: list[str]) -> dict | None:
    if not path.is_file():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        notes.append(f"{path.name} 읽기 실패: {e}")
        return None
    if not isinstance(obj, dict):
        notes.append(f"{path.name} 이 객체가 아님")
        return None
    return obj


def _metrics(path: Path, notes: list[str]) -> dict:
    """Job 이 $SIMAI_RUN_DIR/metrics.json 에 남긴 지표. 스칼라만 받는다."""
    obj = _read_obj(path, notes) or {}
    dropped = [k for k, v in obj.items() if not isinstance(v, METRIC_TYPES)]
    if dropped:
        notes.append("스칼라가 아닌 지표 제외: " + ", ".join(dropped))
    return {k: v for k, v in obj.items() if isinstance(v, METRIC_TYPES)}


# ---------------------------------------------------------------- 스키마 검증


def load_validator(contracts_dir: Path) -> tuple[Callable[[dict], list[str]] | None, str]:
    """run_record 스키마 검증 함수. jsonschema 나 contracts/ 가 없으면 (None, 이유)."""
    try:
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource
    except ImportError:
        return None, "jsonschema 가 없어 Run 기록 스키마 검증을 건너뜀 (uv run 으로 실행하면 설치됨)"
    schema_path = contracts_dir / "experiments" / "run_record.schema.json"
    if not schema_path.is_file():
        return None, f"스키마 없음: {schema_path} → 검증을 건너뜀"
    registry = Registry()
    for p in sorted(contracts_dir.rglob("*.schema.json")):
        s = json.loads(p.read_text(encoding="utf-8"))
        registry = registry.with_resource(s["$id"], Resource.from_contents(s))
    validator = Draft202012Validator(json.loads(schema_path.read_text(encoding="utf-8")), registry=registry)

    def validate(doc: dict) -> list[str]:
        return [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in validator.iter_errors(doc)]

    return validate, ""
