#!/usr/bin/env python3
"""막힌 망 I0 예비 경로 측정 (S15P31A101-288, 4차 GPU 세션 ④, 명세 3.12·19.1 "I0 프록시 지연").

같은 입력(녹화 재생 relay)으로 (a) RunPod HTTPS 프록시(443) + wstunnel TCP 전달, (b) LTE 테더링 Direct TCP 를 각각 10분 잰다.

    # Pod(SSH 로그인 셸): Tier 0 LIVE 를 띄우고 표시 파일(state/tier0.live)을 기록한다. endpoint.json·pod.jsonl·tier0.log 를 남긴다
    SIMAI_LAPTOP_TOKEN_SHA256=sha256:<hex 64> SIMAI_WEIGHTS_SET=tier0 simai-bootstrap run /opt/simai/ai/.venv/bin/python \\
        deploy/scripts/i0_link_measure.py pod --link tunnel --seconds 600 --work /srv/simai/runs/i0-link/a-tunnel
    # 노트북(gateway/relay 의 uv 환경): 녹화 재생 relay + 제어 채널(시계 교환·하트비트)을 endpoint.json 대상으로 붙이고 기록한다
    uv run --project gateway/relay python deploy/scripts/i0_link_measure.py laptop --endpoint <endpoint.json> \\
        --pin sha256:<hex 64> --config <relay TOML> --seconds 600 --out <laptop.jsonl>
    # 어느 쪽이든(표준 라이브러리만): 두 경로를 한 JSON·표로
    python deploy/scripts/i0_link_measure.py summarize --run tunnel=<pod.jsonl>,<laptop.jsonl> --run lte=<pod.jsonl>,<laptop.jsonl> \\
        --out summary.json
    # 노트북: 환경 변수 SIMAI_DEVICE_TOKEN 의 sha256 (Pod 의 SIMAI_LAPTOP_TOKEN_SHA256 에 넣을 값. 토큰은 출력하지 않는다)
    python deploy/scripts/i0_link_measure.py token-hash

pod --link:
  tunnel  Tier 0 가 127.0.0.1:<--control-port>(제어 wss)·127.0.0.1:<--video-port>(영상 TLS, 4대 공용)에서만 듣는다.
          대칭 매핑 값은 SIMAI_PUBLIC_HOST=127.0.0.1·SIMAI_PUBLIC_PORT_70000/70001 로 넣는다(제품 코드 그대로, 등록하면 같은 값이 간다).
          노트북 wstunnel 이 같은 번호의 로컬 포트를 Pod 의 그 포트로 전달한다(-L tcp://127.0.0.1:P:127.0.0.1:P). Pod 이미지의
          SIMAI_TUNNEL_ALLOW 에 두 포트가 있어야 한다(deploy/README "막힌 망 예비 SSH" 의 I0 터널).
  direct  RunPod 대칭 매핑(RUNPOD_PUBLIC_IP·RUNPOD_TCP_PORT_70000/70001, SIMAI_PUBLIC_* 가 있으면 그것)에서 0.0.0.0 으로 듣는다.

EC2 엔드포인트 조회(GET /api/endpoints/tier0)는 서버에 아직 없어서, 노트북은 Pod 가 쓴 endpoint.json(공개 값만: 주소·포트·
공개키 지문)을 고정 대상으로 쓴다. 신뢰는 제품과 같다: --pin(사람이 Pod 출력에서 옮긴 지문)에 없는 지문이면 붙지 않고, 실제 서버
인증서 공개키가 그 값과 다르면 기기 토큰을 보내기 전에 끊는다(simai_site_alert.pinning). 인증서 검증을 끄는 경로는 없다.

Pod 쪽 Tier 0 는 시뮬레이터 교실 보정(카메라 cam1~cam4)과 그 자리에서 만든 자체 서명 TLS 키(임시 폴더, 끝나면 지움)로 돈다.
위치·판정 결과는 의미가 없고 입력 경로의 지연·끊김만 본다. 결과는 제품 성능 지표가 아니다(AGENTS 규칙 7).
비밀 값(기기 토큰·터널 비밀값·TLS 비밀 키)은 출력·기록 파일에 쓰지 않는다. 기기 토큰은 노트북 환경 변수(또는 .env)에서만 읽는다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
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
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
AI = REPO / "ai"

CONTROL_REQ, VIDEO_REQ = 70000, 70001  # 대칭 매핑 요청 번호(제어·영상). 영상 포트는 4대가 같이 쓴다
DEFAULT_CONTROL_PORT, DEFAULT_VIDEO_PORT = 17000, 17001  # tunnel 의 Pod·노트북 양쪽 포트(Windows 동적 포트 예약 범위 49152~ 밖)
CUT_WINDOW_S = (90.0, 110.0)  # RunPod 프록시 연결 최대 100초(명세 3.12): 이 길이로 끝난 연결 구간을 따로 센다
_TOKEN_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S', time.gmtime())}Z] {msg}", file=sys.stderr, flush=True)


def pct(values: Iterable[float], q: float) -> float | None:
    s = sorted(v for v in values if isinstance(v, (int, float)))
    if not s:
        return None
    return round(s[min(len(s) - 1, max(0, round(q / 100 * (len(s) - 1))))], 1)


def iso(t_ms: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_ms / 1000)) + f".{t_ms % 1000:03d}Z"


class JsonlWriter:
    """줄마다 JSON 한 개. 여러 스레드(제어 채널 콜백·주기 기록)가 같이 쓴다."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(path, "a", encoding="utf-8")  # noqa: SIM115 - 측정 동안 연다
        self._lock = threading.Lock()

    def write(self, row: Mapping[str, Any]) -> None:
        line = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._f.write(line + "\n")
            self._f.flush()

    def close(self) -> None:
        with self._lock:
            self._f.close()


