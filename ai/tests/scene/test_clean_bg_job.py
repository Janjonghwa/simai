"""`-372` 사람 지운 배경 묶음 업로드 및 `static_reconstruct` 작업 입력 생성 도구(`simai_ai.scene.clean_bg_job`) 단위·통합 시험."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import threading
from typing import Any
import uuid

import cv2
import numpy as np
import pytest

from simai_ai.providers.contract import schema_errors
from simai_ai.scene.clean_bg_job import run_cli
from simai_ai.scene.reconstruct import sha256, validate_input


def _make_calibration_doc(
    *,
    calibration_version: str = "cal-room-01",
    provider_source: str = "G1_AUTO",
    state: str = "CALIBRATED",
    camera_ids: tuple[str, ...] = ("cam-1", "cam-2", "cam-3", "cam-4"),
    image_size: tuple[int, int] = (32, 24),
) -> dict[str, Any]:
    w, h = image_size
    cameras: list[dict[str, Any]] = []
    for idx, cid in enumerate(camera_ids):
        cameras.append(
            {
                "camera_id": cid,
                "image_size": [w, h],
                "K": [
                    [28.0, 0.0, w / 2.0],
                    [0.0, 28.0, h / 2.0],
                    [0.0, 0.0, 1.0],
                ],
                "distortion": {
                    "model": "OPENCV_PINHOLE",
                    "coeffs": [0.0, 0.0, 0.0, 0.0, 0.0],
                },
                "R": [[-1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, -1.0]],
                "t": [float(idx), -1.0, 2.5],
                "camera_height_m": 2.5,
                "reprojection_error_px": {"median": 0.8, "p95": 1.6},
            }
        )
    doc: dict[str, Any] = {
        "schema_version": "2.1.0",
        "provider_source": provider_source,
        "calibration_version": calibration_version,
        "state": state,
        "created_at": 1_760_000_000_000,
        "coordinate_frame": {
            "frame": "WORLD",
            "unit": "m",
            "handedness": "right",
            "up_axis": "+Z",
            "site_id": "site-classroom-1",
        },
        "world_axes_source": "WALL_AND_FLOOR",
        "scale_basis": "MEASURED_OBJECT" if provider_source == "G1_AUTO" else "DATASET_GT",
        "extrinsic_convention": "OPENCV_WORLD_TO_CAMERA",
        "cameras": cameras,
    }
    if state == "CALIBRATED":
        doc["scale_cross_check_basis"] = ["HUMAN_HEIGHT_STATS"]
        doc["validation"] = {
            "checkpoint_count": 4,
            "max_pairwise_position_disagreement_m": 0.04,
            "max_head_height_error_m": 0.05,
            "floor_plane_disagreement_m": 0.01,
            "floor_coverage_ratio": 0.9,
        }
    return doc


def _encode_jpeg(width: int = 32, height: int = 24, seed: int = 0) -> bytes:
    rng = np.random.default_rng(seed)
    img = rng.integers(20, 220, size=(height, width, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
    assert ok
    return buf.tobytes()


def _write_clean_bg_bundle(
    root: Path,
    cal: dict[str, Any],
    *,
    frame_source: str = "LIVE",
    complete: bool = True,
    pod_style_status_path: bool = False,
) -> tuple[Path, Path, dict[str, bytes]]:
    """`-363` `BackgroundCleanBackground` 출력 구조와 똑같은 묶음 디렉터리를 만든다."""
    cal_ver = cal["calibration_version"]
    bundle_name = "bundle-11111111-2222-3333-4444-555555555555"
    bundle_dir = root / cal_ver / bundle_name
    bundle_dir.mkdir(parents=True, exist_ok=True)

    cal_cams = {c["camera_id"]: c for c in cal["cameras"]}
    camera_Ids = list(cal_cams.keys())
    camera_generations = {cid: 1 for cid in camera_Ids}
    cameras_meta: dict[str, Any] = {}
    raw_images: dict[str, bytes] = {}

    for idx, cid in enumerate(camera_Ids):
        w, h = cal_cams[cid]["image_size"]
        jpg = _encode_jpeg(w, h, seed=100 + idx)
        raw_images[cid] = jpg
        (bundle_dir / f"{cid}.jpg").write_bytes(jpg)
        cameras_meta[cid] = {
            "camera_id": cid,
            "ready": True,
            "calibration_version": cal_ver,
            "sample_count": 32,
            "time_range_ms": [1_000, 32_000],
            "never_revealed_ratio": 0.0,
            "mean_person_cover_ratio": 0.05,
            "image": f"{cid}.jpg",
            "image_sha256": sha256(jpg),
            "image_size": [w, h],
        }

    input_prov = {
        "frames": {"source": frame_source},
        "calibration": {"source": cal["provider_source"], "calibration_version": cal_ver},
        "is_upper_bound": frame_source == "SIM" or cal["provider_source"] in {"SIM", "DATASET_GT"},
    }
    meta_doc = {
        "format": "simai.clean_backgrounds",
        "format_version": "0.1.0",
        "calibration_version": cal_ver,
        "created_at_ms": 33_000,
        "ready": True,
        "complete": complete,
        "input_provenance": input_prov,
        "camera_generations": camera_generations,
        "cameras": cameras_meta,
    }
    (bundle_dir / "clean_backgrounds.json").write_text(json.dumps(meta_doc, indent=2), encoding="utf-8")
    if complete:
        complete_doc = {
            "calibration_version": cal_ver,
            "created_at_ms": 33_000,
            "metadata": "clean_backgrounds.json",
            "camera_ids": camera_Ids,
        }
        (bundle_dir / "COMPLETE").write_text(json.dumps(complete_doc, indent=2), encoding="utf-8")

    bundle_path_in_status = (
        f"/workspace/simai/state/clean-background/{cal_ver}/{bundle_name}"
        if pod_style_status_path
        else str(bundle_dir)
    )
    status_doc = {
        "calibration_version": cal_ver,
        "updated_at_ms": 33_000,
        "input_provenance": input_prov,
        "camera_generations": camera_generations,
        "cameras": cameras_meta,
        "ready": True,
        "complete": complete,
        "bundle": bundle_path_in_status if complete else None,
    }
    (root / "status.json").write_text(json.dumps(status_doc, indent=2), encoding="utf-8")
    return root, bundle_dir, raw_images


class _FakeUploadServer:
    """`POST /api/uploads/sign` 과 `PUT /upload/<key>` 를 흉내 내는 로컬 HTTP 서버."""

    def __init__(
        self,
        *,
        expected_token: str = "secret-tier0-token-xyz987",
        bucket: str = "simai-prod-bucket",
        sign_status: int = 200,
        put_status: int = 200,
        fail_put_on_call: int | None = None,
        bad_object_uri: str | None = None,
        current_calibration_version: str | None = "cal-room-01",
        latest_uploaded_version: str | None = "cal-room-01",
        calibration_status: int = 200,
    ) -> None:
        self.expected_token = expected_token
        self.bucket = bucket
        self.sign_status = sign_status
        self.put_status = put_status
        self.fail_put_on_call = fail_put_on_call
        self.bad_object_uri = bad_object_uri
        self.current_calibration_version = current_calibration_version
        self.latest_uploaded_version = latest_uploaded_version
        self.calibration_status = calibration_status
        self.calibration_requests: list[dict[str, Any]] = []
        self.sign_requests: list[dict[str, Any]] = []
        self.put_requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()

        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path != "/api/calibrations/current":
                    self.send_response(404)
                    self.end_headers()
                    return
                auth = self.headers.get("Authorization", "")
                with parent._lock:
                    parent.calibration_requests.append({"auth": auth, "path": self.path})
                if parent.calibration_status != 200:
                    self.send_response(parent.calibration_status)
                    self.end_headers()
                    self.wfile.write(b'{"error":"calibration_check_failed"}')
                    return
                resp = {
                    "calibration_state": "CALIBRATED" if parent.current_calibration_version else "UNCALIBRATED",
                    "calibration_version": parent.current_calibration_version,
                    "latest_uploaded_version": parent.latest_uploaded_version,
                    "document_uploaded": parent.latest_uploaded_version is not None,
                }
                raw = json.dumps(resp).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/api/uploads/sign":
                    self.send_response(404)
                    self.end_headers()
                    return
                auth = self.headers.get("Authorization", "")
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                with parent._lock:
                    parent.sign_requests.append({"auth": auth, "body": body})
                if auth != f"Bearer {parent.expected_token}":
                    self.send_response(401)
                    self.end_headers()
                    self.wfile.write(b'{"error":"unauthorized"}')
                    return
                if parent.sign_status != 200:
                    self.send_response(parent.sign_status)
                    self.end_headers()
                    self.wfile.write(b'{"error":"sign_failed"}')
                    return
                purpose = body["purpose"]
                ext = ".jpg" if body["content_type"] == "image/jpeg" else (
                    ".png" if body["content_type"] == "image/png" else ".bin"
                )
                obj_uuid = str(uuid.uuid4())
                if purpose == "EVENT_FRAME":
                    event_id = body["event_id"]
                    key = f"event-frames/{event_id}/{obj_uuid}{ext}"
                elif purpose == "AVATAR_ASSET":
                    key = f"avatars/{obj_uuid}{ext}"
                else:
                    key = f"tickets/{obj_uuid}{ext}"
                object_uri = parent.bad_object_uri or f"s3://{parent.bucket}/{key}"
                upload_url = f"{parent.base_url}/upload/{key}?X-Amz-Signature=presigned-secret-sig-12345"
                resp = {
                    "object_key": key,
                    "object_uri": object_uri,
                    "method": "PUT",
                    "upload_url": upload_url,
                    "headers": {
                        "Content-Type": body["content_type"],
                        "Content-Length": str(body["size_bytes"]),
                        "x-amz-checksum-sha256": body["sha256"],
                    },
                    "expires_at_ms": 1_760_000_900_000,
                }
                raw = json.dumps(resp).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_PUT(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                data = self.rfile.read(length)
                with parent._lock:
                    call_idx = len(parent.put_requests) + 1
                    parent.put_requests.append(
                        {
                            "path": self.path,
                            "headers": dict(self.headers.items()),
                            "bytes": data,
                        }
                    )
                if parent.fail_put_on_call == call_idx:
                    self.send_response(500)
                    self.end_headers()
                    return
                self.send_response(parent.put_status)
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

        self._httpd = HTTPServer(("127.0.0.1", 0), Handler)
        host, port = self._httpd.server_address
        self.base_url = f"http://{host}:{port}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self) -> _FakeUploadServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


def test_dry_run_with_clean_bg_bundle_creates_valid_job_input(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--dry-run` 은 네트워크 호출 없이 `-363` 묶음(Pod 경로 `status.json` 포함)을 검증하고 계약 통과 JSON 과 안내를 낸다."""
    cal = _make_calibration_doc()
    cal_path = tmp_path / "calibration.json"
    cal_bytes = json.dumps(cal, indent=2).encode("utf-8")
    cal_path.write_bytes(cal_bytes)

    bundle_root, _, _ = _write_clean_bg_bundle(
        tmp_path / "clean-background",
        cal,
        frame_source="LIVE",
        pod_style_status_path=True,
    )
    out_path = tmp_path / "out" / "static_reconstruct_input.json"

    rc = run_cli(
        [
            "--bundle",
            str(bundle_root),
            "--calibration",
            str(cal_path),
            "--calibration-ref",
            "s3://simai-prod-bucket/calibrations/cal-room-01/calibration.json",
            "--output",
            str(out_path),
            "--dry-run",
        ],
        env={},
    )
    assert rc == 0
    assert out_path.is_file()

    job_input = json.loads(out_path.read_text(encoding="utf-8"))
    validate_input(job_input)
    assert job_input["calibration_version"] == "cal-room-01"
    assert job_input["calibration_ref"] == "s3://simai-prod-bucket/calibrations/cal-room-01/calibration.json"
    assert job_input["calibration_hash"] == sha256(cal_bytes)
    assert [img["camera_id"] for img in job_input["images"]] == ["cam-1", "cam-2", "cam-3", "cam-4"]
    assert all(img["kind"] == "CLEAN_BACKGROUND" for img in job_input["images"])
    assert all(img["uri"].startswith("s3://simai-prod-bucket/") for img in job_input["images"])
    assert job_input["input_provenance"] == {
        "frames": {"source": "LIVE", "camera_ids": ["cam-1", "cam-2", "cam-3", "cam-4"]},
        "calibration": {"source": "G1_AUTO", "calibration_version": "cal-room-01"},
        "is_upper_bound": False,
    }
    assert job_input["options"] == {"align_views": "off", "plane_cleanup": False}

    captured = capsys.readouterr()
    assert "LIVE_EXP_BLOCKED" in captured.out
    assert "admin job enqueue --type static_reconstruct --input-file" in captured.out
    assert "admin scene list --site site-classroom-1" in captured.out
    assert "admin scene activate --site site-classroom-1" in captured.out


