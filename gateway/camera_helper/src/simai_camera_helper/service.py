"""On-demand local preview consumer. No camera connections or image uploads."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .judge import CAMERAS, PAIRS, CoverageJudge, PairMetrics, Params
from .message import DEFAULT_STALE_MS, CameraFrame, build_message
from .offline import DEFAULT_WEIGHTS
from .progress import DEFAULT_STALL_SECONDS, Progress, bounded_setting, log_round, watchdog_loop
from .view_check import MultiViewChecker, ViewChecker, describe

LOG = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[4]

DEFAULT_DEMAND_PATH = "/api/devices/coverage-guides"
GUIDES_PATH = "/api/devices/coverage-guides"
PREVIEW_WS_URL = "ws://127.0.0.1:47032"
PREVIEW_ORIGIN = "http://localhost:5173"

DEFAULT_ALIKED_WEIGHTS = DEFAULT_WEIGHTS / "aliked-n16.pth"
DEFAULT_ALIKED_SHA256 = "5be8704840ed662d9d8c561bf7279c222092674e7eb05fd0feab94899e9d82f2"
DEFAULT_LIGHTGLUE_WEIGHTS = DEFAULT_WEIGHTS / "aliked_lightglue_v0-1_arxiv.pth"
DEFAULT_LIGHTGLUE_SHA256 = "d975e965b105311a6143194852297dff4f02aea5cc2e10cecfed966ca0e22503"


class RedactFilter(logging.Filter):
    """Ensure device tokens or secrets never appear in log records."""

    def __init__(self, *secrets: str):
        super().__init__()
        self._secrets = tuple(s for s in secrets if s)

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for secret in self._secrets:
            if secret in msg:
                msg = msg.replace(secret, "[REDACTED]")
        record.msg = msg
        record.args = ()
        return True


def load_env(path: Path) -> dict[str, str]:
    """Read root .env with the same format and SIMAI_* env precedence as relay."""
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    values.update({k: v for k, v in os.environ.items() if k.startswith("SIMAI_")})
    return values


def checked_weight(path: Path, expected: str) -> Path:
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected.lower()):
        raise ValueError("weight SHA-256 must be configured")
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    if h.hexdigest() != expected.lower():
        raise ValueError("weight SHA-256 mismatch")
    return path


def resolve_weights(env: dict[str, str]) -> tuple[Path, Path]:
    aliked_raw = env.get("SIMAI_CAMERA_HELPER_ALIKED_WEIGHTS", "").strip()
    aliked_path = Path(aliked_raw) if aliked_raw else DEFAULT_ALIKED_WEIGHTS
    aliked_sha = env.get("SIMAI_CAMERA_HELPER_ALIKED_SHA256", "").strip()
    if not aliked_sha and (not aliked_raw or aliked_path == DEFAULT_ALIKED_WEIGHTS):
        aliked_sha = DEFAULT_ALIKED_SHA256

    lightglue_raw = env.get("SIMAI_CAMERA_HELPER_LIGHTGLUE_WEIGHTS", "").strip()
    lightglue_path = Path(lightglue_raw) if lightglue_raw else DEFAULT_LIGHTGLUE_WEIGHTS
    lightglue_sha = env.get("SIMAI_CAMERA_HELPER_LIGHTGLUE_SHA256", "").strip()
    if not lightglue_sha and (not lightglue_raw or lightglue_path == DEFAULT_LIGHTGLUE_WEIGHTS):
        lightglue_sha = DEFAULT_LIGHTGLUE_SHA256

    return checked_weight(aliked_path, aliked_sha), checked_weight(lightglue_path, lightglue_sha)


def idle_message(source_id: str, now_ms: int) -> dict:
    """Build a valid coverage_guide 1.0.0 NO_VIDEO/UNKNOWN message for demand probing."""
    params = Params(image_width_px=1024, max_keypoints=2048, intrinsics_source="DEFAULT", ransac_threshold_px=3.0)
    judge = CoverageJudge(params)
    cameras = [CameraFrame(cid, None, has_video=False) for cid in CAMERAS]
    pairs = [PairMetrics(a, b) for a, b in PAIRS]
    return build_message(source_id, now_ms, cameras, pairs, judge, stage_a=False)


MAX_RESPONSE_BYTES = 256 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    def __init__(
        self,
        base: str,
        token: str,
        demand_path: str = DEFAULT_DEMAND_PATH,
        *,
        source_id: str = "laptop-01",
    ):
        p = urllib.parse.urlsplit(base)
        allowed_scheme = p.scheme == "https" or (
            p.scheme == "http" and p.hostname in ("127.0.0.1", "localhost", "::1")
        )
        if (
            not allowed_scheme
            or not p.hostname
            or p.username
            or p.password
            or p.query
            or p.fragment
            or not token
            or token in urllib.parse.unquote_plus(base)
        ):
            raise ValueError("invalid EC2 configuration")
        if (
            not demand_path.startswith("/")
            or demand_path.startswith("//")
            or "?" in demand_path
            or "#" in demand_path
        ):
            raise ValueError("invalid demand path")
        self.base = base.rstrip("/")
        self.token = token
        self.demand_path = demand_path
        self.source_id = source_id or "laptop-01"
        self.opener = urllib.request.build_opener(NoRedirect())
        # 재사용 시점 확인 기준(S15P31A101-712): 수요 응답의 reuse_reference(활성 수요일 때만 EC2 가 싣는다)
        # S15P31A101-714: 새 EC2 는 후보 맵 목록 reuse_references 도 싣는다(있으면 목록을 맵마다 비교해 1.2.0 으로 보낸다)
        self._reference_lock = threading.Lock()
        self._reference: dict | None = None
        self._references: list[dict] | None = None
        self.last_error_status: int | None = None
        """마지막 결과 전송이 받은 HTTP 오류 코드(성공하면 None). 옛 EC2 의 1.3.0 거부(400)를 알아본다(S15P31A101-761)"""

    def note_response(self, res) -> None:
        """수요가 실린 응답(GET 수요·POST 결과)이면 재사용 기준을 그 값으로 바꾼다(없으면 비운다)."""
        if isinstance(res, dict) and isinstance(res.get("active_demand"), bool):
            active = res["active_demand"]
            reference = res.get("reuse_reference")
            references = res.get("reuse_references")
            with self._reference_lock:
                self._reference = reference if active and isinstance(reference, dict) else None
                self._references = ([r for r in references if isinstance(r, dict)]
                                    if active and isinstance(references, list) else None)

    def reuse_reference(self) -> dict | None:
        with self._reference_lock:
            return self._reference

    def reuse_references(self) -> list[dict] | None:
        """후보 맵 목록(새 EC2). 옛 EC2(목록 키 없음)·수요 없음이면 None 이고, 그때는 reuse_reference 하나만 비교한다."""
        with self._reference_lock:
            return None if self._references is None else list(self._references)

    def __repr__(self) -> str:
        return f"Client(base={self.base!r}, demand_path={self.demand_path!r})"

    def request(self, path: str, body: dict | None = None) -> dict:
        data = None if body is None else json.dumps(body, separators=(",", ":"), allow_nan=False).encode("utf-8")
        headers = {
            "Authorization": "Bearer " + self.token,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        req = urllib.request.Request(self.base + path, data=data, headers=headers)
        with self.opener.open(req, timeout=3) as response:
            # 후보 맵 5개 × 카메라 4대의 서명 주소(S15P31A101-714)를 담도록 256 KiB 까지 받는다
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError("response too large")
            return json.loads(raw) if raw else {}

    def demand(self) -> bool:
        try:
            res = self.request(self.demand_path)
        except urllib.error.HTTPError as err:
            if err.code not in (404, 405):
                raise
            now_ms = time.time_ns() // 1_000_000
            res = self.request(GUIDES_PATH, idle_message(self.source_id, now_ms))
        self.note_response(res)
        return res.get("active_demand") is True

    def post(self, message: dict) -> dict:
        try:
            res = self.request(GUIDES_PATH, message)
        except urllib.error.HTTPError as err:
            self.last_error_status = err.code
            raise
        self.last_error_status = None
        self.note_response(res)
        return res


def retry_post(client, message: dict, active, sleep=time.sleep, attempts: int = 4, on_demand=None) -> bool:
    """Bounded retry; never retry an expired guide or after demand disappears."""
    for attempt in range(attempts):
        if not active() or time.time_ns() // 1_000_000 >= message["valid_until_ms"]:
            return False
        try:
            res = client.post(message)
            if on_demand is not None and isinstance(res, dict) and isinstance(res.get("active_demand"), bool):
                on_demand(res["active_demand"])
            return True
        except Exception:  # noqa: BLE001 - Never log remote responses or exception strings.
            if attempt + 1 < attempts:
                sleep(min(2**attempt, 4))
    return False


def parse_frame(data: bytes) -> tuple[dict, bytes]:
    if len(data) < 4:
        raise ValueError("short preview message")
    n = struct.unpack(">I", data[:4])[0]
    if not 0 < n <= 65536 or 4 + n >= len(data):
        raise ValueError("bad preview header")
    return json.loads(data[4 : 4 + n]), data[4 + n :]


class Frames:
    def __init__(self):
        self.lock = threading.Lock()
        self.items: dict[str, tuple[int, object]] = {}
        self.codecs: dict[str, tuple[int, bytes]] = {}

    def clear(self) -> None:
        with self.lock:
            self.items.clear()
            self.codecs.clear()

    def codec(self, info: dict) -> None:
        cid = info.get("camera_id")
        if cid in CAMERAS:
            prefix = b"".join(
                b"\x00\x00\x00\x01" + base64.b64decode(info[k], validate=True) for k in ("sps", "pps")
            )
            with self.lock:
                self.codecs[cid] = (info["stream_epoch"], prefix)
                self.items.pop(cid, None)

    def frame(self, data: bytes, active, decoder) -> None:
        if not active():
            return
        header, payload = parse_frame(data)
        cid = header.get("camera_id")
        if header.get("keyframe") is not True or cid not in CAMERAS:
            return
        with self.lock:
            codec = self.codecs.get(cid)
        if codec is None or codec[0] != header.get("stream_epoch"):
            return
        image = decoder(codec[1] + payload)
        if image is not None and active():
            with self.lock:
                self.items[cid] = (header["estimated_capture_time_ms"], image)

    def snapshot(self) -> dict[str, tuple[int, object]]:
        with self.lock:
            return dict(self.items)


def decode_keyframe(data: bytes):
    import av

    try:
        codec = av.CodecContext.create("h264", "r")
        codec.skip_frame = "NONKEY"
        codec.thread_count = 1
        frames = codec.decode(av.Packet(data)) + codec.decode(None)
        return frames[-1].to_ndarray(format="bgr24") if frames else None
    except Exception:  # noqa: BLE001 - Ignore single corrupt packet without dropping WS.
        return None


def demand_loop(client: Client, event: threading.Event, stop: threading.Event, interval_s: float = 2.0) -> None:
    while not stop.is_set():
        try:
            if client.demand():
                event.set()
            else:
                event.clear()
        except Exception:  # noqa: BLE001
            event.clear()
            LOG.warning("demand poll failed")
        stop.wait(interval_s)


def preview_loop(
    frames: Frames,
    event: threading.Event,
    stop: threading.Event,
    ws_url: str = PREVIEW_WS_URL,
) -> None:
    from websockets.sync.client import connect

    while not stop.is_set():
        if not event.wait(0.5):
            continue
        try:
            with connect(
                ws_url,
                origin=PREVIEW_ORIGIN,
                compression=None,
                max_size=16 * 1024 * 1024,
                open_timeout=3,
            ) as ws:
                while event.is_set() and not stop.is_set():
                    try:
                        msg = ws.recv(timeout=0.5)
                    except TimeoutError:
                        continue
                    if isinstance(msg, str):
                        obj = json.loads(msg)
                        if obj.get("kind") == "codec" and isinstance(obj.get("codec_info"), dict):
                            frames.codec(obj["codec_info"])
                    else:
                        frames.frame(msg, event.is_set, decode_keyframe)
        except Exception:  # noqa: BLE001
            LOG.warning("preview connection or decoding failed")
            stop.wait(2)
        finally:
            frames.clear()


def reuse_view_check(viewer: ViewChecker, reference, snapshot: dict, frames: list[CameraFrame],
                     round_ts_ms: int, stale_ms: int) -> dict | None:
    """재사용 기준이 있을 때만 같은 카메라끼리 시점을 비교한다(S15P31A101-712). 실패해도 각도 안내는 그대로 보낸다."""
    if reference is None:
        return None
    statuses = {f.camera_id: f.status(round_ts_ms, stale_ms) for f in frames}
    try:
        return viewer.check(reference, snapshot, statuses)
    except Exception:  # noqa: BLE001 - 서명 주소·응답 본문은 기록하지 않는다
        LOG.warning("reuse view check failed")
        return None


def reuse_view_checks(viewers: MultiViewChecker, references, snapshot: dict, frames: list[CameraFrame],
                      round_ts_ms: int, stale_ms: int) -> list[dict] | None:
    """후보 맵 목록이 있을 때만 맵마다 시점을 비교한다(S15P31A101-714). 실패해도 각도 안내는 그대로 보낸다."""
    if references is None:
        return None
    statuses = {f.camera_id: f.status(round_ts_ms, stale_ms) for f in frames}
    try:
        return viewers.check_all(references, snapshot, statuses) or None
    except Exception:  # noqa: BLE001 - 서명 주소·응답 본문은 기록하지 않는다
        LOG.warning("reuse view checks failed")
        return None


def run(env: dict[str, str], *, stop: threading.Event | None = None) -> None:
    token = env.get("SIMAI_DEVICE_TOKEN", "").strip()
    if token:
        LOG.addFilter(RedactFilter(token))
    stale_ms = bounded_setting(env, "SIMAI_CAMERA_HELPER_STALE_MS", DEFAULT_STALE_MS, 1000, 10_000)
    stall_seconds = bounded_setting(env, "SIMAI_CAMERA_HELPER_STALL_SECONDS", DEFAULT_STALL_SECONDS, 5, 300)
    source_id = env.get("SIMAI_LAPTOP_ID", "laptop-01").strip() or "laptop-01"
    demand_path = env.get("SIMAI_CAMERA_HELPER_DEMAND_PATH", "").strip() or DEFAULT_DEMAND_PATH
    ws_url = env.get("SIMAI_CAMERA_HELPER_PREVIEW_WS_URL", "").strip() or PREVIEW_WS_URL
    client = Client(env.get("SIMAI_EC2_BASE_URL", "").strip(), token, demand_path, source_id=source_id)
    frames = Frames()
    active = threading.Event()
    stop_event = stop if stop is not None else threading.Event()
    progress = Progress(stall_seconds)

    def apply_demand(flag: bool) -> None:
        if flag:
            active.set()
        else:
            active.clear()
            frames.clear()

    for target, args in (
        (demand_loop, (client, active, stop_event)),
        (preview_loop, (frames, active, stop_event, ws_url)),
        (watchdog_loop, (progress, frames, active, stop_event)),
    ):
        threading.Thread(target=target, args=args, daemon=True).start()

    matcher = None
    judge = None
    viewer = ViewChecker()
    viewers = MultiViewChecker()
    last_timestamps: dict[str, int] | None = None
    # 옛 EC2 는 coverage_guide 1.3.0(stage_a_readiness)을 계약 검사로 거부한다(400). 그러면 이번 수요 동안은 싣지 않고 보낸다(-761)
    send_stage_a = True
    try:
        while not stop_event.wait(0.5):
            if not active.is_set():
                judge = None
                last_timestamps = None
                send_stage_a = True
                continue
            snapshot = frames.snapshot()
            if set(snapshot) != set(CAMERAS):
                continue
            if matcher is None:
                progress.phase("loading")
                from .matcher import CameraMatcher, load_intrinsics

                lens_path = env.get("SIMAI_CAMERA_HELPER_INTRINSICS", "").strip()
                intrinsics = load_intrinsics(Path(lens_path)) if lens_path else None
                params = Params(
                    image_width_px=1024,
                    max_keypoints=2048,
                    intrinsics_source="STORED" if intrinsics else "DEFAULT",
                    ransac_threshold_px=2.0 if intrinsics else 3.0,
                )
                aliked, lightglue = resolve_weights(env)
                matcher = CameraMatcher(aliked, lightglue, params, intrinsics, device="auto")
                progress.phase("waiting")
                # Refresh snapshot after one-time model loading so frame timestamps are current.
                snapshot = frames.snapshot()
                if set(snapshot) != set(CAMERAS):
                    continue
            if judge is None:
                judge = CoverageJudge(matcher.params)
            timestamps = {cid: snapshot[cid][0] for cid in CAMERAS}
            if timestamps == last_timestamps:
                continue
            if not active.is_set():
                continue
            round_ts_ms = max(time.time_ns() // 1_000_000, max(timestamps.values()))
            outcome = "failed"
            grades = ["UNKNOWN"] * len(PAIRS)
            stage_a = None
            progress.phase("matching")
            match_started = time.monotonic()
            match_ms = None
            try:
                pairs = matcher.match({cid: snapshot[cid][1] for cid in CAMERAS})
                match_ms = int((time.monotonic() - match_started) * 1000)
                if not active.is_set():
                    continue
                last_timestamps = timestamps
                frames_now = [CameraFrame(cid, timestamps[cid]) for cid in CAMERAS]
                references = getattr(client, "reuse_references", lambda: None)()
                views = reuse_view_checks(viewers, references, snapshot, frames_now, round_ts_ms, stale_ms)
                view = None if references is not None else reuse_view_check(
                    viewer, getattr(client, "reuse_reference", lambda: None)(), snapshot, frames_now, round_ts_ms,
                    stale_ms)
                message = build_message(
                    source_id,
                    round_ts_ms,
                    frames_now,
                    pairs,
                    judge,
                    stale_ms=stale_ms,
                    reuse_view_check=view,
                    reuse_view_checks=views,
                    stage_a=send_stage_a,
                )
                if view is not None:
                    LOG.info("reuse view %s", describe(view))
                for one in views or []:
                    LOG.info("reuse view %s %s", one["scene_version"], describe(one))
                grades = [p["grade"] for p in message["pairs"]]
                stage_a = judge.stage_a
                progress.phase("posting")
                sent = retry_post(client, message, active.is_set, sleep=stop_event.wait,
                                  on_demand=apply_demand)
                if sent:
                    outcome = "ok"
                elif time.time_ns() // 1_000_000 >= message["valid_until_ms"]:
                    outcome = "expired"
                if not sent and send_stage_a and getattr(client, "last_error_status", None) == 400:
                    send_stage_a = False
                    LOG.warning("EC2 rejected coverage_guide 1.3.0; sending without stage_a_readiness until demand ends")
            finally:
                if match_ms is None:
                    match_ms = int((time.monotonic() - match_started) * 1000)
                progress.finished(outcome)
                log_round(round_ts_ms, match_ms, timestamps, grades, outcome, stage_a)
    finally:
        stop_event.set()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        run(load_env(ROOT / ".env"))
    except KeyboardInterrupt:
        pass
    except Exception as e:  # noqa: BLE001
        LOG.error("helper stopped (%s)", type(e).__name__)
        raise SystemExit(1) from None