# ---------------------------------------------------------------------------- pod


def pod_plan(link: str, env: Mapping[str, str], cams: Iterable[str], *, control_port: int = DEFAULT_CONTROL_PORT,
             video_port: int = DEFAULT_VIDEO_PORT) -> tuple[dict[str, Any], dict[str, str]]:
    """(노트북이 붙을 대상, Tier 0 에 더할 환경 변수). 모르는 값은 지어내지 않고 ValueError(빠진 이름을 모두 적는다)."""
    cams = list(cams)
    video_ports_env = ",".join(f"{c}:{VIDEO_REQ}" for c in cams)
    if link == "tunnel":
        for name, port in (("--control-port", control_port), ("--video-port", video_port)):
            if not 1 <= port <= 65535:
                raise ValueError(f"{name} 는 1~65535 다")
        if control_port == video_port:
            raise ValueError("--control-port 와 --video-port 가 같다")
        host, listen = "127.0.0.1", "127.0.0.1"
        extra = {"SIMAI_PUBLIC_HOST": host, f"SIMAI_PUBLIC_PORT_{CONTROL_REQ}": str(control_port),
                 f"SIMAI_PUBLIC_PORT_{VIDEO_REQ}": str(video_port)}
    elif link == "direct":
        if str(AI) not in sys.path:
            sys.path.insert(0, str(AI))
        from services.tier0.endpoint_registration import (
            public_host_env,
            public_port_env,
        )

        problems = []
        host_name, host = public_host_env(env)
        if not host:
            problems.append(f"{host_name} 또는 RUNPOD_PUBLIC_IP")
        ports = {}
        for req in (CONTROL_REQ, VIDEO_REQ):
            name, raw = public_port_env(env, req)
            if not raw.isdigit() or not 1 <= int(raw) <= 65535:
                problems.append(f"{name} (템플릿 TCP 포트에 {req} 를 열었는지)")
            else:
                ports[req] = int(raw)
        if problems:
            raise ValueError("Direct TCP 공개 주소를 모른다: " + ", ".join(problems))
        control_port, video_port = ports[CONTROL_REQ], ports[VIDEO_REQ]
        listen, extra = "0.0.0.0", {}
    else:
        raise ValueError("--link 는 tunnel 또는 direct 다")
    endpoint = {"link": link, "host": host, "control_port": control_port,
                "video_ports": {c: video_port for c in cams}, "control_path": "/control"}
    tier0_env = {"SIMAI_TIER0_CONTROL_PORT": str(CONTROL_REQ), "SIMAI_TIER0_VIDEO_PORTS": video_ports_env,
                 "SIMAI_TIER0_CONTROL_LISTEN_HOST": listen, "SIMAI_TIER0_VIDEO_LISTEN_HOST": listen, **extra}
    return endpoint, tier0_env


