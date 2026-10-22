#!/usr/bin/env python3
"""GPU 1차 세션 큐 c 항목: Tier 0 LIVE 입력 Pod 확인 (S15P31A101-182 'Pod 확인' 1~5). -182 머지 뒤 경로 기준이다.

    python deploy/scripts/session1_live.py all          # c1~c5 (큐가 부른다, 결과는 $SIMAI_RUN_DIR)
    python deploy/scripts/session1_live.py serve --seconds 600 --work <폴더>   # LIVE 하네스만 (d 항목·수동 측정)

단계(기대 결과는 -182 MR 'Pod 확인'). Tier 0 기본 디코더는 CPU 이고 NVDEC 은 선택이다(-236):
  c0 cpu_env           (기록만) 컨테이너가 받는 vCPU(cgroup cpu.max·sched_getaffinity)·호스트 CPU 수·CPU 모델. CPU 디코딩 여유의 근거
  c1 nvdec_open        (선택) NvdecDecoder() 가 열린다 ("nvdec ok"). 실패면 이미지의 NVIDIA_DRIVER_CAPABILITIES 에 video 가 있는지 본다
  c2 nvdec_gpu_test    (선택) pytest -m gpu tests/io/test_pyav_decoder.py → 통과 1 이상, 건너뜀 0
  c3 decode_throughput 2304x1296 H.264 4스트림을 CPU(PyAvDecoder, 기본 디코더)로 카메라마다 스레드 하나(LIVE 구조)에서 디코딩 + BGR:
                       그림 한 장 p95 ≤ 10 ms, 4대 합계 ≥ 100 fps, 넣은 수 = 나온 수. 참고 측정(판정 안 함, 실패는 <이름>_error):
                       CPU 한 스레드 번갈아(cpu_), NVDEC 한 스레드 번갈아(nvdec_)·카메라별 스레드(nvdec_par_)·변환 스레드 0·1·nv12 그대로
  c4 live_start        LIVE 하네스: 진입점(엔진 준비 → Tier 0 LIVE, 기본 디코더 cpu) + 노트북 흉내(제어 채널·시계 교환·하트비트)
                       + 영상 송신 4대(2304x1296, 25 fps, 프레임 번호 무늬 H.264, 사람 없음). 종료 코드 0, 카메라 4대 ready,
                       decode_failed 0, detector_errors 0, mismatched 0. frame_age_ms_p95·pipeline_lag_ms 를 기록한다
  c5 ten_people        (a) 기하 단계 10명 p95 ≤ 50 ms (tests/geometry/bench_tier0_geometry.py, 명세 6.6)
                       (b) 10명 녹화 LIVE 재생은 녹화·노트북 relay 가 있어야 해서 큐에서는 SKIP(문서의 수동 절차)

LIVE 하네스는 실제 노트북·relay·EC2 없이 Pod 안에서 돈다(127.0.0.1). 송신 영상에 사람이 없으므로 검출·추적 정확도가 아니라
입력 경로(수신·디코딩·검출 호출·대기열)의 기동과 처리 시간만 본다. 결과는 제품 성능 지표가 아니다(AGENTS 규칙 7).
TLS 인증서·개인 키와 시험 토큰은 임시 폴더에 만들고 끝나면 지운다. 종료 코드: 0 필수 단계 모두 통과, 1 실패 있음.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
AI = REPO / "ai"
for p in (AI / "tests" / "services" / "tier0", AI):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

SIZE = (2304, 1296)
FPS = 25
LAG_MS = 30  # 촬영 → 노트북 송신 지연 흉내


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S', time.gmtime())}Z] {msg}", flush=True)


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return round(s[min(len(s) - 1, max(0, int(round(q / 100 * (len(s) - 1)))))], 3)


class Steps:
    """단계 기록. 필수 단계가 실패하거나 돌지 못하면(SKIP 포함) 실패로 센다. 선택 단계 SKIP 은 이유만 남긴다."""

    def __init__(self, out: Path) -> None:
        self.out = out
        self.rows: list[dict] = []
        self.metrics: dict = {}

    def run(self, name: str, required: bool, fn) -> dict:
        log(f"=== {name}")
        t0 = time.monotonic()
        try:
            status, detail, values = fn()
        except Exception as e:  # noqa: BLE001 - 단계 하나가 깨져도 다음 단계로
            status, detail, values = "FAIL", f"{type(e).__name__}: {e}", {}
        row = {"step": name, "required": required, "status": status, "detail": detail,
               "seconds": round(time.monotonic() - t0, 2), **({"values": values} if values else {})}
        self.rows.append(row)
        self.metrics[f"{name}_status"] = status
        for k, v in (values or {}).items():
            if isinstance(v, (int, float, str, bool)) or v is None:
                self.metrics[f"{name}_{k}"] = v
        log(f"=== {name} → {status}: {detail}")
        return row

    def failed(self) -> int:
        return sum(1 for r in self.rows if r["required"] and r["status"] != "PASS")

    def write(self, extra: dict | None = None) -> None:
        self.metrics["failed_required"] = self.failed()
        (self.out / "steps.json").write_text(json.dumps({"steps": self.rows, **(extra or {})}, ensure_ascii=False, indent=2)
                                             + "\n", encoding="utf-8")
        (self.out / "metrics.json").write_text(json.dumps(self.metrics, ensure_ascii=False) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- c0·c1·c2


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


def cgroup_vcpu(read=None) -> tuple[float | None, str | None]:
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


def c0_cpu_env(read=None):
    """컨테이너가 실제로 받는 CPU. os.cpu_count() 는 호스트 값(H100 Pod 224)이라 CPU 디코딩 여유 판단에 쓰지 않는다.

    usable_vcpu 는 cgroup 할당량·affinity·호스트 수 중 읽은 값의 최솟값이고, 어느 것에서 왔는지 usable_vcpu_source 에 남긴다.
    cgroup 을 읽지 못하면 cgroup_vcpu 는 null 이고 출처가 affinity·host 로 남는다(할당량을 지어내지 않는다).
    """
    read = read or _read
    affinity = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None
    model = next((ln.split(":", 1)[1].strip() for ln in (read("/proc/cpuinfo") or "").splitlines()
                  if ln.startswith("model name")), None)
    quota, quota_src = cgroup_vcpu(read)
    host = os.cpu_count()
    values = {"host_cpu_count": host, "affinity_cpus": affinity, "cgroup_vcpu": quota, "cgroup_vcpu_file": quota_src,
              "cpu_model": model}
    candidates = [(v, name) for v, name in ((quota, "cgroup"), (affinity, "affinity"), (host, "host")) if v is not None]
    usable, source = min(candidates, key=lambda x: x[0]) if candidates else (None, None)
    values |= {"usable_vcpu": usable, "usable_vcpu_source": source}
    return "PASS", (f"쓸 수 있는 vCPU {usable} (출처 {source}; cgroup {quota} {quota_src or '못 읽음·제한 없음'}, "
                    f"affinity {affinity}, 호스트 {host}), 모델 {model}"), values


def c1_nvdec_open(py: str):
    r = subprocess.run([py, "-c", "from simai_ai.io import NvdecDecoder; NvdecDecoder(); print('nvdec ok')"],
                       cwd=AI, capture_output=True, text=True, timeout=120)
    out = (r.stdout + r.stderr).strip()
    print(out, flush=True)
    ok = r.returncode == 0 and "nvdec ok" in out
    return ("PASS" if ok else "FAIL"), out.splitlines()[-1][:300] if out else f"exit {r.returncode}", {}


def c2_nvdec_gpu_test(py: str, out: Path):
    xml = out / "junit-nvdec.xml"
    r = subprocess.run([py, "-m", "pytest", "-q", "-m", "gpu", "tests/io/test_pyav_decoder.py", "-p", "no:cacheprovider",
                        f"--junitxml={xml}"], cwd=AI, timeout=600,
                       env={**os.environ, "SIMAI_TEST_GPU": "1"})  # 없으면 NVDEC 시험이 건너뛰어진다(test_pyav_decoder)
    try:
        suite = ET.parse(xml).getroot()
        suite = suite if suite.tag == "testsuite" else suite.find("testsuite")
        tests, skipped = int(suite.get("tests", 0)), int(suite.get("skipped", 0))
        failed = int(suite.get("failures", 0)) + int(suite.get("errors", 0))
    except (OSError, ET.ParseError, AttributeError):
        return "FAIL", f"junit XML 없음 (exit {r.returncode})", {}
    passed = tests - skipped - failed
    ok = r.returncode == 0 and passed >= 1 and skipped == 0
    return ("PASS" if ok else "FAIL"), f"통과 {passed}, 건너뜀 {skipped}, 실패 {failed} (건너뜀 0 이어야)", \
        {"passed": passed, "skipped": skipped, "failed": failed}


# ---------------------------------------------------------------- c3


def encode_stream(frames: int, size=SIZE, gop: int = 50):
    import live_video as lv  # -182 tests/services/tier0/live_video.py (프레임 번호 무늬 H.264, SPS·PPS 는 codec_info 로만)

    return lv.encode(list(range(frames)), gop=gop, size=size, fps=FPS)


def bench_decoders(make, enc, cams: int = 4, parallel: bool = False) -> dict:
    """카메라 cams 대 디코더에 같은 스트림을 넣는다. 그림 한 장(디코딩+BGR) 시간과 합계 fps.

    parallel=False: 한 스레드에서 번갈아(보수적, 참고 측정). True: 카메라마다 스레드 하나(LIVE 의 카메라별 수신·디코딩 스레드와
    같은 구조, c3 판정은 CPU 디코더의 이 측정이다, -236). 한 스레드라도 실패하면 예외를 그대로 올린다(덜 나온 그림을 통과로 세지 않는다).
    """
    from simai_ai.io import I0CodecInfo

    decs = []
    for i in range(cams):
        d = make()
        d.configure(I0CodecInfo(camera_id=f"cam{i + 1}", stream_epoch=1, codec="H264", pts_timebase="1/90000",
                                sps=enc.sps, pps=enc.pps))
        decs.append(d)
    per: list[float] = []
    counts = [0] * cams
    errors: list[BaseException] = []

    def one(i: int, d) -> None:
        try:
            for seq, (au, key) in enumerate(enc.frames):
                s = time.perf_counter()
                pics = d.decode(au, (1, seq), key)
                per.append((time.perf_counter() - s) * 1000)  # list.append 는 GIL 아래 원자적이다
                counts[i] += len(pics)
            counts[i] += len(d.flush())
        except BaseException as e:  # noqa: BLE001 - 주 스레드에서 다시 올린다
            errors.append(e)

    t0 = time.perf_counter()
    if parallel:
        threads = [threading.Thread(target=one, args=(i, d), daemon=True) for i, d in enumerate(decs)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if errors:
            raise errors[0]
    else:
        for seq, (au, key) in enumerate(enc.frames):
            for i, d in enumerate(decs):
                s = time.perf_counter()
                pics = d.decode(au, (1, seq), key)
                per.append((time.perf_counter() - s) * 1000)
                counts[i] += len(pics)
        for i, d in enumerate(decs):
            counts[i] += len(d.flush())
    pictures = sum(counts)
    total = time.perf_counter() - t0
    return {"frames_in": len(enc.frames) * cams, "pictures_out": pictures, "per_frame_ms_p50": pct(per, 50),
            "per_frame_ms_p95": pct(per, 95), "total_fps": round(pictures / total, 1), "seconds": round(total, 2)}


C3_P95_MS = 10.0  # 카메라 하나의 그림 한 장(디코딩+BGR). 프레임 간격 40 ms 의 1/4 (docs/design/tier0-nvdec-throughput.md)
C3_TOTAL_FPS = 100.0  # 4대 × 25 fps


def c3_judge(r: dict) -> bool:
    """판정: CPU 카메라별 스레드(LIVE 구조). 잰 값이 없거나 그림을 덜 내면 통과로 보지 않는다."""
    return (r.get("per_frame_ms_p95") is not None and r["per_frame_ms_p95"] <= C3_P95_MS
            and r.get("total_fps") is not None and r["total_fps"] >= C3_TOTAL_FPS
            and r.get("frames_in", 0) > 0 and r.get("pictures_out") == r["frames_in"])


def c3_decode_throughput(seconds: float = 10.0, variant_seconds: float = 5.0, size=SIZE):
    from simai_ai.io import NvdecDecoder, PyAvDecoder

    enc = encode_stream(int(seconds * FPS), size=size)
    short = dataclasses.replace(enc, frames=enc.frames[:int(variant_seconds * FPS)])  # 변형은 앞 5초(시간 상한 유지)
    cpu = bench_decoders(PyAvDecoder, enc, parallel=True)  # 판정: 기본 디코더 = Tier 0 LIVE 와 같은 구조
    values: dict = {f"cpu_par_{k}": v for k, v in cpu.items()}
    references = {  # 참고만(판정 안 함): 이전 기준(한 스레드 번갈아)·NVDEC 선택 경로·변환 스레드 (docs/design/tier0-nvdec-throughput.md)
        "cpu": lambda: bench_decoders(PyAvDecoder, enc),
        "nvdec": lambda: bench_decoders(NvdecDecoder, enc),
        "nvdec_par": lambda: bench_decoders(NvdecDecoder, short, parallel=True),
        "nvdec_thr0": lambda: bench_decoders(lambda: NvdecDecoder(convert_threads=0), short),
        "nvdec_thr1": lambda: bench_decoders(lambda: NvdecDecoder(convert_threads=1), short),
        "nvdec_nv12": lambda: bench_decoders(lambda: NvdecDecoder(output_format="nv12"), short),
    }
    for name, run in references.items():
        try:
            values |= {f"{name}_{k}": v for k, v in run().items()}
        except Exception as e:  # noqa: BLE001 - 참고 측정이다(NVDEC 이 없는 GPU 여도 판정은 그대로)
            values[f"{name}_error"] = f"{type(e).__name__}: {e}"
    ok = c3_judge(cpu)
    return ("PASS" if ok else "FAIL"), (f"CPU 카메라별 스레드 그림 p95 {cpu['per_frame_ms_p95']} ms, 합계 {cpu['total_fps']} fps, "
                                        f"{cpu['pictures_out']}/{cpu['frames_in']} 장 (기준 ≤ {C3_P95_MS:g} ms, ≥ {C3_TOTAL_FPS:g} fps). "
                                        f"참고 NVDEC 합계 {values.get('nvdec_total_fps', values.get('nvdec_error'))}"), values


# ---------------------------------------------------------------- c4: LIVE 하네스


class LiveHarness:
    """Pod 안 LIVE Tier 0 하네스. 진입점(엔진 준비) → Tier 0 LIVE, 노트북 흉내, 영상 송신 4대."""

    def __init__(self, work: Path, *, seconds: float, decoder: str | None = None, size=SIZE,
                 extra_env: dict | None = None) -> None:
        import live_video as lv
        import tier0_site as ts

        self.lv, self.ts = lv, ts
        self.work = work
        self.seconds = seconds
        self.size = size
        work.mkdir(parents=True, exist_ok=True)
        self.secrets = Path(tempfile.mkdtemp(prefix="simai-live-tls-"))  # 볼륨 밖
        from test_i1_tls import make_cert

        cert, key = make_cert(self.secrets)
        run = ts.run_sim(work, ts.scenario(8_000))  # 교실 보정(카메라 4대 배치)만 쓴다
        from simai_ai.io import SimObservationFrameSource

        self.cams = tuple(SimObservationFrameSource(run).camera_ids)
        calibration = json.loads((run / "calibration.json").read_text(encoding="utf-8"))
        ts.archive_bundle(work / "boot", calibration, "cfg-1", now_ms() - 1000)
        self.state = work / "state"
        self.marker = self.state / "tier0.live"
        doc = {"gateway_id": ts.GATEWAY, "state_dir": str(self.state), "scene_version": "scene-sim",
               "calibration_file": str(run / "calibration.json"),
               "input": {"mode": "live", "camera_ids": list(self.cams),
                         **({} if decoder is None else {"decoder": decoder})},  # None: Tier 0 기본 디코더(cpu)
               "bootstrap": {"archive": str(work / "boot"), "config_version": "cfg-1"}, "tick_ms": 100}
        self.cfg = work / "tier0-live.json"
        self.cfg.write_text(json.dumps(doc), encoding="utf-8")
        control = ts.free_port()
        self.video_port = ts.free_port()
        env = ts.tier0_env(cert=cert, key=key, control_port=control, ec2_url=None)
        env |= {"SIMAI_TIER0_VIDEO_PORTS": ",".join(f"{c}:{71001 + i}" for i, c in enumerate(self.cams)),
                "SIMAI_TIER0_VIDEO_LISTEN_HOST": "127.0.0.1"}
        env |= {f"RUNPOD_TCP_PORT_{71001 + i}": str(self.video_port) for i in range(len(self.cams))}
        env.pop("RUNPOD_PUBLIC_IP", None)  # EC2 가 없으니 엔드포인트 등록도 없다
        self.env = {**os.environ, "PYTHONUTF8": "1", **env, **(extra_env or {})}
        self.laptop = ts.Laptop(control, cert)
        self.stop = threading.Event()
        self.proc: subprocess.Popen | None = None
        self.threads: list[threading.Thread] = []
        self.samples: list[dict] = []
        self.sent = {c: 0 for c in self.cams}
        self.send_errors: list[str] = []
        self.log_path = work / "tier0-live.log"

    def command(self) -> list[str]:
        entry = shutil.which("simai-tier0")  # 이미지 진입점: 엔진 준비(engine_cache) → python -m services.tier0
        tail = ["--config", str(self.cfg), "--run-seconds", str(self.seconds)]
        return [entry, *tail] if entry else [sys.executable, "-m", "services.tier0", *tail]

    def start(self, enc) -> None:
        log(f"LIVE 하네스: {' '.join(self.command())}")
        logf = open(self.log_path, "ab")  # noqa: SIM115 - 프로세스 수명 동안
        self.proc = subprocess.Popen(self.command(), cwd=AI, env=self.env, stdout=logf, stderr=subprocess.STDOUT)
        self.laptop.start()
        for cam in self.cams:
            t = threading.Thread(target=self._send, args=(cam, enc), daemon=True, name=f"send-{cam}")
            t.start()
            self.threads.append(t)

    def _send(self, cam: str, enc) -> None:
        lv = self.lv
        sender = None
        while not self.stop.is_set() and sender is None:  # 엔진 준비 동안은 영상 포트가 아직 닫혀 있다
            try:
                sender = lv.VideoSender(self.video_port, cam, self.ts.LAPTOP_TOKEN)
            except AssertionError as e:  # HELLO 거절(토큰 등): 다시 붙어도 같다
                self.send_errors.append(f"{cam}: HELLO 거절 {e}")
                return
            except OSError:
                if self.proc is not None and self.proc.poll() is not None:
                    return
                self.stop.wait(1.0)
        if sender is None:
            return
        try:
            epoch = now_ms()
            sender.codec(lv.codec_meta(cam, epoch, enc))
            seq = 0
            t0 = time.monotonic()
            while not self.stop.is_set():
                for au, key in enc.frames:
                    due = t0 + seq / FPS
                    delay = due - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)
                    if self.stop.is_set():
                        return
                    sender.frame(lv.frame_meta(cam, epoch, seq, now_ms() - LAG_MS, key), au)
                    seq += 1
                    self.sent[cam] = seq
        except OSError as e:
            if not self.stop.is_set():
                self.send_errors.append(f"{cam}: {type(e).__name__}: {e}")
        finally:
            sender.close()

    def sample(self) -> dict | None:
        try:
            doc = json.loads(self.marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        live = (doc.get("status") or {}).get("live_input")
        if live:
            self.samples.append({"t": now_ms(), "live_input": live, "gpu_priority": doc.get("gpu_priority")})
        return live

    def wait_ready(self, timeout_s: float) -> bool:
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            live = self.sample()
            if live and all(c.get("ready") for c in (live.get("cameras") or {}).values()) and live.get("cameras"):
                return True
            if self.proc is not None and self.proc.poll() is not None:
                return False
            time.sleep(1.0)
        return False

    def finish(self, timeout_s: float) -> int | None:
        assert self.proc is not None
        end = time.monotonic() + timeout_s
        while self.proc.poll() is None and time.monotonic() < end:
            self.sample()
            time.sleep(1.0)
        self.stop.set()
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(30)
            return None
        return self.proc.returncode

    def close(self) -> None:
        self.stop.set()
        self.laptop.close()
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(30)
        for t in self.threads:
            t.join(5)
        shutil.rmtree(self.secrets, ignore_errors=True)

    def lag_ms(self) -> list[float]:
        return [h["payload"]["pipeline_lag_ms"] for h in self.laptop.all_messages("health")
                if isinstance(h["payload"].get("pipeline_lag_ms"), (int, float))]


def judge_live(h: LiveHarness, code: int | None, ready: bool) -> tuple[str, str, dict]:
    last = next((s["live_input"] for s in reversed(h.samples)), {}) or {}
    cams = last.get("cameras") or {}
    det = last.get("detector") or {}
    decode_failed = sum(int(c.get("decode_failed") or 0) for c in cams.values())
    ages = [c.get("frame_age_ms_p95") for s in h.samples for c in (s["live_input"].get("cameras") or {}).values()
            if isinstance(c.get("frame_age_ms_p95"), (int, float))]
    lags = h.lag_ms()
    values = {"exit_code": code, "cameras_ready": sum(1 for c in cams.values() if c.get("ready")),
              "decode_failed": decode_failed, "detector_errors": det.get("detector_errors"),
              "mismatched": det.get("mismatched"), "inferred": sum(int(c.get("inferred") or 0) for c in cams.values()),
              "frame_age_ms_p95_max": max(ages) if ages else None,
              "pipeline_lag_ms_p50": pct(lags, 50), "pipeline_lag_ms_p95": pct(lags, 95),
              "frames_sent": sum(h.sent.values()), "send_errors": len(h.send_errors)}
    ok = (code == 0 and ready and values["cameras_ready"] == len(h.cams) and decode_failed == 0
          and det.get("detector_errors") == 0 and det.get("mismatched") == 0 and values["inferred"] > 0)
    detail = (f"종료 {code}, ready {values['cameras_ready']}/{len(h.cams)}, decode_failed {decode_failed}, "
              f"detector_errors {values['detector_errors']}, mismatched {values['mismatched']}, "
              f"frame_age p95 최대 {values['frame_age_ms_p95_max']} ms, pipeline_lag p95 {values['pipeline_lag_ms_p95']} ms")
    return ("PASS" if ok else "FAIL"), detail, values


def c4_live_start(work: Path, seconds: float, ready_timeout_s: float):
    enc = encode_stream(250)  # 10 s 를 되풀이해 보낸다(번호·촬영 시각은 계속 늘어난다)
    h = LiveHarness(work, seconds=seconds)
    try:
        h.start(enc)
        ready = h.wait_ready(ready_timeout_s)  # 엔진 첫 빌드가 남아 있으면 오래 걸린다(b 항목 뒤라 보통 캐시)
        code = h.finish(seconds + ready_timeout_s + 60)
        (work / "live-samples.json").write_text(json.dumps(h.samples[-60:], ensure_ascii=False) + "\n", encoding="utf-8")
        return judge_live(h, code, ready)
    finally:
        h.close()


# ---------------------------------------------------------------- c5


def c5_geometry(py: str, out: Path):
    path = out / "geometry.json"
    r = subprocess.run([py, "tests/geometry/bench_tier0_geometry.py", "--people", "1", "5", "10", "--ticks", "100",
                        "--json", str(path)], cwd=AI, timeout=1200)
    doc = json.loads(path.read_text(encoding="utf-8"))
    ten = next(x for x in doc["results"] if x["people"] == 10)
    p95 = ten["stages_ms"]["pipeline"]["p95"]
    ok = r.returncode == 0 and p95 <= 50
    return ("PASS" if ok else "FAIL"), f"10명 기하 pipeline p95 {p95:.1f} ms (기준 ≤ 50 ms, Pod CPU)", \
        {"pipeline_p95_ms_10p": round(p95, 2)}


def c5_recording():
    rec = os.environ.get("SIMAI_SESSION1_RECORDING")
    if not rec:
        return "SKIP", ("10명 녹화 LIVE 재생은 녹화(A0)와 노트북 relay 가 필요해 큐 밖에서 한다 "
                        "(docs/design/runpod-environment.md 'c5 수동')"), {}
    return "SKIP", f"녹화 {rec}: 재생 송신은 아직 자동화하지 않았다(수동 절차)", {}


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Tier 0 LIVE 입력 Pod 확인 (S15P31A101-182 → -183 c 항목)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_all = sub.add_parser("all")
    p_all.add_argument("--live-seconds", type=float, default=120)
    p_all.add_argument("--ready-timeout-s", type=float, default=600)
    p_serve = sub.add_parser("serve", help="LIVE 하네스만 --seconds 동안 (d 항목·수동 측정)")
    p_serve.add_argument("--seconds", type=float, required=True)
    p_serve.add_argument("--work", type=Path, required=True)
    p_serve.add_argument("--ready-timeout-s", type=float, default=600)
    args = ap.parse_args(argv)
    out = Path(os.environ.get("SIMAI_RUN_DIR") or "session1-live").resolve()
    out.mkdir(parents=True, exist_ok=True)
    py = sys.executable

    if args.cmd == "serve":
        h = LiveHarness(args.work, seconds=args.seconds)
        try:
            h.start(encode_stream(250))
            ready = h.wait_ready(args.ready_timeout_s)
            print(json.dumps({"ready": ready, "marker": str(h.marker)}), flush=True)
            code = h.finish(args.seconds + args.ready_timeout_s + 60)
            status, detail, values = judge_live(h, code, ready)
            print(json.dumps({"status": status, "detail": detail, **values}, ensure_ascii=False), flush=True)
            return 0 if status == "PASS" else 1
        finally:
            h.close()

    steps = Steps(out)
    steps.run("c0_cpu_env", False, c0_cpu_env)
    steps.run("c1_nvdec_open", False, lambda: c1_nvdec_open(py))  # NVDEC 은 선택 디코더(-236): 결과만 남긴다
    steps.run("c2_nvdec_gpu_test", False, lambda: c2_nvdec_gpu_test(py, out))
    steps.run("c3_decode_throughput", True, c3_decode_throughput)
    steps.run("c4_live_start", True, lambda: c4_live_start(out / "live", args.live_seconds, args.ready_timeout_s))
    steps.run("c5a_geometry_10p", True, lambda: c5_geometry(py, out))
    steps.run("c5b_live_10p_recording", False, c5_recording)
    steps.write()
    log(f"session1_live: 필수 단계 실패 {steps.failed()}")
    return 1 if steps.failed() else 0


if __name__ == "__main__":
    sys.exit(main())
