#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["jsonschema>=4.23", "referencing>=0.35"]
# ///
"""GPU Pod 에서 실험 Job 을 하나씩 순서대로 돌리는 큐 (명세 20.5, 3.2 Tier 0 자원 우선).

- Job 정의 파일(JSON)의 Job 을 차례로 실행한다. 같은 상태 폴더(Network Volume)를 쓰는 큐는 하나만 돈다(잠금).
- 잠금 파일에 heartbeat 를 쓰고, TTL 이 지나거나 같은 호스트에서 주인이 죽은 잠금은 회수한다.
- Job 마다 타임아웃, 종료 시 자식 프로세스 정리, 끝난 Job(성공·실패)은 재시작해도 다시 돌리지 않는다.
- Tier 0(LIVE)가 돌고 있으면 EXP Job 을 시작하지 않고, 돌던 Job 은 중단한다(LIVE 시간 EXP 금지).
- Job 마다 contracts/experiments/run_record.schema.json 형식의 Run 기록을 남기고 검증한다.

사용법은 같은 폴더의 README.md 를 본다. 표준 라이브러리만 쓰고, 스키마 검증에만 jsonschema 를 쓴다.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from run_record import (
    HashCache,
    build_record,
    git_info,
    gpu_info,
    load_validator,
    now_ms,
    resolve,
    safe_id,
    write_json_atomic,
)

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_GUESS = SCRIPT_DIR.parents[2]
IS_WIN = sys.platform == "win32"
SIMAI_ROOT = Path(os.environ.get("SIMAI_ROOT", "/workspace/simai"))
TOKEN_ENV = "SIMAI_QUEUE_TOKEN"  # Job 프로세스 트리를 찾는 표식. 자식·손자 프로세스에 그대로 이어진다
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
FINISHED = {"succeeded", "failed"}  # 재시작해도 다시 돌리지 않는 상태
REQUIRED_RECORD = ("experiment_id", "jira_key", "owner", "experiment_state", "model_version", "input_provenance")

EXIT_OK, EXIT_JOB_FAILED, EXIT_LOCKED, EXIT_LIVE, EXIT_BAD_JOBS, EXIT_SIGNAL = 0, 1, 2, 3, 4, 130

DEFAULT_GUARD = {
    "enabled": True,
    # Tier 0 LIVE 서비스가 떠 있는 동안 두는 표시 파일
    "marker_files": [str(SIMAI_ROOT / "state" / "tier0.live")],
    "marker_max_age_s": None,  # 설정하면 이보다 오래 갱신되지 않은 표시 파일은 무시한다(Tier 0 비정상 종료 대비)
    "process_patterns": [r"-m\s+services\.tier0\b"],  # Linux /proc 명령줄 정규식
    "check_command": None,  # 종료 코드 0 이면 LIVE 로 본다 (예: 헬스 엔드포인트 확인)
    "on_live": "wait",  # wait: LIVE 가 끝날 때까지 대기 / stop: 큐를 멈추고 종료 코드 3
    "poll_s": 15,
    "max_wait_s": 3600,
}


class JobFileError(Exception):
    pass


def utc_stamp(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def merge_record(base: dict | None, over: dict | None) -> dict:
    """큐 기본값(record)에 Job 값(jobs[].record)을 덮는다. gpu·env 는 한 단계 안쪽까지 합친다."""
    out = copy.deepcopy(base or {})
    for k, v in (over or {}).items():
        if k in ("gpu", "env") and isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_job_file(path: Path) -> dict:
    """Job 정의 파일을 읽고 형식을 검사한다. GPU 를 켜기 전에 노트북에서 잡히도록 엄격하게 본다."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise JobFileError(f"Job 파일을 읽을 수 없음: {e}") from e
    errs: list[str] = []
    q = doc.get("queue") or {}
    if not NAME_RE.match(str(q.get("name", ""))):
        errs.append("queue.name 은 영문·숫자·._- 로 쓴다")
    guard = {**DEFAULT_GUARD, **(q.get("live_guard") or {})}
    if guard["on_live"] not in ("wait", "stop"):
        errs.append("queue.live_guard.on_live 는 wait 또는 stop")
    jobs = doc.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        errs.append("jobs 가 비어 있음")
        jobs = []
    seen: set[str] = set()
    for i, job in enumerate(jobs):
        where = f"jobs[{i}]"
        name = str(job.get("name", ""))
        if not NAME_RE.match(name):
            errs.append(f"{where}.name 은 영문·숫자·._- 로 쓴다")
        if name in seen:
            errs.append(f"{where}.name 중복: {name}")
        seen.add(name)
        cmd = job.get("command")
        if not (isinstance(cmd, str) and cmd.strip()) and not (isinstance(cmd, list) and cmd and all(isinstance(c, (str, int, float)) for c in cmd)):
            errs.append(f"{where}.command 는 문자열(셸) 또는 인자 목록")
        for key, minimum in (("timeout_s", 1e-9), ("expected_s", 0), ("vram_mb", 0)):
            v = job.get(key)
            if not isinstance(v, (int, float)) or isinstance(v, bool) or v < minimum:
                errs.append(f"{where}.{key} 는 {'양수' if minimum else '0 이상의 수'} (필수)")
        rec = merge_record(doc.get("record"), job.get("record"))
        missing = [k for k in REQUIRED_RECORD if k not in rec]
        if missing:
            errs.append(f"{where} Run 기록 필수 값 없음(record 또는 jobs[].record): {', '.join(missing)}")
        if rec.get("config") and not (isinstance(rec["config"], dict) and rec["config"].get("path")):
            errs.append(f"{where}.record.config 에는 저장소 상대 경로 path 가 있어야 한다")
        if any(not isinstance(r, dict) or not r.get("path") for r in rec.get("results") or []):
            errs.append(f"{where}.record.results 의 항목은 {{kind, path}}")
    if errs:
        raise JobFileError("\n".join(errs))
    return doc