def token_hashes(env: Mapping[str, str]) -> str:
    raw = (env.get("SIMAI_LAPTOP_TOKEN_SHA256") or "").strip()
    items = [x.strip() for x in raw.split(",") if x.strip()]
    if not items or not all(_TOKEN_HASH.match(x) for x in items):
        raise ValueError("SIMAI_LAPTOP_TOKEN_SHA256 가 없거나 'sha256:<소문자 hex 64>' 가 아니다(노트북에서 token-hash 로 만든다)")
    return ",".join(items)


def marker_row(doc: Mapping[str, Any], t_ms: int) -> dict[str, Any] | None:
    """표시 파일 한 장 → 기록 한 줄(카메라별 수신·지연·끊김, 보정 T_lag 표본). 운영 상태가 없으면 None."""
    status = doc.get("status") or {}
    live = status.get("live_input") or {}
    gpu = doc.get("gpu_priority") or {}
    load = gpu.get("load") or {}
    if not live:
        return None
    keep = ("video_connected", "ready", "decoded", "inferred", "decode_failed", "frame_age_ms_p95", "queue_wait_ms_max",
            "disconnects", "received", "gateway_to_runpod_loss", "laptop_skipped", "unattributed_gap", "late_dropped")
    return {"t": t_ms, "kind": "pod", "written_at": gpu.get("written_at"),
            "laptop_connected": status.get("laptop_connected"), "system_health": status.get("system_health"),
            "cams": {c: {k: v.get(k) for k in keep} for c, v in (live.get("cameras") or {}).items()},
            "lag": {k: load.get(k) for k in ("samples", "unknown", "lag_ms", "lag_ms_p95", "lag_ms_max", "over_budget")}}


