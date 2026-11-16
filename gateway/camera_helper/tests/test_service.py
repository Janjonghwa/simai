import base64
import hashlib
import io
import json
import logging
import struct
import tempfile
import threading
import time
import unittest
import urllib.error
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from simai_camera_helper.judge import CAMERAS, PAIRS, PairMetrics
from simai_camera_helper.service import (
    DEFAULT_ALIKED_SHA256,
    DEFAULT_ALIKED_WEIGHTS,
    Client,
    Frames,
    RedactFilter,
    checked_weight,
    decode_keyframe,
    idle_message,
    load_env,
    parse_frame,
    resolve_weights,
    retry_post,
    run,
)


def make_frame(camera_id="cam_A", *, key=True, epoch=1, ts_ms=123, payload=b"picture"):
    header = json.dumps(
        {
            "camera_id": camera_id,
            "stream_epoch": epoch,
            "keyframe": key,
            "estimated_capture_time_ms": ts_ms,
        }
    ).encode("utf-8")
    return struct.pack(">I", len(header)) + header + payload


def validate_coverage_guide_schema(instance: dict) -> None:
    import jsonschema
    from referencing import Registry, Resource

    schema_path = Path(__file__).resolve().parents[3] / "contracts/messages/coverage_guide.schema.json"
    defs_path = schema_path.parent.parent / "common/defs.schema.json"
    resource = Resource.from_contents(json.loads(defs_path.read_text(encoding="utf-8")))
    registry = Registry().with_resource("https://simai.dev/contracts/common/defs.schema.json", resource)
    validator = jsonschema.Draft202012Validator(
        json.loads(schema_path.read_text(encoding="utf-8")),
        registry=registry,
    )
    validator.validate(instance)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.frames = Frames()
        for cid in CAMERAS:
            self.frames.codec(
                {
                    "camera_id": cid,
                    "stream_epoch": 1,
                    "sps": base64.b64encode(b"sps").decode("ascii"),
                    "pps": base64.b64encode(b"pps").decode("ascii"),
                }
            )

    def test_only_keyframes_and_only_with_demand(self):
        calls = []

        def decoder(data):
            calls.append(data)
            return "image"

        self.frames.frame(make_frame("cam_A", key=False), lambda: True, decoder)
        self.frames.frame(make_frame("cam_A", key=True), lambda: False, decoder)
        self.frames.frame(make_frame("cam_A", key=True, epoch=2), lambda: True, decoder)
        self.frames.frame(make_frame("cam_Z", key=True), lambda: True, decoder)
        self.assertEqual(calls, [])

        self.frames.frame(make_frame("cam_A", key=True), lambda: True, decoder)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.frames.snapshot()["cam_A"], (123, "image"))

    def test_pyav_decode_keyframe_real_h264_and_corrupt_packet(self):
        import av
        import numpy as np

        enc = av.CodecContext.create("libx264", "w")
        enc.width = 320
        enc.height = 240
        enc.pix_fmt = "yuv420p"
        enc.time_base = Fraction(1, 25)
        enc.options = {"g": "1", "bf": "0", "tune": "zerolatency"}
        img = np.zeros((240, 320, 3), dtype=np.uint8)
        img[:, :, 1] = 180
        vf = av.VideoFrame.from_ndarray(img, format="bgr24")
        vf.pts = 0
        raw = b"".join(bytes(p) for p in enc.encode(vf) + enc.encode(None))
        decoded = decode_keyframe(raw)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded.shape, (240, 320, 3))
        self.assertIsNone(decode_keyframe(b"\x00\x00\x00\x01corrupt"))

    def test_parse_frame_rejects_invalid_headers(self):
        with self.assertRaises(ValueError):
            parse_frame(b"123")
        with self.assertRaises(ValueError):
            parse_frame(struct.pack(">I", 0) + b"{}")
        with self.assertRaises(ValueError):
            parse_frame(struct.pack(">I", 10) + b"{}")

    def test_retry_and_backoff_and_expiration(self):
        class FakeClient:
            calls = 0

            def post(self, message):
                self.calls += 1
                if self.calls < 3:
                    raise OSError("secret must not be logged")
                return {"active_demand": False}

        client = FakeClient()
        waits = []
        demand_states = []
        now = time.time_ns() // 1_000_000
        message = {"valid_until_ms": now + 10_000}
        self.assertTrue(
            retry_post(client, message, lambda: True, waits.append, on_demand=demand_states.append)
        )
        self.assertEqual(waits, [1, 2])
        self.assertEqual(client.calls, 3)
        self.assertEqual(demand_states, [False])

        # Demand off -> 0 attempts.
        self.assertFalse(retry_post(client, message, lambda: False, waits.append))
        self.assertEqual(client.calls, 3)

        # Expired message -> 0 attempts.
        expired = {"valid_until_ms": now - 1}
        self.assertFalse(retry_post(client, expired, lambda: True, waits.append))
        self.assertEqual(client.calls, 3)

    def test_no_demand_produces_zero_matcher_calculations(self):
        stop = threading.Event()
        matcher_calls = []

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def demand(self):
                stop.set()
                return False

            def post(self, message):
                raise AssertionError("post must not be called without demand")

        with (
            patch("simai_camera_helper.service.Client", FakeClient),
            patch("simai_camera_helper.service.preview_loop", lambda *a, **k: None),
            patch(
                "simai_camera_helper.matcher.CameraMatcher",
                side_effect=lambda *a, **k: matcher_calls.append(1),
            ),
        ):
            run(
                {
                    "SIMAI_EC2_BASE_URL": "https://example.com",
                    "SIMAI_DEVICE_TOKEN": "tok-secret-123",
                },
                stop=stop,
            )
        self.assertEqual(matcher_calls, [])

    def test_active_demand_runs_calculation_validates_schema_and_stops_when_post_turns_demand_off(self):
        stop = threading.Event()
        posted = []
        match_calls = []

        class FakeMatcher:
            def __init__(self, aliked, lightglue, params, intrinsics, *, device="auto"):
                self.params = params
                self.device = device

            def match(self, images):
                match_calls.append(set(images))
                return [
                    PairMetrics(a, b, 120, 45, (0.55, 0.5), (0.45, 0.5))
                    for a, b in PAIRS
                ]

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def demand(self):
                return True

            def post(self, message):
                posted.append(message)
                # Turn active_demand off in POST response; service must stop computing further rounds.
                stop.set()
                return {"active_demand": False}

        now_ms = time.time_ns() // 1_000_000

        def fake_preview_loop(frames, event, stop_event, ws_url):
            event.wait(1.0)
            for cid in CAMERAS:
                frames.codec(
                    {
                        "camera_id": cid,
                        "stream_epoch": 1,
                        "sps": base64.b64encode(b"sps").decode("ascii"),
                        "pps": base64.b64encode(b"pps").decode("ascii"),
                    }
                )
                frames.frame(
                    make_frame(cid, key=True, ts_ms=now_ms),
                    event.is_set,
                    lambda _: "decoded_bgr",
                )

        with (
            patch("simai_camera_helper.service.Client", FakeClient),
            patch("simai_camera_helper.service.preview_loop", fake_preview_loop),
            patch("simai_camera_helper.service.resolve_weights", return_value=(Path("a"), Path("b"))),
            patch("simai_camera_helper.matcher.CameraMatcher", FakeMatcher),
        ):
            run(
                {
                    "SIMAI_EC2_BASE_URL": "https://example.com",
                    "SIMAI_DEVICE_TOKEN": "tok-secret-123",
                    "SIMAI_LAPTOP_ID": "gw-lobby",
                },
                stop=stop,
            )

        self.assertEqual(len(match_calls), 1)
        self.assertEqual(len(posted), 1)
        validate_coverage_guide_schema(posted[0])
        self.assertEqual(posted[0]["source_id"], "gw-lobby")
        self.assertEqual(posted[0]["params"]["image_width_px"], 1024)
        self.assertEqual(posted[0]["params"]["max_keypoints"], 2048)

    def test_old_ec2_rejecting_1_3_0_falls_back_to_message_without_stage_a(self):
        """S15P31A101-761: 옛 EC2 가 1.3.0 을 400 으로 거부하면 이번 수요 동안 stage_a_readiness 없이 보낸다."""
        stop = threading.Event()
        posted = []
        tried = threading.Event()

        class FakeMatcher:
            def __init__(self, aliked, lightglue, params, intrinsics, *, device="auto"):
                self.params = params

            def match(self, images):
                return [PairMetrics(a, b, 120, 45, (0.55, 0.5), (0.45, 0.5), 4, 4) for a, b in PAIRS]

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.last_error_status = None

            def demand(self):
                return True

            def post(self, message):
                posted.append(message)
                if "stage_a_readiness" in message:
                    self.last_error_status = 400
                    tried.set()
                    raise urllib.error.HTTPError("https://example.com", 400, "Bad Request", None, io.BytesIO(b""))
                self.last_error_status = None
                stop.set()
                return {"active_demand": True}

        def one_try(client, message, active, sleep=None, attempts=4, on_demand=None):
            try:
                client.post(message)
                return True
            except Exception:  # noqa: BLE001
                return False

        now_ms = time.time_ns() // 1_000_000

        def fake_preview_loop(frames, event, stop_event, ws_url):
            event.wait(1.0)
            for k in range(2):
                for cid in CAMERAS:
                    frames.codec({"camera_id": cid, "stream_epoch": 1,
                                  "sps": base64.b64encode(b"sps").decode("ascii"),
                                  "pps": base64.b64encode(b"pps").decode("ascii")})
                    frames.frame(make_frame(cid, key=True, ts_ms=now_ms + k * 1000), event.is_set,
                                 lambda _: "decoded_bgr")
                tried.wait(5.0)

        with (
            patch("simai_camera_helper.service.Client", FakeClient),
            patch("simai_camera_helper.service.preview_loop", fake_preview_loop),
            patch("simai_camera_helper.service.retry_post", one_try),
            patch("simai_camera_helper.service.resolve_weights", return_value=(Path("a"), Path("b"))),
            patch("simai_camera_helper.matcher.CameraMatcher", FakeMatcher),
        ):
            run({"SIMAI_EC2_BASE_URL": "https://example.com", "SIMAI_DEVICE_TOKEN": "tok-secret-123",
                 "SIMAI_LAPTOP_ID": "gw-lobby"}, stop=stop)

        self.assertEqual([m["schema_version"] for m in posted], ["1.3.0", "1.0.0"])
        self.assertNotIn("stage_a_readiness", posted[1])
        validate_coverage_guide_schema(posted[0])
        validate_coverage_guide_schema(posted[1])

    def test_old_ec2_rejecting_1_4_0_drops_aim_check_first(self):
        """S15P31A101-765: aim_check(1.4.0)를 400 으로 거부하면 이번 수요 동안 aim_check 만 빼고 1.3.0 으로 보낸다."""
        stop = threading.Event()
        posted = []
        tried = threading.Event()
        aim = {"scene_version": "s", "calibration_version": "c", "max_aim_rad": 0.004363, "max_roll_rad": 0.017453,
               "max_setup_aim_rad": 0.02618, "max_setup_roll_rad": 0.05236, "hysteresis_rounds": 2,
               "cameras": [{"camera_id": cid, "status": "UNKNOWN", "reason": "NO_TEXTURE", "matches": None,
                            "inlier_ratio": None, "pan_rad": None, "tilt_rad": None, "roll_rad": None,
                            "target_point": None, "target_px": None, "arrow_rad": None} for cid in CAMERAS]}
        aim_calls = []

        class FakeMatcher:
            def __init__(self, aliked, lightglue, params, intrinsics, *, device="auto"):
                self.params = params

            def match(self, images):
                return [PairMetrics(a, b, 120, 45, (0.55, 0.5), (0.45, 0.5), 4, 4) for a, b in PAIRS]

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.last_error_status = None

            def demand(self):
                return True

            def aim_reference(self):
                return {"scene_version": "s", "calibration_version": "c", "cameras": []}

            def post(self, message):
                posted.append(message)
                if "aim_check" in message:
                    self.last_error_status = 400
                    tried.set()
                    raise urllib.error.HTTPError("https://example.com", 400, "Bad Request", None, io.BytesIO(b""))
                self.last_error_status = None
                stop.set()
                return {"active_demand": True}

        def one_try(client, message, active, sleep=None, attempts=4, on_demand=None):
            try:
                client.post(message)
                return True
            except Exception:  # noqa: BLE001
                return False

        def fake_aim(aimer, reference, *args, **kwargs):
            aim_calls.append(reference)
            return aim if reference is not None else None

        now_ms = time.time_ns() // 1_000_000

        def fake_preview_loop(frames, event, stop_event, ws_url):
            event.wait(1.0)
            for k in range(2):
                for cid in CAMERAS:
                    frames.codec({"camera_id": cid, "stream_epoch": 1,
                                  "sps": base64.b64encode(b"sps").decode("ascii"),
                                  "pps": base64.b64encode(b"pps").decode("ascii")})
                    frames.frame(make_frame(cid, key=True, ts_ms=now_ms + k * 1000), event.is_set,
                                 lambda _: "decoded_bgr")
                tried.wait(5.0)

        with (
            patch("simai_camera_helper.service.Client", FakeClient),
            patch("simai_camera_helper.service.preview_loop", fake_preview_loop),
            patch("simai_camera_helper.service.retry_post", one_try),
            patch("simai_camera_helper.service.aim_check", fake_aim),
            patch("simai_camera_helper.service.resolve_weights", return_value=(Path("a"), Path("b"))),
            patch("simai_camera_helper.matcher.CameraMatcher", FakeMatcher),
        ):
            run({"SIMAI_EC2_BASE_URL": "https://example.com", "SIMAI_DEVICE_TOKEN": "tok-secret-123",
                 "SIMAI_LAPTOP_ID": "gw-lobby"}, stop=stop)

        self.assertEqual([m["schema_version"] for m in posted], ["1.4.0", "1.3.0"])
        self.assertIn("stage_a_readiness", posted[1])
        self.assertNotIn("aim_check", posted[1])
        self.assertIsNone(aim_calls[1])  # 두 번째 라운드는 계산도 하지 않는다
        validate_coverage_guide_schema(posted[0])
        validate_coverage_guide_schema(posted[1])

    def test_client_post_records_http_error_status(self):
        client = Client("https://example.com", "secret-token-xyz", "/api/devices/coverage-guides")
        error = urllib.error.HTTPError("https://example.com", 400, "Bad Request", None, io.BytesIO(b""))
        with patch.object(client, "request", side_effect=error), self.assertRaises(urllib.error.HTTPError):
            client.post({"valid_until_ms": 0})
        self.assertEqual(client.last_error_status, 400)
        with patch.object(client, "request", return_value={"active_demand": True}):
            client.post({"valid_until_ms": 0})
        self.assertIsNone(client.last_error_status)

    def test_idle_message_validates_against_schema_and_client_falls_back_on_405(self):
        msg = idle_message("laptop-01", 1_793_690_000_000)
        validate_coverage_guide_schema(msg)
        self.assertEqual(msg["connectivity"], "UNKNOWN")
        self.assertTrue(all(c["status"] == "NO_VIDEO" for c in msg["cameras"]))

        client = Client("https://example.com", "secret-token-xyz", "/api/devices/coverage-guides")
        self.assertNotIn("secret-token-xyz", repr(client))

        calls = []

        def fake_request(path, body=None):
            calls.append((path, body is not None))
            if body is None:
                raise urllib.error.HTTPError(
                    url="https://example.com" + path,
                    code=405,
                    msg="Method Not Allowed",
                    hdrs=None,
                    fp=io.BytesIO(b""),
                )
            validate_coverage_guide_schema(body)
            return {"accepted": True, "active_demand": True}

        with patch.object(client, "request", side_effect=fake_request):
            self.assertTrue(client.demand())
        self.assertEqual(
            calls,
            [
                ("/api/devices/coverage-guides", False),
                ("/api/devices/coverage-guides", True),
            ],
        )

    def test_client_validation_and_secret_redaction(self):
        with self.assertRaises(ValueError):
            Client("http://example.com", "tok", "/api/devices/coverage-guides")
        with self.assertRaises(ValueError):
            Client("https://example.com/tok", "tok", "/api/devices/coverage-guides")
        with self.assertRaises(ValueError):
            Client("https://example.com", "tok", "//evil.com/path")
        loopback = Client("http://127.0.0.1:8080", "tok-secret", "/api/devices/coverage-guides")
        self.assertEqual(loopback.base, "http://127.0.0.1:8080")

        record = logging.LogRecord(
            name="test",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg="failed with token %s",
            args=("super-secret-device-token",),
            exc_info=None,
        )
        RedactFilter("super-secret-device-token").filter(record)
        self.assertNotIn("super-secret-device-token", record.getMessage())
        self.assertIn("[REDACTED]", record.getMessage())

    def test_missing_or_invalid_weight_hash_rejected(self):
        with self.assertRaises(ValueError):
            checked_weight(Path("unused"), "")
        with tempfile.TemporaryDirectory() as folder:
            w = Path(folder) / "w.pth"
            w.write_bytes(b"weights")
            digest = hashlib.sha256(b"weights").hexdigest()
            self.assertEqual(checked_weight(w, digest), w)
            with self.assertRaises(ValueError):
                checked_weight(w, "0" * 64)
            with self.assertRaises(ValueError):
                resolve_weights({"SIMAI_CAMERA_HELPER_ALIKED_WEIGHTS": str(w)})
            env = load_env(w)
            self.assertIsInstance(env, dict)
        self.assertEqual(len(DEFAULT_ALIKED_SHA256), 64)
        self.assertEqual(DEFAULT_ALIKED_WEIGHTS.name, "aliked-n16.pth")

    def test_compare_relay_script_parses_before_and_after_logs(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "compare_relay",
            Path(__file__).resolve().parents[1] / "compare_relay.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        sample = '2026-11-04 01:00:00,000 INFO [cam_A] 상태 {"camera_id": "cam_A", "frames": 100, "receive_delay_ms": {"p95": 42.0}}\n2026-11-04 01:00:10,000 INFO [cam_A] 상태 {"camera_id": "cam_A", "frames": 250, "receive_delay_ms": {"p95": 44.0}}'
        with tempfile.TemporaryDirectory() as folder:
            log_path = Path(folder) / "relay.log"
            log_path.write_text(sample, encoding="utf-8")
            parsed = mod.read(log_path)
        self.assertAlmostEqual(parsed["cam_A"]["fps"], 15.0)
        self.assertEqual(parsed["cam_A"]["receive_delay_p95_median_ms"], 43.0)


if __name__ == "__main__":
    unittest.main()