# ---------------------------------------------------------------- 프로세스


def pid_alive(pid: int) -> bool:
    if IS_WIN:
        import ctypes

        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _proc_entries():
    """Linux /proc 의 (pid, 명령줄, 환경 바이트). 다른 사용자 프로세스의 환경은 b'' 다."""
    proc = Path("/proc")
    if not proc.is_dir():
        return
    for d in proc.iterdir():
        if not d.name.isdigit():
            continue
        try:
            cmdline = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except OSError:
            continue
        try:
            environ = (d / "environ").read_bytes()
        except OSError:
            environ = b""
        yield int(d.name), cmdline, environ


def token_pids(token: str) -> list[int]:
    needle = f"{TOKEN_ENV}={token}".encode()
    return [pid for pid, _, env in _proc_entries() if needle in env.split(b"\0") and pid != os.getpid()]


def kill_job_tree(pid: int, token: str, grace_s: float) -> None:
    """Job 프로세스와 그 자손을 끝낸다. POSIX 는 프로세스 그룹 TERM → KILL 뒤 표식이 남은 프로세스를 KILL."""
    if IS_WIN:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        return
    try:
        os.killpg(pid, signal.SIGTERM)
        deadline = time.monotonic() + grace_s
        while time.monotonic() < deadline:
            os.killpg(pid, 0)
            time.sleep(0.1)
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    sweep_token(token)


def sweep_token(token: str) -> list[int]:
    """그룹을 벗어난 자손(setsid 등)까지 표식으로 찾아 끝낸다. GPU 메모리를 잡은 채 남지 않게 한다."""
    if IS_WIN:
        return []
    pids = token_pids(token)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return pids


# ---------------------------------------------------------------- LIVE 보호


