"""Tier 0 진입점(S15P31A101-171) 시험 도구: SIM 장면·시작 구성 묶음·설정·노트북(I0) 시험 상대.

- SIM 장면: 9 x 7.5 m 교실, 모서리 카메라 4대(sim/scenarios/basic.json 과 같은 배치). 한 사람이 걸어 들어와 R1 구역
  (HIGH, 현장 경보 대상) 안에 선다. 시뮬레이터(`python -m sim.run`)로 2D keypoint 를 만든다(GPU 없음).
- 시작 구성 묶음: EC2 가 보내는 config_bundle 과 같은 구성 요소(구역·규칙·보정·작업면)를 메모리 저장소에 올리고 실제
  BundleVerifier 로 검증한 뒤 BundleArchive 에 저장한다. 규칙은 등록부 전체(R1·R2·R3·R5 교실 프로파일·R7).
- 노트북 시험 상대(Laptop): I0 제어 채널(wss, 기기 토큰)에 붙어 local_alert·alarm_stop·health 를 받고, clock_sync_request 에
  답하고(같은 컴퓨터 시계), 1초마다 laptop_heartbeat 를 보내고, site_ack 를 보낼 수 있다. websockets 동기 클라이언트라
  TLS 는 SerialTLSSocket 으로 한다(Tier 0 가 보내는 세션 티켓 처리와 송신이 한 SSL 객체에서 겹치지 않게, S15P31A101-175).
  운영 노트북(gateway/site_alert)은 asyncio 클라이언트라 TLS 를 이벤트 루프 한 스레드에서만 만진다.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

AI_DIR = Path(__file__).resolve().parents[3]
REPO = AI_DIR.parent
GATEWAY = "tier0-01"
LAPTOP_TOKEN = "laptop-device-token-171"
ZONE = {
    "zone_id": "z-r1",
    "zone_version": "zone-1",
    "judge_type": "GROUND_FOOTPRINT",
    "polygon_xy": [[3.6, 3.0], [5.6, 3.0], [5.6, 4.6], [3.6, 4.6]],
    "z_range": None,
    "severity": "HIGH",
}
RULES = (("R1", 1), ("R2", 1001), ("R3", 1001), ("R5", 1001), ("R7", 1))


def scenario(duration_ms: int = 60_000, *, start_time_ms: int = 1760342400000) -> dict[str, Any]:
    base = json.loads((AI_DIR / "sim" / "scenarios" / "basic.json").read_text(encoding="utf-8"))
    doc = copy.deepcopy(base)
    doc["name"] = "tier0-e2e"
    doc["description"] = "Tier 0 진입점 시험: 한 사람이 R1 구역으로 걸어 들어가 선다"
    doc["duration_ms"] = duration_ms
    doc["start_time_ms"] = start_time_ms
    doc["people"] = [
        {
            "id": "walker",
            "height_m": 1.72,
            "waypoints": [{"t_ms": 0, "xy": [1.5, 2.0]}, {"t_ms": 2500, "xy": [4.6, 3.8]}],
        }
    ]
    return doc


def run_sim(workdir: Path, doc: dict[str, Any] | None = None, seed: int = 1) -> Path:
    doc = doc or scenario()
    path = workdir / f"{doc['name']}.json"
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    out = workdir / f"{doc['name']}-{seed}"
    result = subprocess.run(
        [sys.executable, "-m", "sim.run", "--scenario", str(path), "--out", str(out), "--seed", str(seed)],
        cwd=AI_DIR, capture_output=True, text=True, encoding="utf-8", env=dict(os.environ, PYTHONUTF8="1"),
    )
    if result.returncode != 0:
        raise RuntimeError(f"시뮬레이터 실패: {result.stderr}")
    return out


# --------------------------------------------------------------------------- 구성 묶음


def _dumps(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")


class _DictFetcher:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    def fetch(self, uri: str) -> bytes:
        from simai_ai.config import FetchError

        if uri not in self.blobs:
            raise FetchError("없음")
        return self.blobs[uri]


def bundle_message(
    calibration: dict[str, Any], version: str, issued_at: int, *, zones=None, rules=RULES, fetcher: _DictFetcher | None = None,
    work_surfaces=(), ppe_policy: dict[str, Any] | None = None, calibration_verification: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], _DictFetcher]:
    """EC2 처럼 구성 요소 → 매니페스트 → config_bundle 알림을 만든다(바이트는 fetcher 에). work_surfaces: 작업면(R2·R3).
    ppe_policy: 선택 구성 요소 ppe_policy 내용(config_bundle 1.3.0, R9). None 이면 싣지 않는다.
    calibration_verification: components.calibration.verification(config_bundle 1.4.0, 세팅 카메라 확인 통과 근거, -745)."""
    from simai_ai.config import sha256_of

    fetcher = fetcher or _DictFetcher()
    base = f"https://ec2.test/config/{version}"
    docs = {
        "zones": {"version": f"zones-{version}", "zones": [ZONE | {"zone_version": f"zone-{version}"}] if zones is None else zones},
        "rules": {"version": f"rules-{version}", "rule_versions": [{"rule_id": r, "rule_version": v} for r, v in rules]},
        "calibration": calibration,
        "operational_geometry": {"version": f"geom-{version}", "work_surfaces": list(work_surfaces),
                                 "static_objects": []},
    }
    if ppe_policy is not None:
        docs["ppe_policy"] = ppe_policy
    components = {}
    for name, doc in docs.items():
        data = _dumps(doc)
        uri = f"{base}/{name}.json"
        fetcher.blobs[uri] = data
        comp = {"version": doc.get("version") or doc.get("calibration_version"), "sha256": sha256_of(data), "uri": uri}
        if name == "rules":
            comp["rule_versions"] = [{"rule_id": r, "rule_version": v} for r, v in rules]
        if name == "calibration" and calibration_verification is not None:
            comp["verification"] = calibration_verification
        components[name] = comp
    manifest = _dumps({"config_version": version, "issued_at": issued_at, "components": components})
    fetcher.blobs[f"{base}/manifest.json"] = manifest
    message = {
        "schema_version": "2.0.0",
        "msg_type": "config_bundle",
        "msg_id": str(uuid.uuid4()),
        "sent_at": issued_at + 1,
        "source_id": "ec2",
        "payload": {
            "config_version": version,
            "previous_config_version": None,
            "issued_at": issued_at,
            "bundle_uri": f"{base}/manifest.json",
            "bundle_hash": sha256_of(manifest),
            "components": components,
        },
    }
    return message, fetcher


def archive_bundle(archive_root: Path, calibration: dict[str, Any], version: str, issued_at: int, **kwargs: Any) -> Any:
    """구성 묶음을 검증해 BundleArchive 에 저장하고 ConfigBundle 을 돌려준다(시작 묶음)."""
    from simai_ai.config import BundleArchive, BundleVerifier
    from simai_ai.risk import FixedClock, default_registry

    message, fetcher = bundle_message(calibration, version, issued_at, **kwargs)
    verifier = BundleVerifier(fetcher, default_registry(), lambda v: v == calibration["calibration_version"],
                              FixedClock(issued_at))
    bundle = verifier.verify(message)
    BundleArchive(archive_root).save(bundle)
    return bundle


# --------------------------------------------------------------------------- 설정·포트


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def write_config(path: Path, *, run_dir: Path, state_dir: Path, anchor_wall_ms: int, archive: Path, version: str,
                 lifecycle: dict[str, Any] | None = None) -> Path:
    doc = {
        "gateway_id": GATEWAY,
        "state_dir": str(state_dir),
        "scene_version": "scene-sim",
        "input": {"mode": "sim", "run_dir": str(run_dir), "anchor_wall_ms": anchor_wall_ms},
        "bootstrap": {"archive": str(archive), "config_version": version},
        "lifecycle": lifecycle or {"clear_hold_ms": 1500, "persistence": [2, 3], "resume_window_ms": 10000},
        "tick_ms": 100,
        "checkpoint_interval_s": 0.5,
    }
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return path


def tier0_env(*, cert: Path, key: Path, control_port: int, ec2_url: str | None, token: str = "i1-tls-secret-token-4d1b"
              ) -> dict[str, str]:
    env = {
        "SIMAI_TIER0_ID": GATEWAY,
        "SIMAI_TIER0_CONTROL_PORT": "70000",
        "RUNPOD_TCP_PORT_70000": str(control_port),
        "SIMAI_TIER0_CONTROL_LISTEN_HOST": "127.0.0.1",
        "SIMAI_TIER0_TLS_CERT_FILE": str(cert),
        "SIMAI_TIER0_TLS_KEY_FILE": str(key),
        "SIMAI_LAPTOP_TOKEN_SHA256": "sha256:" + hashlib.sha256(LAPTOP_TOKEN.encode()).hexdigest(),
        # 엔드포인트 등록(-159): 값이 다 있으면 I1 로 tier0_endpoint 를 보낸다
        "RUNPOD_PUBLIC_IP": "127.0.0.1",
        "SIMAI_TIER0_VIDEO_PORTS": "cam1:70001,cam2:70001,cam3:70001,cam4:70001",
        "RUNPOD_TCP_PORT_70001": "40001",
    }
    if ec2_url is not None:
        env |= {"SIMAI_EC2_I1_URL": ec2_url, "SIMAI_TIER0_TOKEN": token, "SIMAI_EC2_CA_FILE": str(cert)}
    return env


def wait_until(cond, timeout: float = 10.0, step: float = 0.02) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return bool(cond())


# --------------------------------------------------------------------------- 노트북 시험 상대


class Laptop:
    """I0 제어 채널 시험 상대(gateway/site_alert 역할). 받은 메시지를 연결마다 모은다."""

    def __init__(self, port: int, ca_file: Path, *, heartbeat_s: float = 1.0, answer_clock: bool = True) -> None:
        self.url = f"wss://127.0.0.1:{port}/control"
        self._ssl = ssl.create_default_context(cafile=str(ca_file))
        self.heartbeat_s = heartbeat_s
        self.answer_clock = answer_clock
        self.heartbeats_enabled = True
        self.connections: list[list[dict[str, Any]]] = []
        self._lock = threading.Lock()
        self._ws: Any = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.connected = threading.Event()

    def start(self) -> Laptop:
        self._thread = threading.Thread(target=self._run, name="laptop", daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        from websockets.sync.client import connect

        from services.tier0.serial_tls import SerialTLSContext

        while not self._stop.is_set():
            try:
                ws = connect(self.url, ssl=SerialTLSContext(self._ssl),
                             additional_headers={"Authorization": f"Bearer {LAPTOP_TOKEN}"},
                             open_timeout=2, compression=None, proxy=None).__enter__()
            except Exception:  # noqa: BLE001 - Tier 0 가 아직 없거나 재시작 중
                self._stop.wait(0.2)
                continue
            messages: list[dict[str, Any]] = []
            with self._lock:
                self.connections.append(messages)
                self._ws = ws
            self.connected.set()
            beat = threading.Thread(target=self._beat, args=(ws,), daemon=True)
            beat.start()
            try:
                for raw in ws:
                    msg = json.loads(raw)
                    with self._lock:
                        messages.append(msg)
                    if msg.get("msg_type") == "clock_sync_request" and self.answer_clock:
                        self._answer(ws, msg)
            except Exception:  # noqa: BLE001
                pass
            finally:
                with self._lock:
                    self._ws = None
                self.connected.clear()

    def _answer(self, ws: Any, request: dict[str, Any]) -> None:
        t2 = time.time_ns() // 1_000_000
        payload = request["payload"]
        response = {
            "schema_version": "1.3.0",
            "msg_type": "clock_sync_response",
            "msg_id": str(uuid.uuid4()),
            "sent_at": t2,
            "source_id": "laptop-01",
            "payload": {"exchange_id": payload["exchange_id"], "t1_ms": payload["t1_ms"], "t2_ms": t2,
                        "t3_ms": time.time_ns() // 1_000_000},
        }
        ws.send(json.dumps(response))

    def _beat(self, ws: Any) -> None:
        seq = 0
        while not self._stop.is_set() and self._ws is ws:
            if self.heartbeats_enabled:
                seq += 1
                now = time.time_ns() // 1_000_000
                try:
                    ws.send(json.dumps({
                        "schema_version": "1.2.0", "msg_type": "laptop_heartbeat", "msg_id": str(uuid.uuid4()),
                        "sent_at": now, "source_id": "laptop-01",
                        "payload": {"heartbeat_seq": seq, "tier0": {"control": "CONNECTED", "last_message_at": now,
                                                                     "video_cameras": []},
                                    "site_display": "CONTROL_NORMAL",
                                    "alarm": {"sounding_event_ids": [], "displayed_event_ids": []}},
                    }))
                except Exception:  # noqa: BLE001
                    return
            self._stop.wait(self.heartbeat_s)

    def send_site_ack(self, event_id: str, event_sequence: int) -> str:
        ack_id = str(uuid.uuid4())
        now = time.time_ns() // 1_000_000
        message = {
            "schema_version": "2.1.0", "msg_type": "site_ack", "msg_id": str(uuid.uuid4()), "sent_at": now,
            "source_id": "laptop-01",
            "payload": {"event_id": event_id, "event_sequence": event_sequence,
                        "ack": {"ack_id": ack_id, "channel": "SITE_BUTTON", "actor_role": "SITE",
                                "actor_id": "laptop-01", "acked_at": now}},
        }
        with self._lock:
            ws = self._ws
        assert ws is not None, "노트북 제어 채널이 연결돼 있지 않다"
        ws.send(json.dumps(message))
        return ack_id

    def messages(self, index: int = -1, kind: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if not self.connections:
                return []
            msgs = list(self.connections[index])
        return [m for m in msgs if kind is None or m.get("msg_type") == kind]

    def all_messages(self, kind: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            msgs = [m for conn in self.connections for m in conn]
        return [m for m in msgs if kind is None or m.get("msg_type") == kind]

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
        if self._thread is not None:
            self._thread.join(5)
