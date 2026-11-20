"""GPU 자동 부팅 simai-gpu-boot (S15P31A101-700). 가짜 EC2(등록·보고·캐시 서명, 409 포함)·가짜 S3·가짜 nvidia-smi·가짜 simai-bootstrap 으로
등록 → 토큰 파일 → 시작 묶음 → preflight → lobby → 단계 보고 → 정지(ENDED) 를 본다. GPU·RunPod·실제 EC2 를 쓰지 않는다."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import io
import json
import os
import re
import socket
import sys
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from tests.deploy.conftest import REPO, load_script

CONTRACTS = REPO / "contracts"
SECRET = "boot-secret-" + "s" * 32
WORKER_TOKEN = "W" * 43
TIER0_TOKEN = "T" * 43
SESSION = "6f9b2c1e-3d4a-4b5c-8d7e-0f1a2b3c4d5e"


@pytest.fixture
def gb():
    return load_script("simai_gpu_boot")


def _validator(name: str) -> Draft202012Validator:
    registry, target = Registry(), None
    for path in CONTRACTS.rglob("*.schema.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
        if path.name == f"{name}.schema.json":
            target = schema
    return Draft202012Validator(target, registry=registry)


def _cert_pair() -> tuple[str, str]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "tier0-test")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + dt.timedelta(days=1)).sign(key, hashes.SHA256()))
    spki = cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    pem_cert = cert.public_bytes(serialization.Encoding.PEM).decode()
    pem_key = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    return pem_cert, pem_key + "#" + hashlib.sha256(spki).hexdigest()


def _tar_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class FakeEc2:
    """가짜 EC2 + S3. 등록·보고는 계약 스키마로 검사해 어긋나면 errors 에 남긴다."""

    def __init__(self, calibration: bytes, config: bool = True, laptop: list[str] | None = None) -> None:
        self.calibration = calibration
        self.config = config
        self.laptop = ["sha256:" + "c" * 64] if laptop is None else laptop
        self.registers: list[dict] = []
        self.reports: list[dict] = []
        self.signs: list[dict] = []
        self.objects: dict[str, bytes] = {}
        self.errors: list[str] = []
        self.end_after_reports: int | None = None
        self.headers_seen: list[dict] = []
        self.put_status: int | None = None  # 정하면 서명 PUT 에 이 코드만 준다(저장소 쓰기 거부 403 등)
        self.v_reg, self.v_rep = _validator("gpu_session_register"), _validator("gpu_session_report")
        self.v_res = _validator("gpu_session_register_result")
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _body(self) -> bytes:
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def _send(self, code: int, doc: dict | None = None, raw: bytes | None = None) -> None:
                data = raw if raw is not None else (json.dumps(doc).encode() if doc is not None else b"")
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                body = json.loads(self._body() or b"{}")
                auth = self.headers.get("Authorization")
                if self.path == "/api/gpu/sessions":
                    outer.errors += [f"register: {e.message}" for e in outer.v_reg.iter_errors(body)]
                    if auth != f"Bearer {SECRET}":
                        return self._send(401, {"code": "UNAUTHORIZED"})
                    outer.registers.append(body)
                    return self._send(201, outer.result())
                m = re.fullmatch(r"/api/gpu/sessions/([0-9a-f-]+)/reports", self.path)
                if m:
                    outer.errors += [f"report: {e.message}" for e in outer.v_rep.iter_errors(body)]
                    outer.reports.append(body["payload"])
                    if outer.end_after_reports is not None and len(outer.reports) > outer.end_after_reports:
                        return self._send(409, {"code": "GPU_SESSION_ENDED"})
                    return self._send(204)
                if self.path == "/api/worker/gpu-cache/sign":
                    if auth != f"Bearer {WORKER_TOKEN}":
                        return self._send(401, {"code": "UNAUTHORIZED"})
                    outer.signs.append(body)
                    key = f"job-objects/gpu_cache/{body['kind']}/{body['cache_key']}.tar"  # 서버 GpuCacheService.PREFIX
                    have = outer.objects.get(key)
                    base = f"http://127.0.0.1:{outer.port}"
                    if body["method"] == "GET":
                        if have is None:
                            return self._send(200, {"object_key": key, "hit": False})
                        return self._send(200, {"object_key": key, "hit": True, "size_bytes": len(have),
                                                "sha256": hashlib.sha256(have).hexdigest(), "method": "GET",
                                                "url": f"{base}/obj/{key}?X-Amz-Signature=secret", "headers": {}})
                    if have is not None:
                        return self._send(200, {"object_key": key, "hit": True, "size_bytes": len(have)})
                    return self._send(200, {"object_key": key, "hit": False, "method": "PUT",
                                            "url": f"{base}/put/{key}?X-Amz-Signature=secret",
                                            "headers": {"if-none-match": "*", "x-amz-checksum-sha256": "x"}})
                return self._send(404)

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/cal.json":
                    return self._send(200, raw=outer.calibration)
                if path.startswith("/obj/") and path[5:] in outer.objects:
                    return self._send(200, raw=outer.objects[path[5:]])
                return self._send(404)

            def do_PUT(self):
                path = self.path.split("?", 1)[0]
                data = self._body()
                outer.headers_seen.append({k.lower(): v for k, v in self.headers.items()})
                if path.startswith("/put/"):
                    if outer.put_status is not None:
                        return self._send(outer.put_status)
                    if path[5:] in outer.objects:
                        return self._send(412)
                    outer.objects[path[5:]] = data
                    return self._send(200)
                return self._send(404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def result(self) -> dict:
        config = None
        if self.config:
            config = {"config_version": "cfg-41", "manifest": {"uri": "https://s3.example/m.json", "sha256": "sha256:" + "d" * 64},
                      "calibration": {"version": "cal-1", "uri": f"{self.url}/cal.json",
                                      "sha256": "sha256:" + hashlib.sha256(self.calibration).hexdigest()}}
        doc = {"schema_version": "1.0.0", "msg_type": "gpu_session_register_result", "msg_id": "0192a6f0-7c1e-7b3a-9f2d-000000006994",
               "sent_at": 1, "source_id": "ec2", "payload": {
                   "session_id": SESSION, "registered_at": 1, "superseded_session_ids": [],
                   "worker": {"worker_id": "worker-gpu-01", "token": WORKER_TOKEN},
                   "tier0": {"gateway_id": "tier0-lobby-s9", "token": TIER0_TOKEN},
                   "report": {"interval_ms": 1000, "stale_after_ms": 600000},
                   "start": {"ec2_base_url": "https://simai.example", "site_id": "lobby", "camera_ids": ["cam_A", "cam_B"],
                             "measured_heights_m": {"cam_A": 2.44}, "laptop_token_sha256": self.laptop,
                             "scene_version": None, "config": config}}}
        # 가짜 응답의 calibration uri 는 http 라 계약(https)과 다르다. 나머지는 계약대로인지 본다
        errs = [e.message for e in self.v_res.iter_errors(doc) if "https" not in e.message and "^https" not in str(e.schema)]
        assert not errs, errs
        return doc

    def close(self) -> None:
        self.server.shutdown()


FAKE_BS = r'''
import json, os, sys, time
args = sys.argv[1:]
out = os.environ["FAKE_DIR"]
leaked = [k for k in ("SIMAI_GPU_BOOT_SECRET", "SIMAI_TIER0_TLS_PRIVATE_KEY") if k in os.environ]
with open(os.path.join(out, args[0] + ".json"), "w") as f:
    json.dump({"argv": args, "leaked": leaked, "hf": "HF_TOKEN" in os.environ,
               "cope": os.environ.get("SIMAI_COPE_ROOT"), "vllm_cache": os.environ.get("VLLM_CACHE_ROOT")}, f)
if args[0] == "preflight":
    sys.exit(int(os.environ.get("FAKE_PREFLIGHT_RC", "0")))
if args[0] == "lobby":
    work = args[args.index("--work") + 1]
    os.makedirs(os.path.join(work, "state"), exist_ok=True)
    time.sleep(0.3)
    doc = {"gpu_priority": {"phase": "running", "reserve": {"state": "OK"}, "written_at": time.time_ns() // 1_000_000}}
    with open(os.path.join(work, "state", "tier0.live"), "w") as f:
        json.dump(doc, f)
    time.sleep(float(os.environ.get("FAKE_LOBBY_S", "60")))
    sys.exit(int(os.environ.get("FAKE_LOBBY_RC", "0")))
'''

FAKE_SMI = 'print("NVIDIA H100 80GB HBM3, 9.0, 81559")\n'


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def world(tmp_path, gb):
    cal = json.dumps({"calibration_version": "cal-1"}).encode()
    ec2 = FakeEc2(cal)
    fake = tmp_path / "fake"
    fake.mkdir()
    (fake / "bs.py").write_text(FAKE_BS, encoding="utf-8")
    (fake / "smi.py").write_text(FAKE_SMI, encoding="utf-8")
    cert, key_and_spki = _cert_pair()
    key, spki = key_and_spki.rsplit("#", 1)
    env = {
        "PATH": os.environ.get("PATH", ""), "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "SIMAI_GPU_BOOT_SECRET": SECRET, "SIMAI_GPU_BOOT_ID": "gpu-boot-runpod", "SIMAI_EC2_BASE_URL": ec2.url,
        "RUNPOD_POD_ID": "abc123pod", "RUNPOD_DC_ID": "AP-JP-1", "RUNPOD_PUBLIC_IP": "203.0.113.24",
        "RUNPOD_TCP_PORT_17000": "22170", "RUNPOD_TCP_PORT_17001": "22171", "RUNPOD_TCP_PORT_22": "22122",
        "SIMAI_IMAGE_DIGEST": "sha256:" + "e" * 64,
        "SIMAI_TIER0_TLS_CERT_PEM": base64.b64encode(cert.encode()).decode(),  # base64 한 줄(RunPod Secret 형식)
        "SIMAI_TIER0_TLS_PRIVATE_KEY": key,
        "SIMAI_TIER0_PINNED_SPKI_SHA256": "sha256:" + spki,
        "HF_TOKEN": "hf_" + "x" * 30,
        "SIMAI_ROOT": str(tmp_path / "srv" / "simai"),
        "SIMAI_CACHE_ROOT": str(tmp_path / "cache"),
        "SIMAI_VLLM_PORT": str(_free_port()),
        "SIMAI_BOOT_VLLM_READY_TIMEOUT_S": "0.5",
        "FAKE_DIR": str(fake),
    }
    yield {"ec2": ec2, "env": env, "fake": fake, "tmp": tmp_path, "spki": spki, "key": key}
    ec2.close()


def _boot(gb, w, **kw):
    return gb.Boot(dict(w["env"]), http=gb.Http(allow_http=True, timeout=10),
                   smi=[sys.executable, str(w["fake"] / "smi.py")],
                   bootstrap_cmd=[sys.executable, str(w["fake"] / "bs.py")],
                   state_dir=w["tmp"] / "state", poll_s=0.05, **kw)


def _wait(cond, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _run_bg(boot):
    box: dict = {}
    t = threading.Thread(target=lambda: box.setdefault("rc", boot.run()), daemon=True)
    t.start()
    return t, box


def test_auto_registers_writes_private_files_and_reaches_ready_then_reports_ended(gb, world):
    ec2, fake = world["ec2"], world["fake"]
    # Re-ID 묶음은 캐시에 있다(지난 세션이 올림) → Tier 0 전에 받아 COPE 경로를 lobby 환경에 넣는다
    reid = _tar_bytes({"cope/configs/MSMT17/cope.yml": b"x", "weights/cope_msmt17.pth": b"w", "clip-cache/ViT-B-16.pt": b"c"})
    ec2.objects[f"job-objects/gpu_cache/reid/{gb.reid_cache_key({})}.tar"] = reid
    boot = _boot(gb, world)
    t, box = _run_bg(boot)
    assert _wait(lambda: any(r["stage"] == "READY" for r in ec2.reports)), ec2.reports
    boot.stop.set()
    t.join(timeout=30)
    assert box["rc"] == 0
    assert not ec2.errors, ec2.errors

    reg = ec2.registers[0]
    p = reg["payload"]
    assert reg["source_id"] == "gpu-boot-runpod" and p["provider"] == "RUNPOD" and p["instance_id"] == "abc123pod"
    assert p["region"] == "AP-JP-1" and p["public_host"] == "203.0.113.24" and p["gpu_type"] == "NVIDIA H100 80GB HBM3"
    assert {(x["role"], x["public_port"], x["requested_port"]) for x in p["ports"]} == {
        ("CONTROL", 22170, 17000), ("VIDEO", 22171, 17001), ("SSH", 22122, 22)}
    assert p["tls_public_key_sha256"] == "sha256:" + world["spki"] and p["price_per_hour_usd"] is None
    assert SECRET not in json.dumps(reg)

    stages = [r["stage"] for r in ec2.reports]
    assert stages[0] == "WEIGHTS" and "ENGINE" in stages and "READY" in stages and stages[-1] == "ENDED"
    assert stages.index("ENGINE") < stages.index("READY")
    ready = next(r for r in ec2.reports if r["stage"] == "READY")
    assert ready["reason_code"] is None and "vLLM 미준비" in ready.get("detail", "")  # vLLM 이 시한 안에 안 뜸
    assert ec2.reports[-1]["reason_code"] == "PROVIDER_STOP"

    private = world["tmp"] / "srv" / ".simai-private" / "gpu-session"
    assert (private / "worker-gpu-01.token").read_text().strip() == WORKER_TOKEN
    assert (private / "tier0.token").read_text().strip() == TIER0_TOKEN
    if os.name != "nt":
        for f in ("worker-gpu-01.token", "tier0.token", "lobby-bundle/tls/tier0.key"):
            assert (private / f).stat().st_mode & 0o777 == 0o600, f
    bundle = private / "lobby-bundle"
    assert json.loads((bundle / "calibration.json").read_text()) == {"calibration_version": "cal-1"}
    assert (bundle / "laptop_token.sha256").read_text().strip() == "sha256:" + "c" * 64
    assert "PRIVATE KEY" in (bundle / "tls" / "tier0.key").read_text()
    info = json.loads((private / "session.json").read_text())
    assert info["session_id"] == SESSION and WORKER_TOKEN not in json.dumps(info)

    pre = json.loads((fake / "preflight.json").read_text())
    assert pre["argv"][:3] == ["preflight", "--sets", "tier0"] and pre["hf"] and not pre["leaked"]
    lobby = json.loads((fake / "lobby.json").read_text())
    argv = lobby["argv"]
    flat = " ".join(argv)
    assert WORKER_TOKEN not in flat and TIER0_TOKEN not in flat and SECRET not in flat  # 토큰은 파일 경로로만
    opt = {argv[i]: argv[i + 1] for i in range(1, len(argv) - 1) if argv[i].startswith("--")}
    assert opt["--camera-ids"] == "cam_A,cam_B" and opt["--measured-heights"] == "cam_A=2.44"  # 높이 모르는 카메라는 뺀다
    assert opt["--video-ports"] == "cam_A:17001,cam_B:17001" and opt["--control-port"] == "17000"
    assert opt["--public-host"] == "127.0.0.1" and opt["--ec2-base-url"] == "https://simai.example"
    assert opt["--config-version"] == "cfg-41" and opt["--scene-version"] == "scene-0" and opt["--gateway-id"] == "tier0-lobby-s9"
    assert opt["--worker-id"] == "worker-gpu-01" and opt["--tier0-token-file"].endswith("tier0.token")
    # 80GB: PPE 를 더한 자동 부팅 기본(S15P31A101-718). preflight 도 같은 목록으로 GPU 예산을 본다
    assert opt["--services"] == gb.BOOT_SERVICES and pre["argv"][-2:] == ["--services", gb.BOOT_SERVICES]
    assert not lobby["leaked"] and lobby["hf"]
    assert lobby["cope"] == str(world["tmp"] / "srv" / "simai" / "reid" / "cope")
    assert lobby["vllm_cache"] == str(world["tmp"] / "cache" / "vllm" / "vllm")
    assert not (world["tmp"] / "state" / "ended.json").exists()  # 스스로 멈춘 것은 남기지 않는다(다시 켜면 다시 등록)
    assert boot.cache.results.get("reid") == "hit"


def test_session_ended_409_stops_lobby_and_never_registers_again(gb, world):
    ec2 = world["ec2"]
    ec2.end_after_reports = 2  # 세 번째 보고부터 409 GPU_SESSION_ENDED(새 등록에 밀림)
    boot = _boot(gb, world)
    t, box = _run_bg(boot)
    assert _wait(lambda: (world["tmp"] / "state" / "ended.json").is_file())
    assert _wait(lambda: boot.child is not None and boot.child.poll() is not None)  # lobby 를 멈췄다
    n_reports = len(ec2.reports)
    time.sleep(1.5)
    assert len(ec2.reports) == n_reports  # 끝난 세션에 더 보고하지 않는다
    boot.stop.set()
    t.join(timeout=30)
    assert box["rc"] == 0 and len(ec2.registers) == 1

    # 공급자가 컨테이너를 다시 띄워도(같은 컨테이너 디스크) 다시 등록해 진짜 세션을 밀어내지 않는다
    again = _boot(gb, world)
    t2, box2 = _run_bg(again)
    time.sleep(0.5)
    again.stop.set()
    t2.join(timeout=10)
    assert box2["rc"] == 0 and len(ec2.registers) == 1


def test_no_start_config_reports_failed_and_waits_for_manual_path(gb, world):
    world["ec2"].config = False
    boot = _boot(gb, world)
    t, box = _run_bg(boot)
    assert _wait(lambda: any(r["stage"] == "FAILED" for r in world["ec2"].reports))
    failed = next(r for r in world["ec2"].reports if r["stage"] == "FAILED")
    assert failed["reason_code"] == "NO_START_CONFIG" and "손 경로" in failed["detail"]
    assert not (world["fake"] / "lobby.json").exists()
    boot.stop.set()
    t.join(timeout=30)
    assert box["rc"] == 0 and world["ec2"].reports[-1]["stage"] == "ENDED" and not world["ec2"].errors


@pytest.mark.parametrize(("change", "reason"), [
    ({"SIMAI_TIER0_TLS_PRIVATE_KEY": None}, "TLS_KEY_MISSING"),
    ({"SIMAI_TIER0_PINNED_SPKI_SHA256": "sha256:" + "0" * 64}, "TLS_PIN_MISMATCH"),
    ({"FAKE_PREFLIGHT_RC": "3"}, "GPU_TOO_SMALL"),
])
def test_failures_register_then_report_failed_reason(gb, world, change, reason):
    for k, v in change.items():
        if v is None:
            world["env"].pop(k)
        else:
            world["env"][k] = v
    boot = _boot(gb, world)
    t, box = _run_bg(boot)
    assert _wait(lambda: any(r["stage"] == "FAILED" for r in world["ec2"].reports))
    assert next(r for r in world["ec2"].reports if r["stage"] == "FAILED")["reason_code"] == reason
    assert len(world["ec2"].registers) == 1
    boot.stop.set()
    t.join(timeout=30)
    assert box["rc"] == 0


def test_calibration_hash_mismatch_is_failed(gb, world):
    real = world["ec2"].result
    def tampered():
        doc = real()
        doc["payload"]["start"]["config"]["calibration"]["sha256"] = "sha256:" + "f" * 64
        return doc
    world["ec2"].result = tampered
    boot = _boot(gb, world)
    t, _ = _run_bg(boot)
    assert _wait(lambda: any(r["stage"] == "FAILED" for r in world["ec2"].reports))
    assert next(r for r in world["ec2"].reports if r["stage"] == "FAILED")["reason_code"] == "CALIBRATION_HASH_MISMATCH"
    assert not (world["tmp"] / "srv" / ".simai-private" / "gpu-session" / "lobby-bundle" / "calibration.json").exists()
    boot.stop.set()
    t.join(timeout=30)


def test_missing_boot_id_is_usage_error_without_registering(gb, world):
    world["env"].pop("SIMAI_GPU_BOOT_ID")
    assert _boot(gb, world).run() == gb.EXIT_USAGE and not world["ec2"].registers


def test_lobby_exit_maps_to_failed(gb, world):
    world["env"].update({"FAKE_LOBBY_S": "0.5", "FAKE_LOBBY_RC": "5", "SIMAI_BOOT_SERVICES": "tier0"})
    boot = _boot(gb, world)
    t, _ = _run_bg(boot)
    assert _wait(lambda: any(r["stage"] == "FAILED" for r in world["ec2"].reports))
    assert next(r for r in world["ec2"].reports if r["stage"] == "FAILED")["reason_code"] in ("TIER0_EXITED", "ENGINE_BUILD_FAILED")
    assert json.loads((world["fake"] / "lobby.json").read_text())["argv"][-2:] == ["--services", "tier0"]
    boot.stop.set()
    t.join(timeout=30)


@pytest.mark.parametrize("mb, services, note", [
    (81559.0, "BOOT", None),          # H100 80GB: 자동 부팅 기본(lobby 기본 + ppe, -718)
    (97871.0, "BOOT", None),          # 96GB급(RTX PRO 6000 등)도 같은 기본
    (70000.0, "BOOT", None),          # PPE 하한(PPE_MIN_VRAM_MB) 그대로
    (69999.0, None, "ppe 꺼짐: VRAM 부족"),  # 하한 바로 아래: lobby 기본(ppe 없음)
    (46068.0, None, "ppe 꺼짐: VRAM 부족"),  # L40S·A40 48GB급: lobby 기본(여유 2 GiB 남짓이라 ppe 끔, VLM 몫은 19.1 미결)
    (44999.0, "tier0", "24GB급"),      # 48GB급 미만: Tier 0 관제만
    (23028.0, "tier0", "24GB급"),      # A10 24GB
    (20470.0, "tier0", "최소 사양"),    # 24GB급 미만: tier0 + 경고
    (None, None, None),               # 모르면 lobby 기본(preflight 가 멈춘다)
])
def test_boot_services_by_vram(gb, mb, services, note):
    """S15P31A101-705: SIMAI_BOOT_SERVICES 가 비면 VRAM 으로 lobby 서비스 기본값을 정한다"""
    got, why = gb.boot_services({}, {"memory_total_mb": mb}, 45_000, 22_500)
    assert got == (gb.BOOT_SERVICES if services == "BOOT" else services)
    assert (why is None) if note is None else (note in why)


def test_boot_services_explicit_value_wins(gb):
    assert gb.boot_services({"SIMAI_BOOT_SERVICES": " tier0,vllm "}, {"memory_total_mb": 23028.0}, 45_000, 22_500) == ("tier0,vllm", None)
    assert gb.boot_services({"SIMAI_BOOT_SERVICES": "tier0"}, {"memory_total_mb": 81559.0}, 45_000, 22_500) == ("tier0", None)


@pytest.mark.parametrize("mb,explicit,expected", [
    (81559, None, "tier0,worker,avatar,ppe"),
    (46068, None, "tier0,worker,avatar"),
    (23028, None, "tier0"),
    (81559, "tier0,vllm", "tier0"),
])
def test_disabled_vllm_respects_selected_services(gb, mb, explicit, expected):
    env = {"SIMAI_BOOT_DISABLE": "vllm"}
    if explicit:
        env["SIMAI_BOOT_SERVICES"] = explicit
    assert gb.boot_services(env, {"memory_total_mb": mb}, 45000, 22500)[0] == expected
    assert gb.service_findings(expected.split(","), True, {"vllm"})["svc_vllm"]["level"] == "info"


def test_boot_default_is_lobby_default_plus_ppe_within_gpu_budget(gb):
    """S15P31A101-718: 자동 부팅은 안전모·조끼 감시(ppe)를 기본으로 띄운다. 손 lobby 기본(-507 명시 선택)은 그대로 두고,
    PPE 몫(기본 0.05)을 더해도 Tier 0 0.20 + 분석 0.70 + PPE ≤ 0.95 라 preflight 예산 점검을 통과한다(비율이라 80·96GB 같음)"""
    bs = load_script("simai_bootstrap")
    assert gb.BOOT_SERVICES.split(",") == [*bs.LOBBY_SERVICES.split(","), "ppe"]
    assert "ppe" not in bs.LOBBY_SERVICES.split(",")
    fr = bs.compute_gpu_fractions(bs.parse_services(gb.BOOT_SERVICES), {})
    assert fr["ppe_fraction"] == bs.PPE_FRACTION_DEFAULT == 0.05 and fr["avatar_fraction"] == 0.10
    assert fr["worker_fraction"] >= bs.MIN_WORKER_TORCH_FRACTION  # 분석 워커 몫은 PPE 때문에 줄지 않는다(별도 몫)


def test_small_gpu_runs_tier0_only_and_reports_the_reduction(gb, world):
    """24GB급 GPU: lobby 를 tier0 로 띄우고(vLLM 을 기다리지 않음) 단계 보고 설명에 축소 사실을 남긴다"""
    (world["fake"] / "smi.py").write_text(FAKE_SMI.replace("NVIDIA H100 80GB HBM3, 9.0, 81559", "NVIDIA A10, 8.6, 23028"),
                                          encoding="utf-8")
    boot = _boot(gb, world)
    t, _ = _run_bg(boot)
    ec2 = world["ec2"]
    assert _wait(lambda: any(r["stage"] == "READY" for r in ec2.reports)), ec2.reports
    boot.stop.set()
    t.join(timeout=30)
    assert json.loads((world["fake"] / "lobby.json").read_text())["argv"][-2:] == ["--services", "tier0"]
    assert json.loads((world["fake"] / "preflight.json").read_text())["argv"][-2:] == ["--services", "tier0"]
    assert any("Tier 0 관제만" in (r.get("detail") or "") for r in ec2.reports if r["stage"] == "WEIGHTS")
    ready = next(r for r in ec2.reports if r["stage"] == "READY")
    assert "Tier 0 관제만" in ready["detail"]
    assert "vLLM 미준비" not in ready["detail"]  # vLLM 을 띄우지 않으니 기다리지도 않는다
    assert "관제만" not in (ec2.reports[-1].get("detail") or "")  # ENDED 에는 붙이지 않는다


# ---------------------------------------------------------------- 캐시


def test_cache_store_then_hit_and_never_overwrites(gb, world, tmp_path):
    ec2 = world["ec2"]
    cache = gb.GpuCache(gb.Http(allow_http=True), ec2.url, WORKER_TOKEN, tmp_path / "t")
    assert cache.fetch("trt-engine", "h100/sm_90/abc", tmp_path / "e") == "miss"
    src = tmp_path / "engines"
    (src / "m" / "sm_90" / "trt10").mkdir(parents=True)
    (src / "m" / "sm_90" / "trt10" / "a.engine").write_bytes(b"engine")
    (src / "m" / "sm_90" / "trt10" / "a.json").write_text("{}")
    members = ["m/sm_90/trt10/a.engine", "m/sm_90/trt10/a.json"]
    assert cache.store("trt-engine", "h100/sm_90/abc", src, members) == "stored"
    assert ec2.headers_seen[-1]["if-none-match"] == "*"  # 서명에 묶인 헤더를 그대로 싣는다
    assert cache.store("trt-engine", "h100/sm_90/abc", src, members) == "exists"  # 먼저 올린 것이 이긴다
    dest = tmp_path / "restore"
    assert cache.fetch("trt-engine", "h100/sm_90/abc", dest) == "hit"
    assert (dest / "m" / "sm_90" / "trt10" / "a.engine").read_bytes() == b"engine"
    # 받은 내용이 기록된 해시와 다르면 쓰지 않는다
    key = "job-objects/gpu_cache/trt-engine/h100/sm_90/abc.tar"
    good = ec2.objects[key]
    ec2.objects[key] = good[:-1] + b"\x01"
    real_sign = cache._sign
    cache._sign = lambda *a, **k: {**real_sign(*a, **k), "sha256": hashlib.sha256(good).hexdigest()}
    assert cache.fetch("trt-engine", "h100/sm_90/abc", tmp_path / "bad") == "error"


def test_cache_put_403_is_denied_noted_and_boot_goes_on(gb, world, tmp_path):
    """S15P31A101-710: 서명은 받았는데 저장소가 쓰기를 403 으로 거부하면 「캐시 경로 쓰기 거부(S3 403)」를 보고 설명에 남기고 캐시 없이 진행한다."""
    ec2 = world["ec2"]
    ec2.put_status = 403
    notes: list[str] = []
    cache = gb.GpuCache(gb.Http(allow_http=True), ec2.url, WORKER_TOKEN, tmp_path / "t", notice=notes.append)
    src = tmp_path / "reid"
    (src / "cope").mkdir(parents=True)
    (src / "cope" / "a.yml").write_text("x")
    assert cache.store("reid", "cope-1", src, ["cope"]) == "denied"
    assert notes == ["캐시 경로 쓰기 거부(S3 403)"]
    assert "job-objects/gpu_cache/reid/cope-1.tar" not in ec2.objects
    assert not list((tmp_path / "t").iterdir())  # 묶음 tar 는 지운다

    rep = gb.Reporter(gb.Http(allow_http=True), ec2.url, "gpu-boot-runpod", SECRET, "s-1", 3600)
    rep.set("READY", detail="관제 가능")
    rep.add_note(gb.GpuCache.DENIED_NOTE)
    rep.add_note(gb.GpuCache.DENIED_NOTE)  # 같은 말은 한 번만
    assert rep.payload()["detail"] == "관제 가능 · 캐시 경로 쓰기 거부(S3 403)"
    rep.set("READY", detail="관제 가능")  # 다음 단계 설명에도 붙는다
    assert rep.state["detail"] == "관제 가능 · 캐시 경로 쓰기 거부(S3 403)" and rep.state["stage"] == "READY"


def test_safe_extract_rejects_escaping_paths(gb, tmp_path):
    evil = tmp_path / "evil.tar"
    evil.write_bytes(_tar_bytes({"../escape.txt": b"x"}))
    with pytest.raises(tarfile.TarError):
        gb.safe_extract(evil, tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()


def test_cache_keys_separate_gpu_kinds_and_need_known_versions(gb):
    v = {"tensorrt": "10.13.3.9", "ultralytics": "8.4.1", "torch": "2.11.0", "modelopt": "0.37", "vllm": "0.21.0", "triton": "3.5"}
    h100 = {"name": "NVIDIA H100 80GB HBM3", "arch": "sm_90"}
    h200 = {"name": "NVIDIA H200", "arch": "sm_90"}
    k1 = gb.trt_cache_key(h100, "a" * 64, {}, v)
    assert k1.startswith("nvidia-h100-80gb-hbm3/sm_90/") and re.fullmatch(r"[a-z0-9-]+/sm_90/[0-9a-f]{16}", k1)
    assert gb.trt_cache_key(h200, "a" * 64, {}, v) != k1  # 같은 sm 이라도 GPU 종류가 다르면 다른 엔진
    assert gb.trt_cache_key(h100, "b" * 64, {}, v) != k1
    assert gb.trt_cache_key(h100, "a" * 64, {"SIMAI_TIER0_IMGSZ": "960"}, v) != k1
    assert gb.trt_cache_key(h100, "a" * 64, {}, {**v, "tensorrt": "10.14"}) != k1
    assert gb.trt_cache_key(h100, "a" * 64, {}, {**v, "tensorrt": None}) is None  # 모르면 캐시를 쓰지 않는다
    k2 = gb.vllm_cache_key(h100, ["c" * 64], {}, v)
    assert k2 and gb.vllm_cache_key(h100, ["c" * 64], {"SIMAI_VLLM_QUANTIZATION": "none"}, v) != k2
    assert gb.vllm_cache_key(h100, ["c" * 64], {}, {**v, "vllm": None}) is None
    # 서버 키 형식(GpuCacheService.CACHE_KEY)과 맞는다
    server = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}(/[a-z0-9][a-z0-9._-]{0,63}){0,5}$")
    assert all(server.match(k) for k in (k1, k2, gb.reid_cache_key({})))


def test_reid_hashes_match_tier0_embedder(gb):
    from simai_ai.reid import embedder

    assert gb.COPE_WEIGHTS_SHA256 == embedder.DEFAULT_COPE_WEIGHTS_SHA256
    assert gb.CLIP_SHA256 == embedder.DEFAULT_CLIP_VIT_B_16_SHA256


# ---------------------------------------------------------------- 단계·비밀 값


def test_lobby_stage_follows_marker_and_vllm(gb):
    t0 = 1_000
    running = {"gpu_priority": {"phase": "running", "reserve": {"state": "OK"}, "written_at": 2_000}}
    assert gb.lobby_stage(None, t0, True, False, False)[0] == "ENGINE"
    stale = {"gpu_priority": {**running["gpu_priority"], "written_at": 500}}  # 이전 실행이 남긴 표시 파일
    assert gb.lobby_stage(stale, t0, True, False, False)[0] == "ENGINE"
    starting = {"gpu_priority": {"phase": "starting", "written_at": 2_000}}
    assert gb.lobby_stage(starting, t0, True, False, False)[0] == "TIER0"
    assert gb.lobby_stage(running, t0, True, False, False)[0] == "VLLM"
    assert gb.lobby_stage(running, t0, True, True, False) == ("READY", None)
    assert gb.lobby_stage(running, t0, False, False, False) == ("READY", None)
    assert gb.lobby_stage(running, t0, True, False, True)[0] == "READY"


RAW_REID = "Re-ID 꺼짐: FileNotFoundError: COPE 외부 저장소 경로를 찾을 수 없습니다: /srv/simai/reid/cope"
RAW_CALIB = "live_calibration.enabled=true(설정 파일)인데 SIMAI_TIER0_HUMAN_CALIBRATION=0 이라 자동 보정이 돌지 않음"


def _no_raw(items: dict) -> None:
    """쉬운 말(text)에는 예외 이름·경로·환경 변수 원문이 없다. 원문은 detail 에만 있다."""
    for item in items.values():
        for raw in ("FileNotFoundError", "/srv/simai", "live_calibration.enabled=true"):
            assert raw not in item["text"], item


def test_cache_findings_names_reid_miss_and_cache_trouble(gb):
    """Re-ID 묶음 캐시 miss 가 보고에 올라간다(18차: 로그에만 남아 3세션 동안 몰랐다). 엔진·vLLM 캐시 miss 는 정보, 받기 오류는 경고."""
    got = gb.cache_findings({"reid": "miss", "trt-engine": "miss", "vllm-compile": "error"}, reid_is_ready=False)
    assert got["reid"]["level"] == "warning" and "seed-reid" in got["reid"]["text"] and "새 번호" in got["reid"]["text"]
    assert got["cache_trt"]["level"] == "info" and "엔진을 새로 만들었어요" in got["cache_trt"]["text"]
    assert got["cache_vllm"]["level"] == "warning" and "받지 못해" in got["cache_vllm"]["text"]
    assert "reid" not in gb.cache_findings({"reid": "miss"}, reid_is_ready=True)  # 손으로 둔 묶음이 있으면 꺼짐 아님
    assert "error" in gb.cache_findings({"reid": "error"}, False)["reid"]["detail"]
    assert gb.cache_findings({"trt-engine": "hit", "vllm-compile": "hit"}, True) == {}


def test_service_findings_lists_services_not_started(gb):
    assert gb.service_findings(["tier0", "vllm", "worker", "avatar", "ppe"], True) == {}
    got = gb.service_findings(["tier0", "vllm", "worker", "avatar"], True)
    assert list(got) == ["svc_ppe"] and "보호구(PPE)" in got["svc_ppe"]["text"] and "관리자" in got["svc_ppe"]["text"]
    got = gb.service_findings(["tier0"], False)
    assert set(got) == {"svc_ppe", "svc_avatar", "svc_worker", "svc_vllm"}
    assert all(v["level"] == "warning" and len(v["text"]) <= 200 for v in got.values())
    assert "svc_vllm" in gb.service_findings(["tier0", "vllm", "worker"], False)  # VRAM 부족이면 서비스 목록에 있어도 꺼짐


def test_tier0_findings_reads_only_what_the_status_says(gb):
    assert gb.tier0_findings(None) == {} and gb.tier0_findings({"gpu_priority": {}}) == {}  # 상태 칸이 없으면 지어내지 않는다
    doc = {"status": {"disabled_features": [RAW_REID], "info_notes": [RAW_CALIB],
                      "reid": {"state": "disabled"}, "process_split": {"disabled": ["reid", "clean_background"]}}}
    got = gb.tier0_findings(doc)
    _no_raw(got)
    assert got["reid"]["level"] == "warning" and got["reid"]["detail"] == RAW_REID
    assert "seed-reid" in got["reid"]["text"]  # 묶음이 이 서버에 없다: 할 일은 seed-reid
    assert got["cfg_live_calibration"]["level"] == "info" and got["cfg_live_calibration"]["detail"] == RAW_CALIB  # 의도된 설정: 정보
    assert got["process_split"]["level"] == "warning" and "reid, clean_background" in got["process_split"]["detail"]
    assert gb.tier0_findings({"status": {"disabled_features": [], "process_split": None}}) == {}


def test_tier0_findings_reid_text_follows_where_the_bundle_is(gb):
    doc = {"status": {"disabled_features": [RAW_REID], "reid": {"state": "disabled"}}}
    registered = gb.tier0_findings(doc, reid_on_disk=True)["reid"]["text"]  # 부팅 뒤 seed-reid 로 묶음이 올라온 경우
    assert "다음 부팅부터 자동으로 켜집니다(Re-ID 묶음 등록됨)" in registered and "restart-tier0" in registered
    other = gb.tier0_findings({"status": {"disabled_features": ["Re-ID 꺼짐: CUDA 없음"], "reid": {"state": "disabled"}}}, True)
    assert "묶음은 있는데 켜지지 않았어요" in other["reid"]["text"]
    # Tier 0 가 지금 켜져 있다고 하면(낡은 표시 방지) 항목이 없다
    assert "reid" not in gb.tier0_findings({"status": {"disabled_features": [RAW_REID], "reid": {"state": "active"}}}, True)
    raw = "process_split=false(설정 파일)를 SIMAI_TIER0_PROCESS_SPLIT=1 가 덮어씀"
    over = gb.tier0_findings({"status": {"disabled_features": [raw]}})
    assert over["tier0_0"]["level"] == "warning" and "덮여" in over["tier0_0"]["text"] and "PROCESS_SPLIT" not in over["tier0_0"]["text"]


def test_compose_disabled_recomputes_so_resolved_reid_drops_out(gb):
    """READY 뒤 주기 재계산(-756): 부팅 때 Re-ID 묶음 없음이었어도 Tier 0 reid.state=active 로 바뀌면 항목이 빠지고, 설정 정보는 남는다."""
    base = dict(services={}, cache_results={"reid": "miss", "trt-engine": "hit"}, reid_on_disk=False,
                vlm_warmup_failure=None, vlm_timed_out=False, include_vllm_cache=True)
    off = {"status": {"disabled_features": [RAW_REID], "info_notes": [RAW_CALIB], "reid": {"state": "disabled"}}}
    first = gb.compose_disabled(marker_doc=off, **base)
    assert set(first) == {"reid", "cfg_live_calibration"}
    on = {"status": {"disabled_features": [], "info_notes": [RAW_CALIB], "reid": {"state": "active"}}}
    again = gb.compose_disabled(**{**base, "reid_on_disk": True, "marker_doc": on})
    assert set(again) == {"cfg_live_calibration"}  # 해소: Re-ID 가 빠진다(캐시 miss 보고도 낡은 값이라 지운다)
    # 표시 파일에 status 가 아직 없으면 부팅 때 사실(묶음 없음)만 남긴다
    assert set(gb.compose_disabled(marker_doc=None, **base)) == {"reid"}
    more = {**base, "vlm_warmup_failure": "VLM 웜업 실패(TimeoutError)", "vlm_timed_out": True,
            "services": gb.service_findings(["tier0"], True)}
    with_vlm = gb.compose_disabled(marker_doc=on, **more)
    assert {"vlm_warmup", "vlm_timeout", "svc_ppe", "svc_worker"} <= set(with_vlm)
    _no_raw(with_vlm)


def test_reporter_sends_disabled_notices_only_once_known_and_matches_contract(gb):
    rep = gb.Reporter(gb.Http(allow_http=True), "http://127.0.0.1:1", "gpu-boot-runpod", SECRET, SESSION, 3600)
    rep.set("WEIGHTS", detail="x")
    assert "disabled_features" not in rep.payload() and "disabled_notices" not in rep.payload()  # 아직 모름: 칸을 싣지 않는다
    rep.set_disabled("start")
    assert rep.payload()["disabled_features"] == [] and rep.payload()["disabled_notices"] == []  # 확인했고 꺼진 것 없음
    rep.set_disabled("reid", "Re-ID 꺼짐 — 묶음 없음(seed-reid 필요)")
    rep.set_disabled("reid", "Re-ID 꺼짐 — Tier 0 가 이유를 알림", detail=RAW_REID)  # 같은 기능은 덮어쓴다
    rep.set_disabled("cfg_live_calibration", "사람 관절 보정은 기본 꺼짐", level="info", detail=RAW_CALIB)
    payload = rep.payload()
    assert payload["disabled_features"] == ["Re-ID 꺼짐 — Tier 0 가 이유를 알림"]  # 1.2.0 칸은 경고 문구만
    assert payload["disabled_notices"] == [
        {"key": "reid", "level": "warning", "text": "Re-ID 꺼짐 — Tier 0 가 이유를 알림", "detail": RAW_REID},
        {"key": "cfg_live_calibration", "level": "info", "text": "사람 관절 보정은 기본 꺼짐", "detail": RAW_CALIB}]
    doc = gb.envelope("gpu_session_report", "gpu-boot-runpod", payload)
    assert doc["schema_version"] == "1.3.0"
    errors = list(_validator("gpu_session_report").iter_errors(doc))
    assert not errors, errors
    assert gb.envelope("gpu_session_register", "x", {})["schema_version"] == "1.0.0"  # 다른 메시지 판은 그대로


def test_reporter_replace_disabled_drops_resolved_items_and_wakes(gb):
    rep = gb.Reporter(gb.Http(allow_http=True), "http://127.0.0.1:1", "gpu-boot-runpod", SECRET, SESSION, 3600)
    rep.replace_disabled({"reid": gb.notice("Re-ID 꺼짐"), "svc_ppe": gb.notice("PPE 꺼짐")})
    rep._wake.clear()
    rep.replace_disabled({"reid": gb.notice("Re-ID 꺼짐"), "svc_ppe": gb.notice("PPE 꺼짐")})
    assert not rep._wake.is_set()  # 같은 목록이면 다시 보내지 않는다
    rep.replace_disabled({"svc_ppe": gb.notice("PPE 꺼짐")})  # Re-ID 가 해소됨
    assert rep._wake.is_set()
    assert [n["key"] for n in rep.payload()["disabled_notices"]] == ["svc_ppe"]
    rep.replace_disabled({})
    assert rep.payload()["disabled_notices"] == [] and rep.payload()["disabled_features"] == []  # 꺼진 것 없음 확인(칸은 계속 싣는다)


def test_dummy_png_is_a_valid_small_png(gb):
    import struct
    import zlib

    png = gb.dummy_png(8, 4)
    assert png[1:4] == b"PNG" and png[:1] == bytes([0x89])
    assert struct.unpack(">II", png[16:24]) == (8, 4)
    idat = png.index(b"IDAT")
    size = struct.unpack(">I", png[idat - 4:idat])[0]
    assert len(zlib.decompress(png[idat + 4:idat + 4 + size])) == 4 * (1 + 8 * 3)
    assert len(gb.dummy_png()) < 20_000  # 작다


def test_warmup_vlm_sends_the_real_request_shape_and_never_raises(gb):
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "그라데이션"}}]}).encode()

    def opener(req, timeout):
        seen["url"], seen["body"], seen["timeout"] = req.full_url, json.loads(req.data), timeout
        return Resp()

    ticks = iter([10.0, 31.5])
    ok, took, why = gb.warmup_vlm({"SIMAI_VLLM_PORT": "8001"}, opener=opener, clock=lambda: next(ticks))
    assert ok and took == pytest.approx(21.5) and why == "응답 받음"
    assert seen["url"] == "http://127.0.0.1:8001/v1/chat/completions" and seen["timeout"] == gb.WARMUP_TIMEOUT_S
    body = seen["body"]
    assert body["model"] == "cosmos3-nano" and body["repetition_penalty"] == 1.05  # vlm/client.py 의 요청과 같은 모양
    parts = body["messages"][0]["content"]
    assert [p["type"] for p in parts] == ["text", "image_url"] and parts[1]["image_url"]["url"].startswith("data:image/png;base64,")

    def boom(req, timeout):
        raise OSError("refused")

    ticks = iter([1.0, 1.2])
    ok, took, why = gb.warmup_vlm({}, opener=boom, clock=lambda: next(ticks))
    assert not ok and why == "OSError" and took == pytest.approx(0.2)


def test_redact_hides_secrets_and_signed_query(gb):
    gb._secrets.add("very-secret-value")
    text = gb.redact("token=very-secret-value url=https://b.s3.amazonaws.com/k.tar?X-Amz-Signature=abc hf_" + "a" * 25)
    assert "very-secret-value" not in text and "X-Amz-Signature" not in text and "hf_aaaa" not in text


def test_bootstrap_auto_hands_over_only_with_full_boot_env(monkeypatch):
    bs = load_script("simai_bootstrap")
    calls = []
    monkeypatch.setattr(bs, "execvp", lambda argv: calls.append((argv, "child_env")))  # auto 는 이 길로 가면 안 된다(-707)
    monkeypatch.setattr(bs, "exec_gpu_boot", lambda argv: calls.append((argv, bs.gpu_boot_env().get("SIMAI_GPU_BOOT_SECRET"))))
    for k in ("SIMAI_GPU_BOOT_SECRET", "SIMAI_GPU_BOOT_ID", "SIMAI_EC2_BASE_URL", "SIMAI_GPU_BOOT_CMD"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SIMAI_GPU_BOOT_SECRET", SECRET)
    assert bs.main(["auto"]) == bs.EXIT_USAGE and not calls  # ID·EC2 주소가 없으면 시작하지 않는다
    assert "SIMAI_GPU_BOOT_SECRET" not in os.environ  # 그 경우 비밀 값은 환경에서 지운다
    monkeypatch.setenv("SIMAI_GPU_BOOT_SECRET", SECRET)
    monkeypatch.setenv("SIMAI_GPU_BOOT_ID", "gpu-boot-runpod")
    monkeypatch.setenv("SIMAI_EC2_BASE_URL", "https://simai.example")
    assert bs.main(["auto"]) == 0
    assert calls == [(["simai-gpu-boot", "auto"], SECRET)]  # 비밀 값을 꺼내기 전에 넘긴다(simai-gpu-boot 가 꺼낸다)
    # 부팅 비밀값이 없으면 지금까지의 tier0 와 같다(SIMAI_SERVICES 오류로 같은 64)
    for k in ("SIMAI_GPU_BOOT_SECRET", "SIMAI_GPU_BOOT_ID"):
        monkeypatch.delenv(k)
    monkeypatch.setenv("SIMAI_SERVICES", "bogus")
    assert bs.main(["auto"]) == bs.main(["tier0"]) == bs.EXIT_USAGE


def test_bootstrap_drops_boot_secrets_from_children(monkeypatch):
    bs = load_script("simai_bootstrap")
    monkeypatch.setenv("SIMAI_GPU_BOOT_SECRET", SECRET)
    monkeypatch.setenv("SIMAI_TIER0_TLS_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----x")
    assert "SIMAI_GPU_BOOT_SECRET" not in bs.child_env()
    bs.take_token()
    assert "SIMAI_GPU_BOOT_SECRET" not in os.environ and "SIMAI_TIER0_TLS_PRIVATE_KEY" not in os.environ
    assert SECRET not in bs._redact(f"x {SECRET} y")


# ---------------------------------------------------------------- AWS(S15P31A101-703): Secrets Manager 로 비밀 값 받기


class FakeAws:
    """가짜 IMDSv2 + Secrets Manager. 받은 Secrets Manager 요청 머리글을 남긴다."""

    def __init__(self, secret: dict | None, sm_status: int = 200, sm_type: str | None = None) -> None:
        self.secret, self.sm_status, self.sm_type = secret, sm_status, sm_type
        self.sm_requests: list[dict] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code: int, data: bytes = b"") -> None:
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_PUT(self):
                if self.path == "/latest/api/token" and self.headers.get("X-aws-ec2-metadata-token-ttl-seconds"):
                    return self._send(200, b"imds-token")
                return self._send(404)

            def do_GET(self):
                if self.headers.get("X-aws-ec2-metadata-token") != "imds-token":
                    return self._send(401)
                if self.path == "/latest/meta-data/iam/security-credentials/":
                    return self._send(200, b"simai-gpu-instance")
                if self.path == "/latest/meta-data/iam/security-credentials/simai-gpu-instance":
                    return self._send(200, json.dumps({"AccessKeyId": "ASIAEXAMPLE", "SecretAccessKey": "role-secret-key",
                                                       "Token": "role-session-token"}).encode())
                if self.path == "/latest/meta-data/placement/region":
                    return self._send(200, b"ap-northeast-2")
                return self._send(404)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                outer.sm_requests.append({"headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                if outer.sm_status != 200:
                    return self._send(outer.sm_status, json.dumps({"__type": outer.sm_type or "Error"}).encode())
                return self._send(200, json.dumps({"Name": body.get("SecretId"), "SecretString": json.dumps(outer.secret)}).encode())

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()


def _aws_env(world, aws: FakeAws) -> dict:
    env = {k: v for k, v in world["env"].items() if not k.startswith("RUNPOD_")}
    for k in ("SIMAI_GPU_BOOT_SECRET", "SIMAI_TIER0_TLS_PRIVATE_KEY", "HF_TOKEN"):
        env.pop(k)
    env.update({"SIMAI_SECRETS_SOURCE": "aws-secretsmanager", "SIMAI_AWS_SECRET_ID": "simai/gpu-boot",
                "SIMAI_AWS_IMDS_URL": aws.url, "SIMAI_AWS_SECRETS_ENDPOINT": aws.url, "SIMAI_GPU_PROVIDER": "AWS",
                "SIMAI_INSTANCE_ID": "i-0123456789abcdef0", "SIMAI_REGION": "ap-northeast-2a", "SIMAI_PUBLIC_HOST": "198.51.100.7",
                "SIMAI_PUBLIC_PORT_17000": "17000", "SIMAI_PUBLIC_PORT_17001": "17001", "SIMAI_AWS_SECRETS_WAIT_S": "0.2"})
    return env


def test_sigv4_matches_aws_documented_example(gb):
    # AWS 문서 "Signature Version 4 signing process" 예시(IAM ListUsers, 2015-08-30)와 같은 서명이어야 한다
    h = gb.sigv4_headers("GET", "iam.amazonaws.com", "/", "Action=ListUsers&Version=2010-05-08",
                         {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"}, b"",
                         access_key="AKIDEXAMPLE", secret_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", region="us-east-1",
                         service="iam", amz_date="20150830T123600Z")
    assert h["Authorization"] == ("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/iam/aws4_request, "
                                  "SignedHeaders=content-type;host;x-amz-date, "
                                  "Signature=5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7")


def test_aws_secrets_manager_fills_boot_secrets_from_instance_role_and_registers(gb, world):
    # 인증서는 공개 값이라 환경(world 의 base64 한 줄)에 있으면 환경이 먼저다. 비밀 셋은 환경에 없고 Secrets Manager 에서만 온다
    aws = FakeAws({"SIMAI_GPU_BOOT_SECRET": SECRET, "SIMAI_TIER0_TLS_PRIVATE_KEY": world["key"],
                   "HF_TOKEN": "hf_" + "y" * 30, "SIMAI_TIER0_TLS_CERT_PEM": "-----BEGIN CERTIFICATE-----\nignored\n", "UNRELATED": "x"})
    try:
        env = _aws_env(world, aws)
        boot = _boot(gb, {**world, "env": env})
        t, box = _run_bg(boot)
        assert _wait(lambda: world["ec2"].registers), world["ec2"].errors
        boot.stop.set()
        t.join(timeout=30)
    finally:
        aws.close()
    p = world["ec2"].registers[0]["payload"]  # 가짜 EC2 는 Bearer 부팅 비밀값이 맞을 때만 받는다
    assert p["provider"] == "AWS" and p["instance_id"] == "i-0123456789abcdef0" and p["region"] == "ap-northeast-2a"
    assert p["public_host"] == "198.51.100.7" and p["tls_public_key_sha256"] == "sha256:" + world["spki"]
    assert {(x["role"], x["public_port"]) for x in p["ports"]} == {("CONTROL", 17000), ("VIDEO", 17001)}
    req = aws.sm_requests[0]
    assert req["body"] == {"SecretId": "simai/gpu-boot"}
    assert req["headers"]["x-amz-target"] == "secretsmanager.GetSecretValue"
    assert req["headers"]["x-amz-security-token"] == "role-session-token"
    assert re.match(r"AWS4-HMAC-SHA256 Credential=ASIAEXAMPLE/\d{8}/ap-northeast-2/secretsmanager/aws4_request, "
                    r"SignedHeaders=content-type;host;x-amz-date;x-amz-security-token;x-amz-target, Signature=[0-9a-f]{64}$",
                    req["headers"]["authorization"])
    assert boot.secrets["HF_TOKEN"] == "hf_" + "y" * 30 and "UNRELATED" not in boot.secrets
    # 비밀 값·역할 자격은 로그 가림 대상이다
    assert "role-secret-key" not in gb.redact("role-secret-key") and "role-session-token" not in gb.redact("role-session-token")
    assert SECRET not in gb.redact(SECRET)


def test_aws_secrets_rejected_or_unavailable_does_not_register(gb, world):
    aws = FakeAws(None, sm_status=400, sm_type="com.amazonaws.secretsmanager#AccessDeniedException")
    try:
        boot = _boot(gb, {**world, "env": _aws_env(world, aws)})
        with pytest.raises(gb.BootError) as e:
            boot.load_aws_secrets()
        assert e.value.reason == "SECRETS_REJECTED" and "AccessDeniedException" in e.value.detail
        assert len(aws.sm_requests) == 1  # 거절은 다시 하지 않는다
        aws.sm_status, aws.sm_type = 503, "ServiceUnavailable"
        with pytest.raises(gb.BootError) as e:
            boot.load_aws_secrets()
        assert e.value.reason == "SECRETS_UNAVAILABLE"
    finally:
        aws.close()
    assert not world["ec2"].registers


def test_bootstrap_auto_hands_over_with_aws_secrets_source(monkeypatch):
    bs = load_script("simai_bootstrap")
    calls = []
    monkeypatch.setattr(bs, "execvp", lambda argv: calls.append(("child_env", argv)))  # auto 는 이 길로 가면 안 된다(-707)
    monkeypatch.setattr(bs, "exec_gpu_boot", lambda argv: calls.append(argv))
    for k in ("SIMAI_GPU_BOOT_SECRET", "SIMAI_GPU_BOOT_CMD", "SIMAI_GPU_BOOT_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SIMAI_SECRETS_SOURCE", "aws-secretsmanager")
    monkeypatch.setenv("SIMAI_EC2_BASE_URL", "https://simai.example")
    assert bs.main(["auto"]) == bs.EXIT_USAGE and not calls  # ID 가 없으면 시작하지 않는다
    monkeypatch.setenv("SIMAI_GPU_BOOT_ID", "gpu-boot-runpod")
    assert bs.main(["auto"]) == 0 and calls == [["simai-gpu-boot", "auto"]]


# ---- 호스트 CPU 측정(S15P31A101-740)


def test_cpu_model_and_quota_read_proc_and_cgroup_files(gb, tmp_path):
    info = tmp_path / "cpuinfo"
    info.write_text("processor\t: 0\nmodel name\t: Intel(R) Xeon(R)  Platinum 8460Y+\nmodel name\t: other\n", encoding="utf-8")
    assert gb.cpu_model(str(info)) == "Intel(R) Xeon(R) Platinum 8460Y+"
    assert gb.cpu_model(str(tmp_path / "none")) is None
    v2 = tmp_path / "cpu.max"
    v2.write_text("400000 100000\n", encoding="utf-8")
    assert gb.cpu_quota(str(v2)) == 4.0
    v2.write_text("max 100000\n", encoding="utf-8")  # 제한 없음 → v1 파일도 없으니 null
    assert gb.cpu_quota(str(v2), str(tmp_path / "q"), str(tmp_path / "p")) is None
    (tmp_path / "q").write_text("-1", encoding="utf-8")
    (tmp_path / "p").write_text("100000", encoding="utf-8")
    assert gb.cpu_quota(str(tmp_path / "missing"), str(tmp_path / "q"), str(tmp_path / "p")) is None
    (tmp_path / "q").write_text("250000", encoding="utf-8")
    assert gb.cpu_quota(str(tmp_path / "missing"), str(tmp_path / "q"), str(tmp_path / "p")) == 2.5


def test_single_thread_score_is_short_and_positive_and_fails_to_none(gb, monkeypatch):
    t0 = time.perf_counter()
    score = gb.single_thread_score(0.3)
    assert score is not None and score > 0 and time.perf_counter() - t0 < 2.0
    monkeypatch.setitem(sys.modules, "numpy", None)  # numpy 가 없으면 척도가 달라 점수를 내지 않는다
    assert gb.single_thread_score(0.3) is None


def test_host_cpu_is_reported_after_background_measure_and_never_blocks_boot(gb, world):
    ec2 = world["ec2"]
    gb.Boot.cpu_measure = staticmethod(lambda: {"model": "Test CPU", "logical_cpus": 8, "cpu_quota": None,
                                                 "single_thread_score": 1234.5, "measured_at": gb.now_ms()})
    boot = _boot(gb, world)
    t, box = _run_bg(boot)
    assert _wait(lambda: any("host_cpu" in r for r in ec2.reports)), ec2.reports
    assert _wait(lambda: any(r["stage"] == "READY" for r in ec2.reports))
    boot.stop.set()
    t.join(timeout=30)
    assert not ec2.errors, ec2.errors  # 계약(gpu_session_report 1.1.0) 검사 통과
    hc = next(r["host_cpu"] for r in ec2.reports if "host_cpu" in r)
    assert hc["model"] == "Test CPU" and hc["single_thread_score"] == 1234.5


def test_host_cpu_measure_failure_is_swallowed(gb, world):
    def boom():
        raise RuntimeError("no cpu")

    gb.Boot.cpu_measure = staticmethod(boom)
    boot = _boot(gb, world)
    t, box = _run_bg(boot)
    assert _wait(lambda: any(r["stage"] == "READY" for r in world["ec2"].reports))
    boot.stop.set()
    t.join(timeout=30)
    assert box["rc"] == 0 and not any("host_cpu" in r for r in world["ec2"].reports)


@pytest.mark.parametrize("score, warned", [(999.0, True), (1000.0, False), (None, False)])
def test_host_cpu_warning_does_not_block(gb, world, monkeypatch, capsys, score, warned):
    boot = _boot(gb, world)
    monkeypatch.setattr(boot, "cpu_measure", lambda: {"model": "CPU", "logical_cpus": 8, "cpu_quota": None,
                                                        "single_thread_score": score, "measured_at": gb.now_ms()})
    reporter = gb.Reporter(None, "https://example.invalid", "boot", "secret", "session", 10)
    boot.measure_cpu(reporter)
    assert reporter.host_cpu["single_thread_score"] == score
    assert ("경고: CPU 단일 스레드" in capsys.readouterr().err) is warned