class LiveGuard:
    """Tier 0(LIVE)가 돌고 있는지 본다. 표시 파일, 프로세스 명령줄, 확인 명령 중 하나라도 맞으면 LIVE."""

    def __init__(self, cfg: dict, workdir: Path, log):
        c = {**DEFAULT_GUARD, **(cfg or {})}
        self.enabled = bool(c["enabled"])
        self.markers = [resolve(workdir, m) for m in c["marker_files"] or []]
        self.max_age_s = c["marker_max_age_s"]
        self.patterns = [re.compile(p) for p in c["process_patterns"] or []]
        self.check_command = c["check_command"]
        self.on_live = c["on_live"]
        self.poll_s = float(c["poll_s"])
        self.max_wait_s = float(c["max_wait_s"])
        self.workdir = workdir
        self.log = log
        if not self.enabled:
            log("경고: live_guard.enabled=false → LIVE 관제 보호 없음. 관제가 없는 시간에만 쓴다")
        elif self.patterns and IS_WIN:
            log("참고: 프로세스 명령줄 확인은 Linux(/proc)에서만 한다. 이 OS 에서는 표시 파일·확인 명령만 본다")

    def reason(self) -> str | None:
        if not self.enabled:
            return None
        for m in self.markers:
            try:
                age = time.time() - m.stat().st_mtime
            except OSError:
                continue
            if self.max_age_s is not None and age > self.max_age_s:
                continue
            return f"LIVE 표시 파일 {m}"
        if self.patterns:
            for pid, cmdline, env in _proc_entries() or ():
                if pid == os.getpid() or f"{TOKEN_ENV}=".encode() in env:
                    continue  # 큐 자신과 큐가 띄운 Job 은 Tier 0 가 아니다
                if any(p.search(cmdline) for p in self.patterns):
                    return f"Tier 0 프로세스 pid={pid}: {cmdline[:120]}"
        if self.check_command:
            cmd = self.check_command
            try:
                r = subprocess.run(cmd, shell=isinstance(cmd, str), cwd=self.workdir, capture_output=True, timeout=30)
            except (OSError, subprocess.SubprocessError):
                return None
            if r.returncode == 0:
                return "LIVE 확인 명령 성공"
        return None


# ---------------------------------------------------------------- 잠금·heartbeat