def cmd_pod(args: argparse.Namespace) -> int:
    for p in (AI / "tests" / "services" / "tier0", AI):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    try:
        hashes = token_hashes(os.environ)
        if args.seconds <= 0:
            raise ValueError("--seconds 는 양수다")
    except ValueError as e:
        log(f"설정 오류: {e}")
        return 2
    import tier0_site as ts
    from test_i1_tls import make_cert

    from services.tier0.endpoint_registration import cert_der_from_pem, spki_sha256
    from simai_ai.io import SimObservationFrameSource

    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    run = ts.run_sim(work, ts.scenario(8_000))  # 교실 보정(카메라 4대 배치)만 쓴다
    cams = tuple(SimObservationFrameSource(run).camera_ids)
    try:
        endpoint, plan_env = pod_plan(args.link, os.environ, cams, control_port=args.control_port,
                                      video_port=args.video_port)
    except ValueError as e:
        log(f"설정 오류: {e}")
        return 2
    secrets = Path(tempfile.mkdtemp(prefix="simai-i0-link-tls-"))  # 볼륨·작업 폴더 밖, 끝나면 지운다
    proc: subprocess.Popen | None = None
    out = JsonlWriter(work / "pod.jsonl")
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        cert, key = make_cert(secrets)
        pem = cert.read_bytes()
        (work / "tier0-cert.pem").write_bytes(pem)  # 공개 인증서(비밀 키 아님)
        endpoint["tls_public_key_sha256"] = spki_sha256(cert_der_from_pem(pem))
        endpoint["written_at"] = now_ms()
        (work / "endpoint.json").write_text(json.dumps(endpoint, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        calibration = json.loads((run / "calibration.json").read_text(encoding="utf-8"))
        ts.archive_bundle(work / "boot", calibration, "cfg-1", now_ms() - 1000)
        state = work / "state"
        marker = state / "tier0.live"
        cfg = work / "tier0-live.json"
        cfg.write_text(json.dumps({
            "gateway_id": ts.GATEWAY, "state_dir": str(state), "scene_version": "scene-sim",
            "calibration_file": str(run / "calibration.json"), "input": {"mode": "live", "camera_ids": list(cams)},
            "bootstrap": {"archive": str(work / "boot"), "config_version": "cfg-1"}, "tick_ms": 100}), encoding="utf-8")
        # EC2(I1) 없이 돈다: 상속한 EC2 주소·토큰은 넘기지 않는다(health EC2_LINK_LOST 는 지연 측정과 무관)
        drop = ("SIMAI_EC2_I1_URL", "SIMAI_EC2_BASE_URL", "SIMAI_TIER0_TOKEN")
        env = {k: v for k, v in os.environ.items() if k not in drop}
        env |= {"PYTHONUTF8": "1", "SIMAI_TIER0_ID": ts.GATEWAY, "SIMAI_TIER0_TLS_CERT_FILE": str(cert),
                "SIMAI_TIER0_TLS_KEY_FILE": str(key), "SIMAI_LAPTOP_TOKEN_SHA256": hashes, **plan_env}
        if args.tier0_cmd:
            head = shlex.split(args.tier0_cmd)
        else:
            entry = shutil.which("simai-tier0")  # 이미지 진입점: 엔진 준비 → python -m services.tier0
            head = [entry] if entry else [sys.executable, "-m", "services.tier0"]
        cmd = [*head, "--config", str(cfg), "--run-seconds", str(args.ready_timeout_s + args.seconds + 30)]
        log(f"Tier 0 LIVE ({args.link}): {' '.join(cmd)}")
        with open(work / "tier0.log", "ab") as logf:
            proc = subprocess.Popen(cmd, cwd=AI, env=env, stdout=logf, stderr=subprocess.STDOUT)
        print(json.dumps({"endpoint": str(work / "endpoint.json"), "tls_public_key_sha256": endpoint["tls_public_key_sha256"],
                          "host": endpoint["host"], "control_port": endpoint["control_port"],
                          "video_port": next(iter(endpoint["video_ports"].values())), "cameras": list(cams)},
                         ensure_ascii=False), flush=True)
        last_written = None
        ready_announced = laptop_seen = False
        deadline = time.monotonic() + args.ready_timeout_s + args.seconds + 60
        while not stop.is_set() and proc.poll() is None and time.monotonic() < deadline:
            try:
                doc = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                doc = None
            if doc is not None:
                if not ready_announced and doc.get("started"):
                    log("Tier 0 준비됨: 노트북 측정(laptop)을 시작한다")
                    ready_announced = True
                if not laptop_seen and (doc.get("status") or {}).get("laptop_connected"):
                    # 노트북 제어 채널이 처음 붙은 때부터 --seconds + 여유 15초 뒤 끝낸다(노트북 쪽 기록 구간을 덮는다)
                    laptop_seen = True
                    deadline = min(deadline, time.monotonic() + args.seconds + 15)
                    log(f"노트북 연결: {args.seconds + 15:.0f}초 뒤 끝낸다")
                row = marker_row(doc, now_ms())
                if row is not None and row["written_at"] != last_written:
                    last_written = row["written_at"]
                    out.write(row)
            stop.wait(args.interval)
        return 0 if ready_announced and laptop_seen else 1
    except KeyboardInterrupt:
        return 0
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()  # Tier 0 는 SIGTERM 에 정리하고 끝낸다(services.tier0.__main__)
            try:
                proc.wait(40)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(30)
        out.close()
        shutil.rmtree(secrets, ignore_errors=True)


# ---------------------------------------------------------------------------- laptop


def read_endpoint(path: Path, pins: frozenset[str]) -> dict[str, Any]:
    """Pod 가 쓴 endpoint.json. 지문이 노트북 고정값(--pin)에 없으면 ValueError(그 대상으로는 붙지 않는다)."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or not isinstance(doc.get("video_ports"), dict) or not doc["video_ports"]:
        raise ValueError("endpoint.json 형식이 아니다(video_ports 없음)")
    if doc.get("tls_public_key_sha256") not in pins:
        raise ValueError("endpoint.json 의 공개키 지문이 --pin 과 다르다. 접속하지 않는다")
    return doc


def cmd_laptop(args: argparse.Namespace) -> int:  # pragma: no cover - gateway/relay 환경에서만 돈다(4차 세션 수동)
    import logging

    from simai_recorder.clock import SystemClock as RelayClock
    from simai_relay.config import (
        DEFAULT_ENV_FILE,
        RedactSecrets,
        load_cameras,
        load_env,
    )
    from simai_relay.epoch_store import DEFAULT_EPOCH_STATE, EpochStore
    from simai_relay.relay import RelaySession
    from simai_relay.status_report import RelayStatusPublisher
    from simai_relay.tls_sink import EndpointWatcher, TlsSinkFactory
    from simai_site_alert.app import SiteAlertApp
    from simai_site_alert.clock import SystemClock as SiteClock
    from simai_site_alert.core import SiteAlertCore
    from simai_site_alert.endpoint import EndpointResolver, Tier0Endpoint
    from simai_site_alert.link_status import SiteLinkMonitor
    from simai_site_alert.pinning import parse_pins
    from simai_site_alert.relay_status import RelayStatusReceiver
    from simai_site_alert.sound import FakeSoundBackend
    from simai_site_alert.transport import WebSocketControlChannel

    env = load_env(Path(args.env_file or DEFAULT_ENV_FILE))
    env.update({k: v for k, v in os.environ.items() if k.startswith(("SIMAI_", "RTSP_"))})
    token = (env.get("SIMAI_DEVICE_TOKEN") or "").strip()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(RedactSecrets([v for k, v in env.items() if v and (k.startswith(("RTSP_USER", "RTSP_PASS"))
                                                                          or k == "SIMAI_DEVICE_TOKEN")]))
    logging.getLogger().handlers[:] = [handler]
    logging.getLogger().setLevel(logging.INFO)
    relay_clock, site_clock = RelayClock(), SiteClock()
    try:
        if not token:
            raise ValueError("SIMAI_DEVICE_TOKEN 이 없다(환경 변수 또는 .env)")
        pins = parse_pins(args.pin or env.get("SIMAI_TIER0_PINNED_SPKI_SHA256"))
        if not pins:
            raise ValueError("--pin(Pod 출력의 tls_public_key_sha256) 이 없다. 고정값 없이는 접속하지 않는다")
        doc = read_endpoint(Path(args.endpoint), pins)
        cameras = load_cameras(Path(args.config), relay_clock, env)
        unknown = sorted({c.camera_id for c in cameras} - set(doc["video_ports"]))
        if unknown:
            raise ValueError(f"relay 설정의 camera_id {unknown} 가 Pod 카메라 {sorted(doc['video_ports'])} 에 없다")
        endpoint = Tier0Endpoint(gateway_id=doc.get("gateway_id") or "tier0-01", public_ip=doc["host"],
                                 control_port=int(doc["control_port"]),
                                 video_ports={c: int(p) for c, p in doc["video_ports"].items()},
                                 tls_public_key_sha256=doc["tls_public_key_sha256"], registered_at=int(doc["written_at"]))
    except (OSError, KeyError, TypeError, ValueError) as e:
        log(f"설정 오류: {e}")
        return 2
    fetch = lambda: endpoint  # EC2 조회 대신 고정 대상(신뢰는 pins 로만)
    out = JsonlWriter(Path(args.out))
    out.write({"t": now_ms(), "kind": "start", "link": doc.get("link"), "host": endpoint.public_ip,
               "control_port": endpoint.control_port, "video_ports": dict(endpoint.video_ports), "seconds": args.seconds})
    watcher = EndpointWatcher(fetch, pins)
    laptop_id = env.get("SIMAI_LAPTOP_ID") or "laptop-01"
    factory = TlsSinkFactory(watcher.source, device_token=token, pins=pins, laptop_id=laptop_id, ca_file=None)
    session = RelaySession(cameras, factory, relay_clock, epoch_store=EpochStore(Path(DEFAULT_EPOCH_STATE)))
    channel = WebSocketControlChannel(resolver=EndpointResolver(fetch, pins, control_path=doc.get("control_path") or "/control"),
                                      device_token=token)
    core = SiteAlertCore(FakeSoundBackend(), laptop_id=laptop_id, clock=site_clock)
    app = SiteAlertApp(core, channel, monitor=SiteLinkMonitor(), ec2=None, clock=site_clock)
    on_message, on_connected = app._on_message, app._on_connected

    def record_message(raw: str | bytes, received_mono: float) -> None:
        t2 = now_ms()
        try:
            msg = json.loads(raw)
            if isinstance(msg, dict) and msg.get("msg_type") == "clock_sync_request":
                # 현장 경보 단말은 시계 교환에 아직 답하지 않는다(clock_sync 무시). 답이 없으면 Tier 0 가 노트북–Pod 시계 차이를 몰라
                # T_lag 가 null 이고 frame_age 에 두 컴퓨터 시계 차이가 섞인다. 측정 도구가 하네스 노트북 흉내(tier0_site.Laptop)와
                # 같은 방식으로 답한다(t2 받은 시각, t3 보내는 시각, 계약 clock_sync_response)
                p = msg.get("payload") or {}
                t3 = now_ms()
                channel.send({"schema_version": "1.3.0", "msg_type": "clock_sync_response", "msg_id": str(uuid.uuid4()),
                              "sent_at": t3, "source_id": laptop_id,
                              "payload": {"exchange_id": p.get("exchange_id"), "t1_ms": p.get("t1_ms"), "t2_ms": t2,
                                          "t3_ms": t3}})
            elif isinstance(msg, dict) and msg.get("msg_type") == "health":
                p = msg.get("payload") or {}
                out.write({"t": now_ms(), "kind": "health", "pipeline_lag_ms": p.get("pipeline_lag_ms"),
                           "lower_bound_ms": p.get("pipeline_lag_lower_bound_ms"), "system_health": p.get("system_health"),
                           "cameras_ok": sum(1 for c in p.get("cameras") or () if c.get("status") == "OK")})
        except (ValueError, TypeError, AttributeError):
            pass
        on_message(raw, received_mono)

    def record_connected(connected: bool) -> None:
        out.write({"t": now_ms(), "kind": "control", "connected": bool(connected)})
        on_connected(connected)

    app._on_message, app._on_connected = record_message, record_connected  # 제품 콜백 앞에서 기록만 한다
    publisher = RelayStatusPublisher(lambda: session.heartbeat_cameras(), factory.video_cameras, port=args.status_port)
    receiver = RelayStatusReceiver(app.on_relay_report, port=args.status_port, monotonic=site_clock.monotonic)
    keep = ("tier0_video", "connects", "disconnects", "connect_failures", "pin_rejections", "auth_rejections",
            "sent_frames", "sent_bytes", "skipped", "dropped_link", "queue_max_depth")
    watcher.start()
    session.start(args.seconds)
    publisher.start()
    receiver.start()
    app.start(test_alert_s=None)
    log(f"측정 시작 ({doc.get('link')}, {endpoint.public_ip}:{endpoint.control_port}, {args.seconds:.0f}초)")
    try:
        while not session.join(timeout=args.interval):
            out.write({"t": now_ms(), "kind": "relay",
                       "cams": {s["camera_id"]: {k: s.get(k) for k in keep} for s in factory.snapshots()}})
    except KeyboardInterrupt:
        session.stop()
        session.join()
    finally:
        out.write({"t": now_ms(), "kind": "relay",
                   "cams": {s["camera_id"]: {k: s.get(k) for k in keep} for s in factory.snapshots()}})
        app.close()  # 카메라 송신(TlsCameraSink)은 RelaySession 이 끝낼 때 닫는다
        receiver.close()
        publisher.stop()
        watcher.stop()
        out.write({"t": now_ms(), "kind": "end", "errors": session.errors()})
        out.close()
    log(f"측정 끝 → {args.out}")
    return 0


# ---------------------------------------------------------------------------- summarize


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue  # 끊긴 마지막 줄
    return rows


def segments(events: list[tuple[int, bool]], start: int, end: int) -> list[dict[str, Any]]:
    """(시각, 연결됨) 변화 → 연결 구간 목록. 끝까지 이어진 구간은 cut=False."""
    out: list[dict[str, Any]] = []
    up: int | None = None
    for t, connected in sorted(events):
        if connected and up is None:
            up = t
        elif not connected and up is not None:
            out.append({"start": iso(up), "end": iso(t), "seconds": round((t - up) / 1000, 1), "cut": True})
            up = None
    if up is not None:
        out.append({"start": iso(up), "end": iso(end), "seconds": round((end - up) / 1000, 1), "cut": False})
    return out


def _delta(rows: list[dict[str, Any]], cam: str, key: str) -> int:
    vals = [r["cams"][cam].get(key) for r in rows if cam in r.get("cams", {}) and isinstance(r["cams"][cam].get(key), int)]
    return (vals[-1] - vals[0]) if len(vals) >= 2 else 0


def summarize_run(label: str, pod_rows: list[dict[str, Any]], laptop_rows: list[dict[str, Any]]) -> dict[str, Any]:
    start = next((r for r in laptop_rows if r.get("kind") == "start"), {})
    relay = [r for r in laptop_rows if r.get("kind") == "relay"]
    if start.get("t") and start.get("seconds"):
        # 측정 시간이 끝나며 relay·제어 채널을 닫는 끊김은 세지 않는다(끝내기 0.5초 전까지만 본다)
        relay = [r for r in relay if r["t"] <= start["t"] + start["seconds"] * 1000 - 500]
    if len(relay) < 2:
        raise ValueError(f"{label}: 노트북 기록에 relay 줄이 2개 미만이다")
    t0, t1 = relay[0]["t"], relay[-1]["t"]
    secs = max(1e-3, (t1 - t0) / 1000)
    pod = [r for r in pod_rows if r.get("kind") == "pod" and t0 <= (r.get("written_at") or r["t"]) <= t1]  # 표시 파일은 5초마다
    cams = sorted({c for r in relay for c in r.get("cams", {})})

    ages = [c.get("frame_age_ms_p95") for r in pod for c in r["cams"].values()]
    lag_pod = [v for r in pod for v in (r.get("lag", {}).get("lag_ms") or [])]
    lag_unknown = sum(int(r.get("lag", {}).get("unknown") or 0) for r in pod)
    health = [r for r in laptop_rows if r.get("kind") == "health" and t0 <= r["t"] <= t1]
    lag_health = [r.get("pipeline_lag_ms") for r in health]

    per_cam = {}
    timeline: list[dict[str, Any]] = []
    for cam in cams:
        sent_bytes = _delta(relay, cam, "sent_bytes")
        events = []
        prev = None
        for r in relay:
            state = (r["cams"].get(cam) or {}).get("tier0_video")
            if state is None:
                continue
            up = state == "CONNECTED"
            if up != prev:
                events.append((r["t"], up))
                if prev is not None:
                    timeline.append({"t": iso(r["t"]), "link": f"video:{cam}", "event": "UP" if up else "DOWN"})
                prev = up
        segs = segments(events, t0, t1)
        per_cam[cam] = {
            "mbps": round(sent_bytes * 8 / secs / 1e6, 3), "sent_fps": round(_delta(relay, cam, "sent_frames") / secs, 2),
            "connects": _delta(relay, cam, "connects"), "disconnects": _delta(relay, cam, "disconnects"),
            "connect_failures": _delta(relay, cam, "connect_failures"),
            "laptop_skipped": _delta(relay, cam, "skipped"), "dropped_link": _delta(relay, cam, "dropped_link"),
            "pod_received_fps": round(_delta(pod, cam, "received") / secs, 2) if pod else None,
            "pod_disconnects": _delta(pod, cam, "disconnects") if pod else None,
            "gateway_to_runpod_loss": _delta(pod, cam, "gateway_to_runpod_loss") if pod else None,
            "segments": segs}
    control_events = [(r["t"], bool(r["connected"])) for r in laptop_rows if r.get("kind") == "control" and r["t"] <= t1]
    for t, up in control_events:
        if t0 <= t:
            timeline.append({"t": iso(t), "link": "control", "event": "UP" if up else "DOWN"})
    control_segs = segments(control_events, t0, t1)
    all_segs = control_segs + [s for c in per_cam.values() for s in c["segments"]]
    cut_100 = [s for s in all_segs if s["cut"] and CUT_WINDOW_S[0] <= s["seconds"] <= CUT_WINDOW_S[1]]
    return {
        "label": label, "link": start.get("link"), "window": {"start": iso(t0), "end": iso(t1), "seconds": round(secs, 1)},
        "frame_age_ms_p95": {"p50": pct(ages, 50), "max": pct(ages, 100), "samples": sum(1 for a in ages if a is not None)},
        "pipeline_lag_ms_pod": {"p50": pct(lag_pod, 50), "p95": pct(lag_pod, 95), "max": pct(lag_pod, 100),
                                "samples": len(lag_pod), "unknown": lag_unknown},
        "pipeline_lag_ms_health": {"p50": pct(lag_health, 50), "p95": pct(lag_health, 95),
                                   "null": sum(1 for v in lag_health if v is None), "messages": len(lag_health)},
        "throughput_mbps": round(sum(c["mbps"] for c in per_cam.values()), 3),
        "video_reconnects": sum(c["disconnects"] for c in per_cam.values()),
        "control_disconnects": sum(1 for s in control_segs if s["cut"]),
        "control_segments": control_segs,
        "cut_near_100s": len(cut_100),
        "longest_segment_s": max((s["seconds"] for s in all_segs), default=None),
        "cameras": per_cam,
        "timeline": sorted(timeline, key=lambda e: e["t"]),
    }


def table(results: list[dict[str, Any]]) -> str:
    head = ("| 경로 | 구간(s) | frame_age p95 중앙·최대(ms) | T_lag p50·p95(ms, Pod) | health lag null | 처리량(Mbps) "
            "| 영상 재연결 | 제어 끊김 | 100초 끊김 | 최장 연결(s) |")
    rows = [head, "|" + "---|" * 10]
    for r in results:
        fa, lag, h = r["frame_age_ms_p95"], r["pipeline_lag_ms_pod"], r["pipeline_lag_ms_health"]
        rows.append(f"| {r['label']} | {r['window']['seconds']} | {fa['p50']}·{fa['max']} | {lag['p50']}·{lag['p95']} "
                    f"| {h['null']}/{h['messages']} | {r['throughput_mbps']} | {r['video_reconnects']} "
                    f"| {r['control_disconnects']} | {r['cut_near_100s']} | {r['longest_segment_s']} |")
    return "\n".join(rows)


def cmd_summarize(args: argparse.Namespace) -> int:
    results = []
    try:
        for spec in args.run:
            label, sep, paths = spec.partition("=")
            pod_path, comma, laptop_path = paths.partition(",")
            if not sep or not comma or not label:
                raise ValueError(f"--run {spec!r} 은 '<이름>=<pod.jsonl>,<laptop.jsonl>' 형식이다")
            results.append(summarize_run(label, read_jsonl(Path(pod_path)), read_jsonl(Path(laptop_path))))
    except (OSError, ValueError) as e:
        log(f"오류: {e}")
        return 2
    doc = {"tool": "i0_link_measure", "ticket": "S15P31A101-288", "generated_at": iso(now_ms()),
           "note": "시뮬레이터 보정·녹화 재생 입력의 망 구간 측정. 제품 성능 지표가 아니다(AGENTS 규칙 7)", "runs": results}
    md = table(results)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        out.with_suffix(".md").write_text(md + "\n", encoding="utf-8")
    print(md)
    return 0


def cmd_token_hash(_: argparse.Namespace) -> int:
    token = (os.environ.get("SIMAI_DEVICE_TOKEN") or "").strip()
    if not token:
        log("환경 변수 SIMAI_DEVICE_TOKEN 이 없다")
        return 2
    print("sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest())
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="막힌 망 I0 예비 경로 측정 (S15P31A101-288)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pod", help="Pod: Tier 0 LIVE + 표시 파일 기록")
    p.add_argument("--link", choices=["tunnel", "direct"], required=True)
    p.add_argument("--seconds", type=float, required=True, help="측정 시간(노트북 laptop 과 같게)")
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--ready-timeout-s", type=float, default=900, help="엔진 준비·노트북 시작을 기다리는 여유")
    p.add_argument("--control-port", type=int, default=DEFAULT_CONTROL_PORT, help="tunnel: 제어 포트(노트북 로컬 포트와 같다)")
    p.add_argument("--video-port", type=int, default=DEFAULT_VIDEO_PORT, help="tunnel: 영상 포트(4대 공용)")
    p.add_argument("--interval", type=float, default=1.0)
    p.add_argument("--tier0-cmd", default=None, help="시험용: Tier 0 명령(기본 simai-tier0)")
    p.set_defaults(func=cmd_pod)
    p = sub.add_parser("laptop", help="노트북: relay + 제어 채널 + 기록 (gateway/relay uv 환경)")
    p.add_argument("--endpoint", required=True, help="Pod 가 쓴 endpoint.json")
    p.add_argument("--pin", default=None, help="Pod 출력의 tls_public_key_sha256 (없으면 SIMAI_TIER0_PINNED_SPKI_SHA256)")
    p.add_argument("--config", required=True, help="relay TOML ([[cameras]] camera_id 는 Pod 카메라 cam1~cam4)")
    p.add_argument("--seconds", type=float, required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--env-file", default=None, help="기기 토큰·카메라 계정 .env (기본: 저장소 루트 .env)")
    p.add_argument("--status-port", type=int, default=47031, help="relay → 단말 상태 보고 UDP 포트(현장 경보 단말을 같이 띄우지 않는다)")
    p.add_argument("--interval", type=float, default=1.0)
    p.set_defaults(func=cmd_laptop)
    p = sub.add_parser("summarize", help="pod.jsonl·laptop.jsonl → JSON·표")
    p.add_argument("--run", action="append", required=True, help="<이름>=<pod.jsonl>,<laptop.jsonl> (여러 번)")
    p.add_argument("--out", default=None, help="JSON 경로(같은 이름 .md 에 표)")
    p.set_defaults(func=cmd_summarize)
    p = sub.add_parser("token-hash", help="SIMAI_DEVICE_TOKEN 의 sha256 (Pod 에 넘길 값)")
    p.set_defaults(func=cmd_token_hash)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