def test_upload_flow_with_bundle_and_fake_server(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """가짜 서명·업로드 서버로 4대 배경 이미지를 올리고 `-364` 작업 입력 JSON 을 만든다(토큰·서명 쿼리는 출력 금지)."""
    cal = _make_calibration_doc()
    cal_path = tmp_path / "calibration.json"
    cal_bytes = json.dumps(cal).encode("utf-8")
    cal_path.write_bytes(cal_bytes)

    _, bundle_dir, raw_images = _write_clean_bg_bundle(tmp_path / "clean-background", cal)
    out_path = tmp_path / "job_input.json"
    secret_token = "secret-tier0-token-do-not-print-999"

    with _FakeUploadServer(expected_token=secret_token, bucket="simai-prod-bucket") as server:
        rc = run_cli(
            [
                "--bundle",
                str(bundle_dir),
                "--calibration",
                str(cal_path),
                "--calibration-ref",
                "s3://simai-prod-bucket/calibrations/cal-room-01.json",
                "--output",
                str(out_path),
                "--server",
                server.base_url,
                "--align-views",
                "scale",
                "--plane-cleanup",
            ],
            env={"SIMAI_TIER0_TOKEN": secret_token},
        )
        assert rc == 0
        assert len(server.sign_requests) == 4
        assert len(server.put_requests) == 4

        event_ids = {req["body"]["event_id"] for req in server.sign_requests}
        assert len(event_ids) == 1  # 같은 세션 UUID 로 묶인다
        for cid, sign_req, put_req in zip(["cam-1", "cam-2", "cam-3", "cam-4"], server.sign_requests, server.put_requests):
            expected_bytes = raw_images[cid]
            assert sign_req["body"]["purpose"] == "EVENT_FRAME"
            assert sign_req["body"]["content_type"] == "image/jpeg"
            assert sign_req["body"]["size_bytes"] == len(expected_bytes)
            assert sign_req["body"]["sha256"] == sha256(expected_bytes).split(":", 1)[1]
            assert put_req["bytes"] == expected_bytes

    job_input = json.loads(out_path.read_text(encoding="utf-8"))
    validate_input(job_input)
    assert job_input["options"] == {"align_views": "scale", "plane_cleanup": True}
    assert all(img["uri"].startswith("s3://simai-prod-bucket/event-frames/") for img in job_input["images"])

    envelope = {
        "msg_type": "job_request",
        "msg_id": str(uuid.uuid4()),
        "schema_version": "2.0.0",
        "sent_at": 1_760_000_000_000,
        "source_id": "ec2",
        "payload": {
            "job_id": str(uuid.uuid4()),
            "lease_id": str(uuid.uuid4()),
            "job_type": "static_reconstruct",
            "priority": "BACKGROUND",
            "attempt": 1,
            "max_attempts": 2,
            "leased_at": 1_760_000_000_000,
            "lease_expires_at": 1_760_000_060_000,
            "heartbeat_interval_ms": 15_000,
            "timeout_ms": 4_200_000,
            "input": job_input,
        },
    }
    assert schema_errors(envelope, "job_request") == []

    captured = capsys.readouterr()
    assert secret_token not in captured.out
    assert secret_token not in captured.err
    assert "presigned-secret-sig-12345" not in captured.out
    assert "presigned-secret-sig-12345" not in captured.err


def test_explicit_images_and_upload_calibration_with_upper_bound_provenance(tmp_path: Path) -> None:
    """`--image` 4대 입력과 `--upload-calibration`, `DATASET_GT` 보정 시 `is_upper_bound=True` 를 검증한다."""
    cal = _make_calibration_doc(provider_source="DATASET_GT", state="PROVISIONAL")
    cal_path = tmp_path / "calibration.json"
    cal_bytes = json.dumps(cal).encode("utf-8")
    cal_path.write_bytes(cal_bytes)

    image_args: list[str] = []
    for idx, cam in enumerate(cal["cameras"]):
        cid = cam["camera_id"]
        p = tmp_path / f"{cid}.jpg"
        p.write_bytes(_encode_jpeg(32, 24, seed=200 + idx))
        image_args.extend(["--image", f"{cid}={p}"])

    out_path = tmp_path / "job_input.json"
    worker_token = "worker-secret-token-abc"

    with _FakeUploadServer(expected_token=worker_token, bucket="simai-prod-bucket") as server:
        rc = run_cli(
            [
                *image_args,
                "--calibration",
                str(cal_path),
                "--upload-calibration",
                "--output",
                str(out_path),
                "--server",
                server.base_url,
                "--frame-source",
                "DATASET",
                "--dataset-id",
                "cmu_panoptic/160422_ultimatum1",
            ],
            env={"SIMAI_WORKER_TOKEN": worker_token},
        )
        assert rc == 0
        # 보정 1회 + 카메라 4대 = 5회 업로드
        assert len(server.sign_requests) == 5
        assert len(server.put_requests) == 5
        assert server.sign_requests[0]["body"]["purpose"] == "AVATAR_ASSET"
        assert server.sign_requests[0]["body"]["content_type"] == "application/octet-stream"
        assert server.put_requests[0]["bytes"] == cal_bytes

    job_input = json.loads(out_path.read_text(encoding="utf-8"))
    validate_input(job_input)
    assert job_input["calibration_ref"].startswith("s3://simai-prod-bucket/avatars/")
    assert job_input["input_provenance"]["is_upper_bound"] is True
    assert job_input["input_provenance"]["frames"]["dataset_id"] == "cmu_panoptic/160422_ultimatum1"


def test_stop_immediately_and_do_not_write_output_on_upload_or_validation_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """서명 실패·PUT 실패·해시 불일치·미완료 묶음 등 실패 시 즉시 멈추고 출력 파일을 쓰지 않으며 비밀 값을 노출하지 않는다."""
    cal = _make_calibration_doc()
    cal_path = tmp_path / "calibration.json"
    cal_path.write_bytes(json.dumps(cal).encode("utf-8"))
    bundle_root, bundle_dir, _ = _write_clean_bg_bundle(tmp_path / "clean-background", cal)
    secret_token = "super-secret-token-never-leak"

    # 1) 2번째 카메라 PUT 에서 HTTP 500 실패 -> 즉시 중단(3·4번째 서명/PUT 호출 안 함), 출력 파일 없음
    out_put_fail = tmp_path / "out_put_fail.json"
    with _FakeUploadServer(expected_token=secret_token, fail_put_on_call=2) as server:
        rc = run_cli(
            [
                "--bundle",
                str(bundle_root),
                "--calibration",
                str(cal_path),
                "--calibration-ref",
                "s3://simai-prod-bucket/calibrations/cal-room-01.json",
                "--output",
                str(out_put_fail),
                "--server",
                server.base_url,
            ],
            env={"SIMAI_UPLOAD_TOKEN": secret_token},
        )
        assert rc == 2
        assert not out_put_fail.exists()
        assert len(server.sign_requests) == 2
        assert len(server.put_requests) == 2

    captured = capsys.readouterr()
    assert secret_token not in captured.out
    assert secret_token not in captured.err
    assert "presigned-secret-sig-12345" not in captured.err

    # 2) 서명 요청 401 실패 -> PUT 0회, 출력 파일 없음
    out_sign_fail = tmp_path / "out_sign_fail.json"
    with _FakeUploadServer(expected_token="different-token") as server:
        rc = run_cli(
            [
                "--bundle",
                str(bundle_root),
                "--calibration",
                str(cal_path),
                "--calibration-ref",
                "s3://simai-prod-bucket/calibrations/cal-room-01.json",
                "--output",
                str(out_sign_fail),
                "--server",
                server.base_url,
            ],
            env={"SIMAI_UPLOAD_TOKEN": secret_token},
        )
        assert rc == 2
        assert not out_sign_fail.exists()
        assert len(server.put_requests) == 0

    captured = capsys.readouterr()
    assert secret_token not in captured.err

    # 3) status.json 없이 상위 폴더만 넘긴 경우 bundle-* 글로빙 금지
    (bundle_root / "status.json").unlink()
    out_no_status = tmp_path / "out_no_status.json"
    rc = run_cli(
        [
            "--bundle",
            str(bundle_root),
            "--calibration",
            str(cal_path),
            "--output",
            str(out_no_status),
            "--dry-run",
        ],
        env={},
    )
    assert rc == 2
    assert not out_no_status.exists()

    # 4) 이미지 바이트 변조(clean_backgrounds.json 의 image_sha256 불일치) -> 로컬 검증에서 즉시 거절
    (bundle_dir / "cam-1.jpg").write_bytes(_encode_jpeg(32, 24, seed=999))
    out_tampered = tmp_path / "out_tampered.json"
    rc = run_cli(
        [
            "--bundle",
            str(bundle_dir),
            "--calibration",
            str(cal_path),
            "--output",
            str(out_tampered),
            "--dry-run",
        ],
        env={},
    )
    assert rc == 2
    assert not out_tampered.exists()


def test_images_dir_token_file_and_contract_mismatch_guards(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--images-dir` + `--token-file` 정상 경로와 버킷 불일치·해상도 불일치·평문 원격 HTTP 거절을 검증한다."""
    cal = _make_calibration_doc()
    cal_path = tmp_path / "calibration.json"
    cal_path.write_bytes(json.dumps(cal).encode("utf-8"))

    imgs_dir = tmp_path / "imgs"
    imgs_dir.mkdir()
    for idx, cam in enumerate(cal["cameras"]):
        cid = cam["camera_id"]
        (imgs_dir / f"{cid}.jpg").write_bytes(_encode_jpeg(32, 24, seed=300 + idx))

    token_file = tmp_path / "token.txt"
    secret = "file-secret-token-777"
    token_file.write_text(secret + "\n", encoding="utf-8")

    # 1) --calibration-ref 버킷과 업로드 버킷이 다르면 출력 파일을 쓰지 않는다
    out_bucket_mismatch = tmp_path / "out_bucket_mismatch.json"
    with _FakeUploadServer(expected_token=secret, bucket="simai-prod-bucket") as server:
        rc = run_cli(
            [
                "--images-dir",
                str(imgs_dir),
                "--calibration",
                str(cal_path),
                "--calibration-ref",
                "s3://other-bucket-name/calibrations/cal-room-01.json",
                "--output",
                str(out_bucket_mismatch),
                "--server",
                server.base_url,
                "--token-file",
                str(token_file),
            ],
            env={},
        )
        assert rc == 2
        assert not out_bucket_mismatch.exists()

    # 2) 평문 원격 HTTP 주소 거절
    out_http = tmp_path / "out_http.json"
    rc = run_cli(
        [
            "--images-dir",
            str(imgs_dir),
            "--calibration",
            str(cal_path),
            "--calibration-ref",
            "s3://simai-prod-bucket/calibrations/cal-room-01.json",
            "--output",
            str(out_http),
            "--server",
            "http://example.com:8080",
            "--token-file",
            str(token_file),
        ],
        env={},
    )
    assert rc == 2
    assert not out_http.exists()

    # 3) --images-dir + --token-file 정상 업로드
    out_ok = tmp_path / "out_ok.json"
    with _FakeUploadServer(expected_token=secret, bucket="simai-prod-bucket") as server:
        rc = run_cli(
            [
                "--images-dir",
                str(imgs_dir),
                "--calibration",
                str(cal_path),
                "--calibration-ref",
                "s3://simai-prod-bucket/calibrations/cal-room-01.json",
                "--output",
                str(out_ok),
                "--server",
                server.base_url,
                "--token-file",
                str(token_file),
            ],
            env={},
        )
        assert rc == 0
        assert out_ok.is_file()

    # 4) 해상도 불일치 시 거절
    (imgs_dir / "cam-1.jpg").write_bytes(_encode_jpeg(64, 48, seed=400))
    out_res = tmp_path / "out_res.json"
    rc = run_cli(
        [
            "--images-dir",
            str(imgs_dir),
            "--calibration",
            str(cal_path),
            "--output",
            str(out_res),
            "--dry-run",
        ],
        env={},
    )
    assert rc == 2
    assert not out_res.exists()

    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err


def test_purpose_selection_and_token_mismatch_guards(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`UploadPurpose` 규칙: `--upload-calibration` 은 워커 토큰·`AVATAR_ASSET` 기본, 토큰·용도 불일치 시 업로드 전 중단."""
    cal = _make_calibration_doc()
    cal_path = tmp_path / "calibration.json"
    cal_path.write_bytes(json.dumps(cal).encode("utf-8"))
    bundle_root, _, _ = _write_clean_bg_bundle(tmp_path / "clean-background", cal)

    tier0_tok = "tier0-secret-tok-111"
    worker_tok = "worker-secret-tok-222"

    # 1) --upload-calibration 시 SIMAI_TIER0_TOKEN 과 SIMAI_WORKER_TOKEN 이 함께 있어도 워커 토큰·AVATAR_ASSET 하나로 모두 올린다
    out_both = tmp_path / "out_both.json"
    with _FakeUploadServer(expected_token=worker_tok, bucket="simai-prod-bucket") as server:
        rc = run_cli(
            [
                "--bundle",
                str(bundle_root),
                "--calibration",
                str(cal_path),
                "--upload-calibration",
                "--output",
                str(out_both),
                "--server",
                server.base_url,
            ],
            env={"SIMAI_TIER0_TOKEN": tier0_tok, "SIMAI_WORKER_TOKEN": worker_tok},
        )
        assert rc == 0
        assert out_both.is_file()
        assert len(server.sign_requests) == 5
        assert all(req["body"]["purpose"] == "AVATAR_ASSET" for req in server.sign_requests)
        assert server.sign_requests[0]["body"]["content_type"] == "application/octet-stream"

    # 2) --upload-calibration 인데 SIMAI_TIER0_TOKEN 만 있으면 업로드 전에 멈춘다
    out_mismatch_1 = tmp_path / "out_mismatch_1.json"
    with _FakeUploadServer(expected_token=tier0_tok) as server:
        rc = run_cli(
            [
                "--bundle",
                str(bundle_root),
                "--calibration",
                str(cal_path),
                "--upload-calibration",
                "--output",
                str(out_mismatch_1),
                "--server",
                server.base_url,
            ],
            env={"SIMAI_TIER0_TOKEN": tier0_tok},
        )
        assert rc == 2
        assert not out_mismatch_1.exists()
        assert len(server.sign_requests) == 0
    err_1 = capsys.readouterr().err
    assert "AVATAR_ASSET" in err_1 and "SIMAI_WORKER_TOKEN" in err_1

    # 3) --upload-calibration 과 --purpose EVENT_FRAME 충돌 시 업로드 전에 멈춘다
    out_mismatch_2 = tmp_path / "out_mismatch_2.json"
    with _FakeUploadServer(expected_token=worker_tok) as server:
        rc = run_cli(
            [
                "--bundle",
                str(bundle_root),
                "--calibration",
                str(cal_path),
                "--upload-calibration",
                "--purpose",
                "EVENT_FRAME",
                "--output",
                str(out_mismatch_2),
                "--server",
                server.base_url,
            ],
            env={"SIMAI_WORKER_TOKEN": worker_tok},
        )
        assert rc == 2
        assert not out_mismatch_2.exists()
        assert len(server.sign_requests) == 0
    err_2 = capsys.readouterr().err
    assert "AVATAR_ASSET" in err_2

    # 4) --purpose AVATAR_ASSET 인데 SIMAI_TIER0_TOKEN 만 있으면 업로드 전에 멈춘다
    out_mismatch_3 = tmp_path / "out_mismatch_3.json"
    with _FakeUploadServer(expected_token=tier0_tok) as server:
        rc = run_cli(
            [
                "--bundle",
                str(bundle_root),
                "--calibration",
                str(cal_path),
                "--calibration-ref",
                "s3://simai-prod-bucket/calibrations/cal-room-01.json",
                "--purpose",
                "AVATAR_ASSET",
                "--output",
                str(out_mismatch_3),
                "--server",
                server.base_url,
            ],
            env={"SIMAI_TIER0_TOKEN": tier0_tok},
        )
        assert rc == 2
        assert not out_mismatch_3.exists()
        assert len(server.sign_requests) == 0
    err_3 = capsys.readouterr().err
    assert "AVATAR_ASSET" in err_3

    # 5) --purpose EVENT_FRAME 인데 SIMAI_WORKER_TOKEN 만 있으면 업로드 전에 멈춘다
    out_mismatch_4 = tmp_path / "out_mismatch_4.json"
    with _FakeUploadServer(expected_token=worker_tok) as server:
        rc = run_cli(
            [
                "--bundle",
                str(bundle_root),
                "--calibration",
                str(cal_path),
                "--calibration-ref",
                "s3://simai-prod-bucket/calibrations/cal-room-01.json",
                "--purpose",
                "EVENT_FRAME",
                "--output",
                str(out_mismatch_4),
                "--server",
                server.base_url,
            ],
            env={"SIMAI_WORKER_TOKEN": worker_tok},
        )
        assert rc == 2
        assert not out_mismatch_4.exists()
        assert len(server.sign_requests) == 0
    err_4 = capsys.readouterr().err
    assert "EVENT_FRAME" in err_4


def test_ec2_calibration_version_registration_check(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """EC2 `GET /api/calibrations/current` 로 `calibration_version` 을 확인한다. 현재·최근이 아니면 경고만 하고 진행한다."""
    cal = _make_calibration_doc(calibration_version="cal-room-01")
    cal_path = tmp_path / "calibration.json"
    cal_path.write_bytes(json.dumps(cal).encode("utf-8"))
    bundle_root, _, _ = _write_clean_bg_bundle(tmp_path / "clean-background", cal)
    worker_tok = "worker-secret-tok-333"

    # 1) EC2 현재·최근이 다른 보정일 때 -> 등록돼 있지만 현재가 아닐 수 있어(9차 266z) 경고만 하고 진행한다
    out_unregistered = tmp_path / "out_unregistered.json"
    with _FakeUploadServer(
        expected_token=worker_tok,
        current_calibration_version="cal-other-99",
        latest_uploaded_version="cal-other-99",
    ) as server:
        rc = run_cli(
            [
                "--bundle",
                str(bundle_root),
                "--calibration",
                str(cal_path),
                "--upload-calibration",
                "--output",
                str(out_unregistered),
                "--server",
                server.base_url,
            ],
            env={"SIMAI_WORKER_TOKEN": worker_tok},
        )
        assert rc == 0
        assert out_unregistered.exists()
        assert len(server.calibration_requests) == 1
        assert len(server.sign_requests) > 0
    err_unreg = capsys.readouterr().err
    assert "경고: EC2 의 현재·최근 보정이 아니다(cal-other-99)" in err_unreg
    assert (
        "EC2 에 등록된 보정만 쓸 수 있다(SceneJobListener 가 결과를 거절). "
        "등록된 보정 문서를 쓰거나 simai_ai.calibration.publish 로 먼저 올린다"
    ) in err_unreg

    # 2) --skip-calibration-check 지정 시 경고만 남기고 진행한다
    out_skipped = tmp_path / "out_skipped.json"
    with _FakeUploadServer(
        expected_token=worker_tok,
        current_calibration_version="cal-other-99",
        latest_uploaded_version="cal-other-99",
    ) as server:
        rc = run_cli(
            [
                "--bundle",
                str(bundle_root),
                "--calibration",
                str(cal_path),
                "--upload-calibration",
                "--skip-calibration-check",
                "--output",
                str(out_skipped),
                "--server",
                server.base_url,
            ],
            env={"SIMAI_WORKER_TOKEN": worker_tok},
        )
        assert rc == 0
        assert out_skipped.is_file()
        assert len(server.calibration_requests) == 0
        assert len(server.sign_requests) == 5
    err_skip = capsys.readouterr().err
    assert "--skip-calibration-check" in err_skip

    # 3) 워커 토큰으로 GET /api/calibrations/current 가 401 을 반환할 때도 경고만 남기고 진행한다
    out_401 = tmp_path / "out_401.json"
    with _FakeUploadServer(
        expected_token=worker_tok,
        calibration_status=401,
    ) as server:
        rc = run_cli(
            [
                "--bundle",
                str(bundle_root),
                "--calibration",
                str(cal_path),
                "--upload-calibration",
                "--output",
                str(out_401),
                "--server",
                server.base_url,
            ],
            env={"SIMAI_WORKER_TOKEN": worker_tok},
        )
        assert rc == 0
        assert out_401.is_file()
        assert len(server.calibration_requests) == 1
        assert len(server.sign_requests) == 5
    err_401 = capsys.readouterr().err
    assert "경고" in err_401