class QueueLock:
    """O_EXCL 로 만드는 잠금 파일. Network Volume 에서도 쓸 수 있게 flock 대신 파일 생성과 heartbeat 로 한다."""

    def __init__(self, path: Path, ttl_s: float, heartbeat_s: float, queue_name: str, log):
        self.path = path
        self.ttl_s = ttl_s
        self.heartbeat_s = heartbeat_s
        self.token = uuid.uuid4().hex
        self.host = socket.gethostname()
        self.queue_name = queue_name
        self.started_at = now_ms()
        self.current: dict = {}
        self.lost = threading.Event()
        self._write = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.log = log

    def payload(self) -> dict:
        return {
            "token": self.token,
            "queue": self.queue_name,
            "host": self.host,
            "pid": os.getpid(),
            "started_at": self.started_at,
            "heartbeat_at": now_ms(),
            "ttl_s": self.ttl_s,
            "current": self.current,
        }

    def read(self) -> dict | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def stale_reason(self, cur: dict | None) -> str | None:
        if cur is None:
            try:
                age = time.time() - self.path.stat().st_mtime
            except OSError:
                return "잠금 파일이 사라짐"
            return f"읽을 수 없는 잠금, {age:.0f}s 동안 갱신 없음" if age > self.ttl_s else None
        age = (now_ms() - int(cur.get("heartbeat_at", 0))) / 1000
        if age > float(cur.get("ttl_s", self.ttl_s)):
            return f"heartbeat {age:.0f}s 전 (TTL {cur.get('ttl_s', self.ttl_s)}s)"
        if cur.get("host") == self.host and not pid_alive(int(cur.get("pid", -1))):
            return f"같은 호스트의 주인 pid {cur.get('pid')} 가 없음"
        return None

    def acquire(self) -> tuple[bool, dict | None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(5):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                cur = self.read()
                why = self.stale_reason(cur)
                if why is None:
                    return False, cur
                self.log(f"오래된 잠금 회수: {why} (이전 주인 {cur and cur.get('host')}:{cur and cur.get('pid')})")
                grave = self.path.with_name(f"{self.path.name}.stale-{self.token}")
                try:
                    os.rename(self.path, grave)  # 둘이 동시에 회수해도 rename 은 하나만 성공한다
                    os.remove(grave)
                except FileNotFoundError:
                    pass
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.payload(), f, ensure_ascii=False)
            self._thread = threading.Thread(target=self._beat, name="queue-heartbeat", daemon=True)
            self._thread.start()
            return True, None
        return False, self.read()

    def beat(self, current: dict | None = None) -> None:
        """heartbeat 를 쓴다. current 를 주면 지금 도는 Job 정보도 바꾼다(status 와 다른 큐가 바로 보도록)."""
        with self._write:
            if current is not None:
                self.current = current
            cur = self.read()
            if cur is not None and cur.get("token") != self.token:
                if not self.lost.is_set():
                    self.log(f"잠금을 잃음: 다른 큐 {cur.get('host')}:{cur.get('pid')} 가 회수함")
                self.lost.set()
                return
            write_json_atomic(self.path, self.payload())

    def _beat(self) -> None:
        while not self._stop.wait(self.heartbeat_s):
            if self.lost.is_set():
                return
            self.beat()

    def release(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        cur = self.read()
        if cur and cur.get("token") == self.token:
            try:
                os.remove(self.path)
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------- 큐


class Queue:
    def __init__(self, doc: dict, job_file: Path, state_root: Path, repo_dir: Path | None, contracts_dir: Path, persist: bool = True):
        """persist=False(check)면 상태 폴더에 아무것도 만들지 않는다(노트북에 /workspace 경로가 생기지 않게)."""
        self.doc = doc
        self.job_file = job_file.resolve()
        q = doc["queue"]
        self.name = q["name"]
        self.workdir = resolve(self.job_file.parent, q.get("workdir", ".")).resolve()
        self.continue_on_failure = bool(q.get("continue_on_failure", True))
        self.poll_s = float(q.get("poll_s", 1.0))
        self.kill_grace_s = float(q.get("kill_grace_s", 10))
        self.vram_wait_s = float(q.get("vram_wait_s", 300))
        self.gpu_query_cmd = [str(c) for c in q.get("gpu_query_cmd", ["nvidia-smi"])]
        self.volume = q.get("volume")
        lock_cfg = q.get("lock") or {}
        self.state_root = state_root
        self.qdir = state_root / self.name
        self.state_path = self.qdir / "state.json"
        self.repo_dir = repo_dir
        self.contracts_dir = contracts_dir
        self.stop = threading.Event()
        self._log_file = self.qdir / "queue.log" if persist else None
        if persist:
            self.qdir.mkdir(parents=True, exist_ok=True)
        self.lock = QueueLock(
            state_root / "queue.lock", float(lock_cfg.get("ttl_s", 120)), float(lock_cfg.get("heartbeat_s", 15)), self.name, self.log
        )
        self.guard = LiveGuard(q.get("live_guard") or {}, self.workdir, self.log)
        self.cache = HashCache(state_root / "hash_cache.json" if persist else None)
        self.validate, why = load_validator(contracts_dir)
        if why:
            self.log(f"경고: {why}")
        self.state: dict = {"queue": self.name, "jobs": {}}
        self.records_invalid = 0

    # -- 공용

    def log(self, msg: str) -> None:
        line = f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}Z] {msg}"
        print(line, flush=True)
        if self._log_file is not None:
            with open(self._log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def jobs(self) -> list[dict]:
        return self.doc["jobs"]

    def record_cfg(self, job: dict) -> dict:
        return merge_record(self.doc.get("record"), job.get("record"))

    def load_state(self) -> None:
        if self.state_path.is_file():
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
        for job in self.jobs():
            self.state["jobs"].setdefault(job["name"], {"status": "pending", "attempts": 0, "runs": []})

    def save_state(self) -> None:
        self.state["updated_at"] = now_ms()
        write_json_atomic(self.state_path, self.state)

    def _record(self, job: dict, **kw) -> bool:
        """Run 기록을 쓰고 스키마로 검증한다. 검증 실패도 기록은 남기고 큐 종료 코드로 알린다."""
        record = build_record(
            queue_name=self.name,
            job=job,
            rec_cfg=self.record_cfg(job),
            workdir=self.workdir,
            repo_dir=self.repo_dir,
            job_file=self.job_file,
            git=self.git,
            gpu=self.gpu,
            volume=self.volume,
            cache=self.cache,
            **kw,
        )
        path = kw["run_dir"] / "run_record.json"
        write_json_atomic(path, record)
        if self.validate is None:
            return True
        errors = self.validate(record)
        for e in errors:
            self.log(f"  Run 기록 스키마 위반 {record['run_id']}: {e}")
        if errors:
            self.records_invalid += 1
        return not errors

    # -- 확인(dry run)

    def dry_run_errors(self) -> list[str]:
        """실제 실행 전에 Job 마다 기록을 미리 만들어 스키마를 통과하는지 본다(큰 파일 해시·freeze 는 생략)."""
        self.git = git_info(self.repo_dir, self.doc.get("git"))
        self.gpu = {"model": "", "driver": "", "cuda": ""}
        errs: list[str] = []
        if not self.git["git_commit"]:
            errs.append("git 정보 없음: 저장소 안에서 실행하거나 노트북에서 stamp 로 git 값을 넣은 Job 파일을 쓴다")
        if self.validate is None:
            return errs
        for job in self.jobs():
            rec_cfg = self.record_cfg(job)
            light = {k: v for k, v in rec_cfg.items() if k not in ("weights", "dataset", "env", "results")}
            record = build_record(
                run_id=safe_id(f"{self.name}-{job['name']}-dryrun"),
                queue_name=self.name,
                job=job,
                rec_cfg=light,
                attempt=0,
                started_at=now_ms(),
                finished_at=now_ms(),
                status="SUCCEEDED",
                failure_reason=None,
                exit_code=0,
                run_dir=self.qdir / "_dryrun",
                log_path=None,
                workdir=self.workdir,
                repo_dir=self.repo_dir,
                job_file=self.job_file,
                git=self.git,
                gpu=self.gpu,
                volume=self.volume,
                cache=self.cache,
            )
            errs += [f"{job['name']}: {e}" for e in self.validate(record)]
        return errs

    # -- 실행

    def run(self, rerun: list[str], rerun_failed: bool) -> int:
        errs = self.dry_run_errors()
        if errs:
            for e in errs:
                self.log(f"Job 정의 오류: {e}")
            return EXIT_BAD_JOBS
        ok, cur = self.lock.acquire()
        if not ok:
            self.log(f"다른 큐가 실행 중이라 시작하지 않음: {cur}")
            return EXIT_LOCKED
        old = {}
        for sig in (signal.SIGINT, signal.SIGTERM):
            old[sig] = signal.signal(sig, lambda *_: self.stop.set())
        try:
            return self._run_locked(rerun, rerun_failed)
        finally:
            for sig, h in old.items():
                signal.signal(sig, h)
            self.lock.release()

    def _run_locked(self, rerun: list[str], rerun_failed: bool) -> int:
        self.git = git_info(self.repo_dir, self.doc.get("git"))
        self.gpu = gpu_info(self.gpu_query_cmd)
        self.load_state()
        self._recover()
        for name, st in self.state["jobs"].items():
            if name in rerun or (rerun_failed and st["status"] == "failed"):
                st["status"] = "pending"
        self.save_state()

        todo = [j for j in self.jobs() if self.state["jobs"][j["name"]]["status"] not in FINISHED]
        done = [j["name"] for j in self.jobs() if j not in todo]
        if done:
            self.log(f"이미 끝난 Job 건너뜀: {', '.join(done)}")
        total = sum(j["expected_s"] for j in todo)
        self.log(f"큐 {self.name}: Job {len(todo)}개, 예상 {total / 60:.1f}분 (타임아웃 합 {sum(j['timeout_s'] for j in todo) / 60:.1f}분)")

        code = EXIT_OK
        for job in todo:
            st = self.state["jobs"][job["name"]]
            while True:
                if not self._wait_not_live():
                    return self._early_exit()
                ok, why = self._vram_ok(job)
                if self.stop.is_set() or self.lock.lost.is_set():
                    return self._early_exit()
                if not ok:
                    self._abort_without_start(job, st, why)
                    break
                outcome = self._execute(job, st)
                if outcome == "preempted":
                    continue  # LIVE 가 끝나면 같은 Job 을 다시 돈다(on_live=stop 이면 위에서 멈춤)
                if outcome == "aborted":
                    return self._early_exit()
                break
            if st["status"] == "failed":
                code = EXIT_JOB_FAILED
                if not job.get("continue_on_failure", self.continue_on_failure):
                    self.log(f"{job['name']} 실패, continue_on_failure=false → 큐 중단")
                    break
        if self.records_invalid:
            self.log(f"스키마를 통과하지 못한 Run 기록 {self.records_invalid}개")
            code = code or EXIT_JOB_FAILED
        self.log(f"큐 종료 (코드 {code})")
        return code

    def _early_exit(self) -> int:
        self.save_state()
        if self.stop.is_set():
            self.log("중지 신호로 큐 종료. 남은 Job 은 다음 실행 때 이어서 돈다")
            return EXIT_SIGNAL
        if self.lock.lost.is_set():
            self.log("잠금을 잃어 큐 종료. 잠금을 가져간 큐를 확인한다")
            return EXIT_LOCKED
        self.log("LIVE 관제 때문에 큐 종료. 남은 Job 은 다음 실행 때 이어서 돈다")
        return EXIT_LIVE

    def _recover(self) -> None:
        """이전 큐가 Job 을 돌리다 죽었으면: 남은 프로세스를 끝내고 ABORTED 기록을 남긴 뒤 다시 돌 Job 으로 둔다."""
        for job in self.jobs():
            st = self.state["jobs"][job["name"]]
            if st["status"] != "running":
                continue
            self.log(f"복구: {job['name']} 가 실행 중 상태로 남아 있음 (run {st.get('run_id')})")
            if st.get("token"):
                killed = sweep_token(st["token"])
                if IS_WIN and st.get("host") == socket.gethostname() and st.get("pid") and pid_alive(st["pid"]):
                    kill_job_tree(st["pid"], st["token"], 0)
                    killed = [st["pid"]]
                if killed:
                    self.log(f"  남은 Job 프로세스 정리: {killed}")
            run_dir = self.qdir / "runs" / st["run_id"]
            if not (run_dir / "run_record.json").is_file():
                self._record(
                    job,
                    run_id=st["run_id"],
                    attempt=st["attempts"],
                    started_at=st["started_at"],
                    finished_at=None,  # 언제 끝났는지 모른다
                    status="ABORTED",
                    failure_reason="큐가 Job 실행 중에 끝남(재시작 때 발견)",
                    exit_code=None,
                    run_dir=run_dir,
                    log_path=run_dir / "job.log",
                )
            st["status"] = "aborted"

    def _wait_not_live(self) -> bool:
        reason = self.guard.reason()
        if reason is None:
            return True
        if self.guard.on_live == "stop":
            self.log(f"LIVE 관제 중이라 EXP Job 을 시작하지 않음: {reason}")
            return False
        self.log(f"LIVE 관제 중 → EXP Job 대기 (최대 {self.guard.max_wait_s:.0f}s): {reason}")
        deadline = time.monotonic() + self.guard.max_wait_s
        while reason is not None:
            if self.stop.is_set() or self.lock.lost.is_set():
                return False
            if time.monotonic() >= deadline:
                self.log("LIVE 대기 시간 초과")
                return False
            self.stop.wait(self.guard.poll_s)
            reason = self.guard.reason()
        self.log("LIVE 관제 없음 → 재개")
        return True

    def _vram_ok(self, job: dict) -> tuple[bool, str]:
        need = float(job["vram_mb"])
        if need <= 0:
            return True, ""
        deadline = time.monotonic() + self.vram_wait_s
        while True:
            g = gpu_info(self.gpu_query_cmd)
            if g["memory_total_mb"] is None:
                self.log(f"nvidia-smi 없음: {job['name']} 의 VRAM {need:.0f} MB 확인 생략")
                return True, ""
            free = g["memory_total_mb"] - g["memory_used_mb"]
            if free >= need:
                return True, ""
            if time.monotonic() >= deadline or self.stop.is_set():
                return False, f"VRAM 부족: 필요 {need:.0f} MB, 남은 {free:.0f} MB ({self.vram_wait_s:.0f}s 대기)"
            self.stop.wait(self.poll_s)

    def _begin(self, job: dict, st: dict) -> tuple[str, Path, int]:
        st["attempts"] += 1
        started = now_ms()
        run_id = safe_id(f"{self.name}-{job['name']}-{utc_stamp(started)}-a{st['attempts']}")
        run_dir = self.qdir / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        st["runs"].append(run_id)
        st.update(run_id=run_id, started_at=started, host=socket.gethostname())
        return run_id, run_dir, started

    def _abort_without_start(self, job: dict, st: dict, why: str) -> None:
        run_id, run_dir, started = self._begin(job, st)
        self.log(f"{job['name']} 시작 안 함: {why}")
        st.update(status="aborted", finished_at=now_ms(), exit_code=None, reason=why)
        st["record_valid"] = self._record(
            job, run_id=run_id, attempt=st["attempts"], started_at=started, finished_at=st["finished_at"],
            status="ABORTED", failure_reason=why, exit_code=None, run_dir=run_dir, log_path=None,
        )
        self.save_state()

    def _execute(self, job: dict, st: dict) -> str:
        """Job 하나를 돌린다. 반환: done(성공·실패 기록), preempted(LIVE 로 중단), aborted(신호·잠금 잃음)."""
        run_id, run_dir, started = self._begin(job, st)
        log_path = run_dir / "job.log"
        token = uuid.uuid4().hex
        env = {**os.environ, **{k: str(v) for k, v in (job.get("env") or {}).items()}}
        env |= {TOKEN_ENV: token, "SIMAI_RUN_ID": run_id, "SIMAI_RUN_DIR": str(run_dir), "SIMAI_QUEUE_NAME": self.name}
        cmd = job["command"]
        shell = isinstance(cmd, str)
        if shell and not IS_WIN:
            cmd, shell = ["/bin/bash" if Path("/bin/bash").exists() else "/bin/sh", "-c", cmd], False
        elif not shell:
            cmd = [str(c) for c in cmd]
        cwd = resolve(self.workdir, job.get("cwd", "."))
        kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if IS_WIN else {"start_new_session": True}

        st.update(status="running", token=token, pid=None)
        self.lock.beat({"job": job["name"], "run_id": run_id, "started_at": started, "expected_s": job["expected_s"]})
        self.save_state()
        self.log(f"▶ {job['name']} 시작 (run {run_id}, 예상 {job['expected_s']}s, 타임아웃 {job['timeout_s']}s)")

        status, reason, outcome, rc = None, None, "done", None
        with open(log_path, "ab") as logf:
            try:
                proc = subprocess.Popen(cmd, shell=shell, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT, **kwargs)
            except OSError as e:
                status, reason = "FAILED", f"실행 실패: {e}"
            else:
                st["pid"] = proc.pid
                self.save_state()
                rc, status, reason, outcome = self._watch(job, proc, token)
        finished = now_ms()
        if status is None:
            status = "SUCCEEDED" if rc == 0 else "FAILED"
            reason = None if rc == 0 else f"종료 코드 {rc}"

        st.update(finished_at=finished, exit_code=rc, reason=reason)
        st["status"] = {"SUCCEEDED": "succeeded", "FAILED": "failed"}.get(status, "aborted")
        st["record_valid"] = self._record(
            job, run_id=run_id, attempt=st["attempts"], started_at=started, finished_at=finished, status=status,
            failure_reason=reason, exit_code=rc, run_dir=run_dir, log_path=log_path,
        )
        self.lock.beat({})
        self.save_state()
        mark = "✔" if status == "SUCCEEDED" else "✘"
        self.log(f"{mark} {job['name']} {status} ({(finished - started) / 1000:.1f}s){': ' + reason if reason else ''}")
        return outcome

    def _watch(self, job: dict, proc: subprocess.Popen, token: str) -> tuple[int | None, str | None, str | None, str]:
        start = time.monotonic()
        deadline = start + float(job["timeout_s"])
        next_live = start + self.guard.poll_s
        warned = False
        status = reason = None
        outcome = "done"
        while True:
            try:
                rc = proc.wait(timeout=self.poll_s)
                break
            except subprocess.TimeoutExpired:
                pass
            now = time.monotonic()
            if now >= deadline:
                status, reason = "FAILED", f"타임아웃 {job['timeout_s']}s 초과"
            elif self.stop.is_set():
                status, reason, outcome = "ABORTED", "큐 중지 신호", "aborted"
            elif self.lock.lost.is_set():
                status, reason, outcome = "ABORTED", "큐 잠금을 잃음", "aborted"
            elif now >= next_live:
                next_live = now + self.guard.poll_s
                live = self.guard.reason()
                if live:
                    status, reason, outcome = "ABORTED", f"LIVE 관제 시작으로 중단: {live}", "preempted"
            if status is None and not warned and job["expected_s"] and now - start > job["expected_s"]:
                warned = True
                self.log(f"  {job['name']} 가 예상 시간 {job['expected_s']}s 를 넘김")
            if status is not None:
                self.log(f"  {job['name']} 중단: {reason}")
                kill_job_tree(proc.pid, token, self.kill_grace_s)
                try:
                    rc = proc.wait(timeout=self.kill_grace_s + 5)
                except subprocess.TimeoutExpired:
                    rc = None
                break
        if status is None and not IS_WIN:
            kill_job_tree(proc.pid, token, self.kill_grace_s)  # 정상 종료여도 남긴 자식(백그라운드 서버 등)을 정리
        return rc, status, reason, outcome


# ---------------------------------------------------------------- CLI


def status_report(state_root: Path, name: str) -> str:
    lines = []
    lock_path = state_root / "queue.lock"
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        lock = None
    if lock:
        age = (now_ms() - lock["heartbeat_at"]) / 1000
        stale = " (TTL 초과: 큐가 죽었을 수 있음)" if age > lock.get("ttl_s", 120) else ""
        lines.append(f"잠금: 큐 {lock['queue']} {lock['host']}:{lock['pid']}, heartbeat {age:.0f}s 전{stale}, 현재 {lock.get('current') or '-'}")
    else:
        lines.append("잠금: 없음 (도는 큐 없음)")
    try:
        state = json.loads((state_root / name / "state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        lines.append(f"큐 {name}: 상태 파일 없음")
        return "\n".join(lines)
    for job, st in state["jobs"].items():
        extra = f" exit={st.get('exit_code')}" if "exit_code" in st else ""
        lines.append(f"  {job:<28} {st['status']:<10} 시도 {st['attempts']}{extra} {st.get('reason') or ''}".rstrip())
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="GPU 실험 Job 큐 (README.md 참고)")
    ap.add_argument("--state-dir", type=Path, default=Path(os.environ.get("SIMAI_QUEUE_DIR", SIMAI_ROOT / "queue")),
                    help="잠금·상태·로그·Run 기록 폴더 (기본 $SIMAI_QUEUE_DIR 또는 $SIMAI_ROOT/queue)")
    ap.add_argument("--repo", type=Path, default=REPO_GUESS, help="git 정보를 읽을 저장소 (기본: 이 스크립트가 있는 저장소)")
    ap.add_argument("--contracts", type=Path, default=REPO_GUESS / "contracts", help="contracts/ 폴더 (Run 기록 스키마)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run", help="큐 실행")
    p_run.add_argument("jobs", type=Path)
    p_run.add_argument("--rerun", action="append", default=[], metavar="JOB", help="끝난 Job 을 다시 돌린다 (여러 번 줄 수 있음)")
    p_run.add_argument("--rerun-failed", action="store_true", help="실패한 Job 을 다시 돌린다")
    p_check = sub.add_parser("check", help="Job 파일과 Run 기록 형식만 확인 (노트북에서, GPU 없이)")
    p_check.add_argument("jobs", type=Path)
    p_stamp = sub.add_parser("stamp", help="노트북 저장소의 git 정보를 Job 파일에 넣어 Pod 로 보낼 사본을 만든다")
    p_stamp.add_argument("jobs", type=Path)
    p_stamp.add_argument("-o", "--out", type=Path, required=True)
    p_status = sub.add_parser("status", help="잠금·Job 상태 보기")
    p_status.add_argument("jobs", type=Path)
    args = ap.parse_args(argv)

    repo = args.repo if (args.repo / ".git").exists() else None
    try:
        doc = load_job_file(args.jobs)
    except JobFileError as e:
        print(f"Job 파일 오류 ({args.jobs}):\n{e}", file=sys.stderr)
        return EXIT_BAD_JOBS

    if args.cmd == "status":
        print(status_report(args.state_dir, doc["queue"]["name"]))
        return EXIT_OK
    if args.cmd == "stamp":
        git = git_info(repo, None)
        if git["source"] != "repo":
            print("저장소에서 git 정보를 읽지 못함", file=sys.stderr)
            return EXIT_BAD_JOBS
        if git["dirty_worktree"]:
            print("경고: 커밋되지 않은 변경이 있음 → Run 기록에 dirty_worktree=true 로 남는다", file=sys.stderr)
        doc["git"] = {"commit": git["git_commit"], "branch": git["branch"], "dirty_worktree": git["dirty_worktree"]}
        args.out.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{args.out} ← git {git['git_commit'][:12]} ({git['branch']})")
        return EXIT_OK

    queue = Queue(doc, args.jobs, args.state_dir, repo, args.contracts, persist=args.cmd == "run")
    if args.cmd == "check":
        errs = queue.dry_run_errors()
        for e in errs:
            print(f"오류: {e}", file=sys.stderr)
        total = sum(j["expected_s"] for j in doc["jobs"])
        print(f"Job {len(doc['jobs'])}개, 예상 {total / 60:.1f}분, Run 기록 형식 {'오류' if errs else '통과'}")
        return EXIT_BAD_JOBS if errs else EXIT_OK
    return queue.run(args.rerun, args.rerun_failed)


if __name__ == "__main__":
    sys.exit(main())
