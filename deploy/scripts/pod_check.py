#!/usr/bin/env python3
"""RunPod Pod 점검 (S15P31A101-31, GPU 1차 세션 큐 a 항목). 표준 라이브러리만 쓴다.

GPU·드라이버·CUDA(Tier 0 이미지는 CUDA 13·드라이버 580 이상, 아래면 순방향 호환 필요), Network Volume 마운트,
볼륨 디렉터리, 용량, 쓰기 속도, fsync 시간, 원자적 rename·파일 잠금(저널 단일 작성자)을 확인하고 JSON 으로 남긴다.
비밀 파일은 있는지와 크기만 본다(내용을 읽지 않는다).

    python deploy/scripts/pod_check.py                       # Pod: /workspace, 결과는 $SIMAI_RUN_DIR/pod_check.json
    python deploy/scripts/pod_check.py --root /tmp/vol --allow-not-mount --nvidia-smi "python fake_smi.py"   # 노트북 드라이런

종료 코드: 0 필수 항목 통과(WARN 은 있을 수 있음), 1 필수 항목 실패, 2 인자·결과 파일 오류.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"
# docs/design/runpod-environment.md 의 볼륨 구조. 없으면 WARN(처음 쓰는 볼륨이면 정상), weights 만 없으면 FAIL 이 아니다
VOLUME_DIRS = ("weights", "datasets", "runs", "recordings", "obs2d", "engines", "state", "queue", "setup", "logs")
MIN_DRIVER = 580  # CUDA 13.0 이 요구하는 드라이버 (deploy/README.md "Tier 0 가 CUDA 13 인 이유")
MIN_CUDA = (13, 0)
DEFAULT_SMI = "nvidia-smi"


class Report:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, name: str, status: str, detail: str = "", **values) -> dict:
        item = {"name": name, "status": status, "detail": detail, **values}
        self.items.append(item)
        print(f"{status:<4}  {name}: {detail}", flush=True)
        return item

    def worst(self) -> str:
        order = {PASS: 0, SKIP: 0, WARN: 1, FAIL: 2}
        return max((i["status"] for i in self.items), key=lambda s: order[s], default=PASS)


def smi_command(text: str) -> list[str]:
    """nvidia-smi 명령. JSON 목록(["python", "C:/.../fake.py"]) 또는 셸 형식 문자열."""
    text = text.strip()
    if text.startswith("["):
        return [str(c) for c in json.loads(text)]
    return shlex.split(text)


def run(cmd: list[str], timeout: float = 30) -> tuple[int | None, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"{type(e).__name__}: {e}"
    return r.returncode, (r.stdout or "") + (r.stderr or "")


# ---------------------------------------------------------------- GPU


def check_gpu(rep: Report, smi: list[str]) -> dict:
    rc, out = run([*smi, "--query-gpu=name,driver_version,compute_cap,memory.total,memory.used",
                   "--format=csv,noheader,nounits"])
    gpu: dict = {"query_ok": rc == 0}
    if rc != 0 or not out.strip():
        rep.add("gpu", FAIL, f"nvidia-smi 실패 (코드 {rc}): {out.strip()[:200]}")
        return gpu
    rows = [[v.strip() for v in line.split(",")] for line in out.strip().splitlines() if line.strip()]
    first = rows[0]
    gpu |= {"count": len(rows), "name": first[0], "driver": first[1] if len(first) > 1 else ""}
    if len(first) >= 5:
        gpu |= {"compute_cap": first[2], "memory_total_mb": _num(first[3]), "memory_used_mb": _num(first[4])}
    elif len(first) >= 4:  # compute_cap 를 모르는 오래된 nvidia-smi
        gpu |= {"memory_total_mb": _num(first[2]), "memory_used_mb": _num(first[3])}
    status = PASS if "H100" in gpu["name"] and len(rows) == 1 else WARN
    rep.add("gpu", status, f"{gpu['name']} x{len(rows)} (기대: H100 80GB 하나)")

    major = _driver_major(gpu["driver"])
    gpu["driver_major"] = major
    _rc, head = run(list(smi))
    m = re.search(r"CUDA Version:\s*([0-9]+)\.([0-9]+)", head or "")
    gpu["cuda_driver_max"] = f"{m.group(1)}.{m.group(2)}" if m else ""
    if major is None:
        rep.add("driver", FAIL, f"드라이버 버전을 읽지 못함: {gpu['driver']!r}")
    elif major >= MIN_DRIVER:
        rep.add("driver", PASS, f"{gpu['driver']} (CUDA 13 요구 {MIN_DRIVER} 이상)")
    else:
        # 데이터센터 GPU 는 이미지의 순방향 호환 라이브러리로 돌 수 있다. 실제 판정은 b 항목(torch.cuda.is_available)
        rep.add("driver", WARN, f"{gpu['driver']} < {MIN_DRIVER}: 순방향 호환(cuda-compat)에 기댄다. b 항목에서 판정")
    if m and (int(m.group(1)), int(m.group(2))) >= MIN_CUDA:
        rep.add("cuda", PASS, f"드라이버 최대 CUDA {gpu['cuda_driver_max']} (요구 13.0)")
    elif m:
        rep.add("cuda", WARN, f"드라이버 최대 CUDA {gpu['cuda_driver_max']} < 13.0: Pod CUDA 필터를 13.0 이상으로")
    else:
        rep.add("cuda", WARN, "nvidia-smi 머리말에서 CUDA 버전을 찾지 못함")
    compat = Path("/usr/local/cuda/compat")
    gpu["cuda_compat_present"] = compat.is_dir()
    return gpu


def _driver_major(text: str) -> int | None:
    m = re.match(r"\s*(\d+)", text or "")
    return int(m.group(1)) if m else None


def _num(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


# ---------------------------------------------------------------- 볼륨


def mount_info(path: Path) -> dict:
    """path 를 담은 마운트(가장 긴 접두사)의 장치·파일시스템. /proc/mounts 가 없으면(Windows) 빈 값."""
    info = {"path": str(path), "is_mount": os.path.ismount(path), "device": "", "fstype": "", "mountpoint": ""}
    try:
        lines = Path("/proc/mounts").read_text(encoding="utf-8").splitlines()
    except OSError:
        return info
    target = str(path.resolve())
    best = ""
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        mp = parts[1].replace("\\040", " ")
        if (target == mp or target.startswith(mp.rstrip("/") + "/")) and len(mp) > len(best):
            best = mp
            info |= {"device": parts[0], "fstype": parts[2], "mountpoint": mp}
    return info


def check_volume(rep: Report, root: Path, allow_not_mount: bool, min_free_gb: float) -> dict:
    vol: dict = {"root": str(root)}
    if not root.is_dir():
        rep.add("volume_root", FAIL, f"{root} 가 없다 (Network Volume 을 /workspace 에 붙인 Pod 인가)")
        return vol
    mi = mount_info(root)
    vol["mount"] = mi
    if mi["is_mount"] or (mi["mountpoint"] and mi["mountpoint"] != "/"):
        rep.add("volume_mount", PASS, f"{mi['mountpoint'] or root} ({mi['fstype'] or '?'}, {mi['device'] or '?'})")
    elif allow_not_mount:
        rep.add("volume_mount", WARN, f"{root} 는 마운트 지점이 아니다(드라이런 허용)")
    else:
        rep.add("volume_mount", FAIL, f"{root} 가 마운트 지점이 아니다: 컨테이너 디스크에 쓰게 된다 (볼륨 미부착)")

    usage = shutil.disk_usage(root)
    vol |= {"total_gb": round(usage.total / 1e9, 2), "free_gb": round(usage.free / 1e9, 2)}
    status = PASS if usage.free / 1e9 >= min_free_gb else FAIL
    rep.add("volume_free", status, f"{vol['free_gb']} GB 남음 / {vol['total_gb']} GB (필요 {min_free_gb} GB 이상)")
    container = shutil.disk_usage("/" if os.name != "nt" else Path.cwd().anchor)
    vol["container_free_gb"] = round(container.free / 1e9, 2)
    rep.add("container_disk_free", PASS if container.free / 1e9 >= 10 else WARN,
            f"컨테이너 디스크 {vol['container_free_gb']} GB 남음 (이미지 풀면 약 19.5GB 를 뺀 값, 10GB 이상 권장)")

    simai = root / "simai"
    present = {d: (simai / d).is_dir() for d in VOLUME_DIRS}
    vol["dirs"] = present
    missing = [d for d, ok in present.items() if not ok]
    rep.add("volume_dirs", PASS if not missing else WARN,
            "모두 있음" if not missing else f"{simai} 아래 없음: {', '.join(missing)} (처음이면 만든다)")
    weights = simai / "weights" / "yolo" / "yolo26x-pose.pt"
    vol["tier0_weights"] = weights.is_file()
    rep.add("tier0_weights", PASS if weights.is_file() else WARN,
            f"{weights} {'있음' if weights.is_file() else '없음 (b 항목 엔진 빌드가 실패한다)'}")

    private = root / ".simai-private"
    entries = {}
    if private.is_dir():
        for p in sorted(private.iterdir()):
            if p.is_file():
                st = p.stat()
                entries[p.name] = {"bytes": st.st_size, "mode": oct(st.st_mode & 0o777)}
    vol["private"] = {"present": private.is_dir(), "files": entries}  # 이름·크기·권한만. 내용은 읽지 않는다
    hf = entries.get("hf.env")
    rep.add("private_hf_env", PASS if hf and hf["bytes"] > 0 else WARN,
            f"{private / 'hf.env'} {'있음' if hf else '없음'} (내용은 읽지 않음, 볼륨은 chmod 가 안 먹는다)")
    return vol


def check_io(rep: Report, root: Path, write_mb: int, fsync_count: int, fsync_warn_ms: float) -> dict:
    """쓰기 속도(write_mb MB + fsync), 작은 추가+fsync 지연(저널 기록과 같은 형태), rename, flock."""
    io: dict = {}
    probe = root / "simai" / "state" / f".pod-check-{socket.gethostname()}-{os.getpid()}"
    try:
        probe.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        rep.add("volume_write", FAIL, f"{probe} 를 만들지 못함: {e}")
        return io
    try:
        big = probe / "write.bin"
        chunk = os.urandom(1 << 20)
        t0 = time.perf_counter()
        with open(big, "wb") as f:
            for _ in range(write_mb):
                f.write(chunk)
            f.flush()
            t1 = time.perf_counter()
            os.fsync(f.fileno())
        t2 = time.perf_counter()
        io |= {"write_mb": write_mb, "write_s": round(t2 - t0, 3), "write_fsync_s": round(t2 - t1, 3),
               "write_mb_s": round(write_mb / max(t2 - t0, 1e-9), 1)}
        t3 = time.perf_counter()
        with open(big, "rb") as f:
            while f.read(1 << 22):
                pass
        io["read_mb_s_cached"] = round(write_mb / max(time.perf_counter() - t3, 1e-9), 1)
        rep.add("volume_write", PASS, f"{write_mb} MB 쓰기+fsync {io['write_s']} s ({io['write_mb_s']} MB/s)")

        lat = []
        with open(probe / "journal-like.log", "ab") as f:
            for i in range(fsync_count):
                f.write(json.dumps({"i": i, "pad": "x" * 400}).encode() + b"\n")
                f.flush()
                s = time.perf_counter()
                os.fsync(f.fileno())
                lat.append((time.perf_counter() - s) * 1000)
        lat.sort()
        io["fsync_ms"] = {"n": len(lat), "p50": round(statistics.median(lat), 2),
                          "p95": round(lat[int(0.95 * (len(lat) - 1))], 2), "p99": round(lat[int(0.99 * (len(lat) - 1))], 2),
                          "max": round(lat[-1], 2)}
        status = PASS if io["fsync_ms"]["p99"] <= fsync_warn_ms else WARN
        rep.add("fsync", status, f"작은 추가+fsync {fsync_count}회 p50 {io['fsync_ms']['p50']} ms, p99 {io['fsync_ms']['p99']} ms"
                f" (WARN 기준 p99 > {fsync_warn_ms} ms: 저널 쓰기 정체·체크포인트 지연 위험)")

        a, b = probe / "a.tmp", probe / "b.json"
        a.write_text("1", encoding="utf-8")
        b.write_text("0", encoding="utf-8")
        os.replace(a, b)
        ok = b.read_text(encoding="utf-8") == "1" and not a.exists()
        io["atomic_replace"] = ok
        rep.add("atomic_replace", PASS if ok else FAIL, "os.replace 로 덮어쓰기 (체크포인트·잠금 파일)")
        io["flock"] = _flock_probe(probe / "lock")
        rep.add("flock", PASS if io["flock"] in ("ok", "n/a") else FAIL,
                {"ok": "배타 잠금 얻고 두 번째 시도는 거부", "n/a": "fcntl 없음(Windows 드라이런)"}.get(io["flock"], io["flock"]))
    except OSError as e:
        rep.add("volume_io", FAIL, f"쓰기 시험 중 오류: {type(e).__name__}: {e}")
    finally:
        shutil.rmtree(probe, ignore_errors=True)
    return io


def _flock_probe(path: Path) -> str:
    """저널 단일 작성자 잠금(eventlog._lock)과 같은 flock. 같은 파일을 다른 fd 로 다시 잠그면 거부돼야 한다."""
    try:
        import fcntl
    except ImportError:
        return "n/a"
    fd1 = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    fd2 = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd1, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return "ok"
        return "두 번째 잠금이 거부되지 않음 (볼륨이 flock 을 무시한다)"
    except OSError as e:
        return f"잠금 실패: {e}"
    finally:
        os.close(fd2)
        os.close(fd1)


def check_env(rep: Report) -> dict:
    keys = ("RUNPOD_POD_ID", "RUNPOD_DC_ID", "RUNPOD_PUBLIC_IP", "RUNPOD_TCP_PORT_22", "SIMAI_IMAGE_DIGEST", "SIMAI_ROOT")
    env = {k: os.environ.get(k, "") for k in keys}
    dc = env["RUNPOD_DC_ID"]
    if dc:
        rep.add("datacenter", PASS if dc.upper() == "AP-JP-1" else FAIL, f"{dc} (기대 AP-JP-1, 볼륨과 같은 데이터센터)")
    else:
        rep.add("datacenter", SKIP, "RUNPOD_DC_ID 없음 (RunPod 밖)")
    rep.add("ssh_port", PASS if env["RUNPOD_TCP_PORT_22"] else SKIP,
            f"공개 IP {env['RUNPOD_PUBLIC_IP'] or '?'} 포트 {env['RUNPOD_TCP_PORT_22'] or '?'} (Pod 재시작마다 바뀔 수 있다)")
    return env


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):  # pytest 캡처 스트림에는 없다
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="RunPod Pod 점검 (S15P31A101-31)")
    ap.add_argument("--root", type=Path, default=Path("/workspace"), help="Network Volume 마운트 경로 (기본 /workspace)")
    ap.add_argument("--out", type=Path, default=None, help="결과 JSON (기본 $SIMAI_RUN_DIR/pod_check.json, 없으면 표준출력만)")
    ap.add_argument("--nvidia-smi", default=os.environ.get("SIMAI_NVIDIA_SMI", DEFAULT_SMI),
                    help="nvidia-smi 명령 (드라이런: 'python fake_nvidia_smi.py'). 환경 변수 SIMAI_NVIDIA_SMI")
    ap.add_argument("--allow-not-mount", action="store_true", help="root 가 마운트 지점이 아니어도 실패로 보지 않는다(드라이런)")
    ap.add_argument("--min-free-gb", type=float, default=50.0, help="볼륨 남은 용량 하한 (기본 50GB: 엔진·녹화·실험 산출물)")
    ap.add_argument("--write-mb", type=int, default=1024, help="쓰기 속도 시험 크기 MB (기본 1024)")
    ap.add_argument("--fsync-count", type=int, default=200, help="작은 추가+fsync 횟수 (기본 200)")
    ap.add_argument("--fsync-warn-ms", type=float, default=50.0, help="fsync p99 경고 기준 ms (기본 50)")
    args = ap.parse_args(argv)

    out = args.out or (Path(os.environ["SIMAI_RUN_DIR"]) / "pod_check.json" if os.environ.get("SIMAI_RUN_DIR") else None)
    rep = Report()
    started = time.time_ns() // 1_000_000
    smi = smi_command(args.nvidia_smi)
    result = {"check": "pod_check", "jira": "S15P31A101-31", "started_at": started, "host": socket.gethostname()}

    def guarded(name: str, fn, *a) -> dict:
        try:
            return fn(rep, *a)
        except Exception as e:  # noqa: BLE001 - 점검 하나가 깨져도 나머지와 결과 파일은 남긴다
            rep.add(name, FAIL, f"점검 오류 {type(e).__name__}: {e}")
            return {}

    result["env"] = guarded("env", check_env)
    result["gpu"] = guarded("gpu", check_gpu, smi)
    result["volume"] = guarded("volume", check_volume, args.root, args.allow_not_mount, args.min_free_gb)
    result["io"] = guarded("volume_io", check_io, args.root, args.write_mb, args.fsync_count, args.fsync_warn_ms)         if args.root.is_dir() else {}
    result |= {"finished_at": time.time_ns() // 1_000_000, "items": rep.items, "verdict": rep.worst()}
    print(f"\npod_check: {result['verdict']}", flush=True)

    if out is not None:
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError as e:
            print(f"pod_check: 결과를 쓰지 못함 {out}: {e}", file=sys.stderr)
            return 2
        run_dir = os.environ.get("SIMAI_RUN_DIR")
        if run_dir:  # GPU 큐 Run 기록의 metrics(스칼라만)
            io = result["io"]
            metrics = {"verdict": result["verdict"], "gpu_name": result["gpu"].get("name", ""),
                       "driver": result["gpu"].get("driver", ""), "volume_free_gb": result["volume"].get("free_gb"),
                       "write_mb_s": io.get("write_mb_s"), "fsync_p50_ms": (io.get("fsync_ms") or {}).get("p50"),
                       "fsync_p99_ms": (io.get("fsync_ms") or {}).get("p99")}
            try:
                (Path(run_dir) / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False) + "\n", encoding="utf-8")
            except OSError as e:
                print(f"pod_check: metrics.json 을 쓰지 못함: {e}", file=sys.stderr)
    return 1 if result["verdict"] == FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
