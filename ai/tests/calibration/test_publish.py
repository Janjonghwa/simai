"""보정 문서 EC2 업로드·구성 묶음 발행 CLI 시험 (S15P31A101-332, 명세 3.2·4.5·12.6)."""

from __future__ import annotations

import copy
import hashlib
import http.server
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from simai_ai.calibration.document import content_hash
from simai_ai.calibration.publish import (
    ADMIN_PASSWORD_ENV,
    ADMIN_USER_ENV,
    EXIT_OK,
    EXIT_SERVER,
    EXIT_VALIDATION,
    PublishValidationError,
    load_and_validate_document,
    main,
)
from simai_ai.providers.testing import load_example

SECRET_USER = "ops-admin-secret-id"
SECRET_PASSWORD = "ops-super-secret-password-!@#"
SECRET_TOKEN = "jwt-secret-access-token-xyz987"


def _make_valid_doc(
    *,
    calibration_version: str = "cal-2026-04-20-002",
    site_id: str = "site-classroom-01",
    state: str = "CALIBRATED",
) -> dict[str, Any]:
    doc = copy.deepcopy(load_example("calibration.g1_auto.json"))
    doc["calibration_version"] = calibration_version
    doc["coordinate_frame"]["site_id"] = site_id
    doc["state"] = state
    doc["cameras"][0]["K"][0][0] = 1000.0
    doc["content_hash"] = content_hash(doc)
    return doc


def _write_doc_file(tmp_path: Path, doc: dict[str, Any], name: str = "calibration.json") -> Path:
    text = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    path = tmp_path / name
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


class FakeSimaiServer:
    """표준 라이브러리 http.server 기반 가짜 EC2 서버."""

    def __init__(
        self,
        *,
        site_id: str = "site-classroom-01",
        current_version: str | None = "cal-2026-04-20-001",
        latest_uploaded_version: str | None = "cal-2026-04-20-001",
        user_role: str = "ADMIN",
    ) -> None:
        self.site_id = site_id
        self.current_version = current_version
        self.latest_uploaded_version = latest_uploaded_version
        self.current_state = "CALIBRATED" if current_version else "UNINITIALIZED"
        self.user_role = user_role

        self.requests: list[dict[str, Any]] = []
        self.uploaded_documents: list[dict[str, Any]] = []
        self.published_bundles: list[dict[str, Any]] = []
        self.forced_errors: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}
        self.override_bundle_calibration_version: str | None = None
        self.confirm_current_hide_upload: bool = False
        self.confirm_bundles_empty: bool = False

        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def _dispatch(self, method: str) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(length) if length > 0 else b""
                parsed_body = json.loads(raw_body.decode("utf-8")) if raw_body else None
                auth_header = self.headers.get("Authorization")
                outer.requests.append(
                    {
                        "method": method,
                        "path": self.path,
                        "auth": auth_header,
                        "body": parsed_body,
                    }
                )

                forced = outer.forced_errors.get((method, self.path))
                if forced is not None:
                    status, payload = forced
                    self._send_json(status, payload)
                    return

                if (method, self.path) == ("POST", "/api/auth/login"):
                    if not isinstance(parsed_body, dict) or (
                        parsed_body.get("username") != SECRET_USER
                        or parsed_body.get("password") != SECRET_PASSWORD
                    ):
                        self._send_json(
                            401,
                            {"code": "INVALID_CREDENTIALS", "detail": "아이디 또는 비밀번호가 올바르지 않습니다."},
                        )
                        return
                    self._send_json(
                        200,
                        {
                            "accessToken": SECRET_TOKEN,
                            "tokenType": "Bearer",
                            "expiresIn": 3600,
                            "user": {
                                "id": "admin-uuid-1",
                                "role": outer.user_role,
                                "displayLabel": "관리자",
                            },
                        },
                    )
                    return

                if auth_header != f"Bearer {SECRET_TOKEN}":
                    self._send_json(401, {"code": "UNAUTHORIZED", "detail": "인증이 필요합니다."})
                    return

                if (method, self.path) == ("GET", "/api/calibrations/current"):
                    self._send_json(
                        200,
                        {
                            "site_id": outer.site_id,
                            "calibration_state": outer.current_state,
                            "calibration_version": outer.current_version,
                            "previous_calibration_version": None,
                            "provider_source": "G1_AUTO" if outer.current_version else None,
                            "reason": "initial",
                            "effective_at": 1776000000000,
                            "updated_at": "2026-04-20T00:00:00Z",
                            "cameras": [],
                            "document_uploaded": outer.latest_uploaded_version is not None,
                            "latest_uploaded_version": outer.latest_uploaded_version,
                        },
                    )
                    return

                if (method, self.path) == ("POST", "/api/calibrations"):
                    if outer.user_role != "ADMIN":
                        self._send_json(403, {"code": "FORBIDDEN", "detail": "권한이 없습니다."})
                        return
                    doc_text = parsed_body.get("document_text") if isinstance(parsed_body, dict) else None
                    if not isinstance(doc_text, str) or not doc_text.strip():
                        self._send_json(
                            400,
                            {"code": "CALIBRATION_DOCUMENT_INVALID", "detail": "document_text 가 필요하다"},
                        )
                        return
                    doc = json.loads(doc_text)
                    doc_site = doc.get("coordinate_frame", {}).get("site_id")
                    if doc_site != outer.site_id:
                        self._send_json(
                            400,
                            {
                                "code": "CALIBRATION_SITE_MISMATCH",
                                "detail": f"보정 문서의 site_id({doc_site})가 현장({outer.site_id})과 다릅니다.",
                            },
                        )
                        return
                    cal_ver = doc["calibration_version"]
                    if cal_ver == outer.latest_uploaded_version or cal_ver == outer.current_version:
                        self._send_json(
                            409,
                            {
                                "code": "CALIBRATION_VERSION_CONFLICT",
                                "detail": f"보정 버전 {cal_ver} 은 이미 다른 내용으로 등록되어 있습니다.",
                            },
                        )
                        return
                    sha = "sha256:" + hashlib.sha256(doc_text.encode("utf-8")).hexdigest()
                    upload_record = {
                        "calibration_version": cal_ver,
                        "schema_version": doc["schema_version"],
                        "provider_source": doc["provider_source"],
                        "provider_version": doc["provider_version"],
                        "state": doc["state"],
                        "scale_basis": doc["scale_basis"],
                        "content_hash": doc["content_hash"],
                        "sha256": sha,
                        "reason": parsed_body.get("reason") or "admin calibration upload",
                        "created_at": doc["created_at"],
                        "uploaded_at": "2026-04-20T01:02:03Z",
                        "raw_document_text": doc_text,
                    }
                    outer.uploaded_documents.append(upload_record)
                    if not outer.confirm_current_hide_upload:
                        outer.latest_uploaded_version = cal_ver
                        outer.current_version = cal_ver
                        outer.current_state = doc["state"]
                    self._send_json(
                        200,
                        {k: v for k, v in upload_record.items() if k != "raw_document_text"},
                    )
                    return

                if (method, self.path) == ("POST", "/api/config-bundles"):
                    if outer.user_role != "ADMIN":
                        self._send_json(403, {"code": "FORBIDDEN", "detail": "권한이 없습니다."})
                        return
                    if not outer.uploaded_documents and not outer.current_version:
                        self._send_json(
                            409,
                            {"code": "CURRENT_CALIBRATION_MISSING", "detail": "적용할 보정 문서가 없습니다."},
                        )
                        return
                    cal_ver = (
                        outer.override_bundle_calibration_version
                        or outer.current_version
                        or outer.uploaded_documents[-1]["calibration_version"]
                    )
                    cfg_ver = f"cfg-2026-04-20-{len(outer.published_bundles) + 1:03d}"
                    rule_profile = (
                        parsed_body.get("rule_profile")
                        if isinstance(parsed_body, dict) and parsed_body.get("rule_profile")
                        else "OPERATION"
                    )
                    bundle = {
                        "config_version": cfg_ver,
                        "bundle_hash": "sha256:" + "b" * 64,
                        "zone_version": "zone-2026-04-20-001",
                        "calibration_version": cal_ver,
                        "rule_profile": rule_profile,
                        "status": "PUBLISHED",
                        "applied_config_version": None,
                        "error_code": None,
                        "issued_at": "2026-04-20T01:02:04Z",
                        "ack_at": None,
                    }
                    outer.published_bundles.insert(0, bundle)
                    self._send_json(200, bundle)
                    return

                if (method, self.path) == ("GET", "/api/config-bundles"):
                    if outer.confirm_bundles_empty:
                        self._send_json(200, [])
                    else:
                        self._send_json(200, list(outer.published_bundles))
                    return

                self._send_json(404, {"code": "NOT_FOUND", "detail": self.path})

            def do_GET(self) -> None:  # noqa: N802
                self._dispatch("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._dispatch("POST")

            def _send_json(self, status: int, payload: Any) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def write_requests(self) -> list[dict[str, Any]]:
        return [
            r
            for r in self.requests
            if r["method"] == "POST" and r["path"] in ("/api/calibrations", "/api/config-bundles")
        ]

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2.0)


@pytest.fixture
def fake_server():
    srv = FakeSimaiServer()
    try:
        yield srv
    finally:
        srv.close()


@pytest.fixture
def admin_env() -> dict[str, str]:
    return {
        ADMIN_USER_ENV: SECRET_USER,
        ADMIN_PASSWORD_ENV: SECRET_PASSWORD,
    }


def _assert_no_secrets_leaked(out: str, err: str) -> None:
    for secret in (SECRET_USER, SECRET_PASSWORD, SECRET_TOKEN):
        assert secret not in out
        assert secret not in err


def test_publish_success_uploads_document_text_and_publishes_bundle(
    tmp_path: Path,
    fake_server: FakeSimaiServer,
    admin_env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    doc = _make_valid_doc(calibration_version="cal-2026-04-20-002")
    doc_path = _write_doc_file(tmp_path, doc)

    rc = main(
        [
            str(doc_path),
            "--server",
            fake_server.url,
            "--site-id",
            "site-classroom-01",
            "--reason",
            "walk calibration update",
            "--rule-profile",
            "CLASSROOM_TEST",
        ],
        env=admin_env,
    )
    captured = capsys.readouterr()
    assert rc == EXIT_OK
    assert captured.err == ""
    _assert_no_secrets_leaked(captured.out, captured.err)

    lines = captured.out.strip().splitlines()
    assert len(lines) == 1
    summary = json.loads(lines[0])
    assert summary["dry_run"] is False
    assert summary["calibration_version"] == "cal-2026-04-20-002"
    assert summary["config_version"] == "cfg-2026-04-20-001"
    assert summary["site_id"] == "site-classroom-01"
    assert summary["state"] == "CALIBRATED"
    assert summary["content_hash"] == doc["content_hash"]
    assert summary["server"]["upload"]["calibration_version"] == "cal-2026-04-20-002"
    assert summary["server"]["bundle"]["config_version"] == "cfg-2026-04-20-001"
    assert summary["server"]["bundle"]["rule_profile"] == "CLASSROOM_TEST"
    assert summary["server"]["current_calibration"]["calibration_version"] == "cal-2026-04-20-002"
    assert summary["server"]["current_calibration"]["document_uploaded"] is True

    # 원문 텍스트가 재직렬화 없이 그대로 전달되어 1000.0 실수 표기가 보존되었는지 확인한다.
    assert len(fake_server.uploaded_documents) == 1
    assert fake_server.uploaded_documents[0]["raw_document_text"] == doc_path.read_text(encoding="utf-8")
    assert "1000.0" in fake_server.uploaded_documents[0]["raw_document_text"]

    # 호출 순서 확인: login -> GET current -> POST calibrations -> POST config-bundles -> GET current -> GET config-bundles
    assert [(r["method"], r["path"]) for r in fake_server.requests] == [
        ("POST", "/api/auth/login"),
        ("GET", "/api/calibrations/current"),
        ("POST", "/api/calibrations"),
        ("POST", "/api/config-bundles"),
        ("GET", "/api/calibrations/current"),
        ("GET", "/api/config-bundles"),
    ]


def test_dry_run_performs_checks_without_writing_to_server(
    tmp_path: Path,
    fake_server: FakeSimaiServer,
    admin_env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    doc = _make_valid_doc(calibration_version="cal-2026-04-20-002")
    doc_path = _write_doc_file(tmp_path, doc)

    rc = main(
        [str(doc_path), "--server", fake_server.url, "--dry-run"],
        env=admin_env,
    )
    captured = capsys.readouterr()
    assert rc == EXIT_OK
    assert captured.err == ""
    _assert_no_secrets_leaked(captured.out, captured.err)

    summary = json.loads(captured.out.strip())
    assert summary["dry_run"] is True
    assert summary["calibration_version"] == "cal-2026-04-20-002"
    assert summary["config_version"] is None
    assert summary["server"]["current_calibration"]["calibration_version"] == "cal-2026-04-20-001"

    assert fake_server.write_requests == []
    assert fake_server.uploaded_documents == []
    assert fake_server.published_bundles == []
    assert [(r["method"], r["path"]) for r in fake_server.requests] == [
        ("POST", "/api/auth/login"),
        ("GET", "/api/calibrations/current"),
    ]


def test_schema_and_contract_errors_fail_before_network(
    tmp_path: Path,
    fake_server: FakeSimaiServer,
    admin_env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    # 1) 스키마 위반: cameras 빈 배열
    bad_schema = _make_valid_doc()
    bad_schema["cameras"] = []
    bad_schema_path = _write_doc_file(tmp_path, bad_schema, "bad_schema.json")

    rc = main([str(bad_schema_path), "--server", fake_server.url], env=admin_env)
    captured = capsys.readouterr()
    assert rc == EXIT_VALIDATION
    assert "스키마 위반" in captured.err
    assert fake_server.requests == []

    # 2) 계약 상태 위반: CALIBRATED 인데 MODEL_ESTIMATE 사용 (명세 4.4 불변식 위반)
    bad_contract = _make_valid_doc()
    bad_contract["scale_basis"] = "MODEL_ESTIMATE"
    bad_contract["content_hash"] = content_hash(bad_contract)
    bad_contract_path = _write_doc_file(tmp_path, bad_contract, "bad_contract.json")

    rc = main([str(bad_contract_path), "--server", fake_server.url], env=admin_env)
    captured = capsys.readouterr()
    assert rc == EXIT_VALIDATION
    assert "계약 검사 위반" in captured.err
    assert fake_server.requests == []


def test_content_hash_mismatch_fails_before_network(
    tmp_path: Path,
    fake_server: FakeSimaiServer,
    admin_env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    doc = _make_valid_doc()
    doc["content_hash"] = "sha256:" + "0" * 64
    doc_path = _write_doc_file(tmp_path, doc)

    rc = main([str(doc_path), "--server", fake_server.url], env=admin_env)
    captured = capsys.readouterr()
    assert rc == EXIT_VALIDATION
    assert "content_hash 불일치" in captured.err
    assert fake_server.requests == []


def test_site_mismatch_blocks_upload(
    tmp_path: Path,
    fake_server: FakeSimaiServer,
    admin_env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    doc = _make_valid_doc(site_id="site-other-building")
    doc_path = _write_doc_file(tmp_path, doc)

    # 1) --site-id 와 문서 site_id 가 다르면 네트워크 호출 전에 즉시 중단
    rc = main(
        [str(doc_path), "--server", fake_server.url, "--site-id", "site-classroom-01"],
        env=admin_env,
    )
    captured = capsys.readouterr()
    assert rc == EXIT_VALIDATION
    assert "site_id" in captured.err
    assert fake_server.requests == []

    # 2) 서버 GET /api/calibrations/current 의 site_id 와 문서 site_id 가 다르면 POST 전에 중단
    rc = main([str(doc_path), "--server", fake_server.url], env=admin_env)
    captured = capsys.readouterr()
    assert rc == EXIT_VALIDATION
    assert "site_id" in captured.err
    assert fake_server.write_requests == []

    # 3) 서버가 GET 에서는 site_id 를 안 돌려주더라도 POST /api/calibrations 에서 400 CALIBRATION_SITE_MISMATCH 를 내면 구성 묶음 발행 없이 중단
    fake_server.requests.clear()
    fake_server.site_id = ""
    fake_server.forced_errors[("POST", "/api/calibrations")] = (
        400,
        {"code": "CALIBRATION_SITE_MISMATCH", "detail": "현장 불일치"},
    )
    rc = main([str(doc_path), "--server", fake_server.url], env=admin_env)
    captured = capsys.readouterr()
    assert rc == EXIT_VALIDATION
    assert "CALIBRATION_SITE_MISMATCH" in captured.err
    assert [r["path"] for r in fake_server.write_requests] == ["/api/calibrations"]


def test_same_version_as_server_current_blocks_upload(
    tmp_path: Path,
    fake_server: FakeSimaiServer,
    admin_env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    # 서버 현재 버전(cal-2026-04-20-001)과 동일한 버전의 문서를 올리려 하면 업로드 전에 중단한다.
    same_ver_doc = _make_valid_doc(calibration_version="cal-2026-04-20-001")
    doc_path = _write_doc_file(tmp_path, same_ver_doc)

    rc = main([str(doc_path), "--server", fake_server.url], env=admin_env)
    captured = capsys.readouterr()
    assert rc == EXIT_VALIDATION
    assert "같은 버전" in captured.err
    assert fake_server.write_requests == []
    assert fake_server.uploaded_documents == []


@pytest.mark.parametrize(
    ("target", "status", "code"),
    [
        (("POST", "/api/auth/login"), 401, "INVALID_CREDENTIALS"),
        (("POST", "/api/calibrations"), 403, "FORBIDDEN"),
        (("POST", "/api/calibrations"), 500, "INTERNAL_SERVER_ERROR"),
        (("POST", "/api/config-bundles"), 409, "CURRENT_CALIBRATION_MISSING"),
        (("POST", "/api/config-bundles"), 503, "SERVICE_UNAVAILABLE"),
        (("POST", "/api/config-bundles"), 500, "INTERNAL_SERVER_ERROR"),
    ],
)
def test_server_4xx_5xx_errors_abort_without_leaking_secrets(
    tmp_path: Path,
    fake_server: FakeSimaiServer,
    admin_env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
    target: tuple[str, str],
    status: int,
    code: str,
) -> None:
    doc = _make_valid_doc(calibration_version="cal-2026-04-20-002")
    doc_path = _write_doc_file(tmp_path, doc)
    fake_server.forced_errors[target] = (status, {"code": code, "detail": "오류 발생"})

    rc = main([str(doc_path), "--server", fake_server.url], env=admin_env)
    captured = capsys.readouterr()
    assert rc == EXIT_SERVER
    assert captured.out == ""
    assert f"HTTP {status}" in captured.err
    assert code in captured.err
    _assert_no_secrets_leaked(captured.out, captured.err)

    if target[1] in ("/api/auth/login", "/api/calibrations"):
        assert fake_server.published_bundles == []


def test_bundle_calibration_version_mismatch_is_reported_as_server_error(
    tmp_path: Path,
    fake_server: FakeSimaiServer,
    admin_env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    doc = _make_valid_doc(calibration_version="cal-2026-04-20-002")
    doc_path = _write_doc_file(tmp_path, doc)
    fake_server.override_bundle_calibration_version = "cal-2026-04-20-001"

    rc = main([str(doc_path), "--server", fake_server.url], env=admin_env)
    captured = capsys.readouterr()
    assert rc == EXIT_SERVER
    assert "calibration_version" in captured.err
    _assert_no_secrets_leaked(captured.out, captured.err)


def test_missing_admin_env_or_invalid_server_url_fails_fast(
    tmp_path: Path,
    fake_server: FakeSimaiServer,
    admin_env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    doc = _make_valid_doc(calibration_version="cal-2026-04-20-002")
    doc_path = _write_doc_file(tmp_path, doc)

    # 환경 변수 누락
    rc = main([str(doc_path), "--server", fake_server.url], env={})
    captured = capsys.readouterr()
    assert rc == EXIT_VALIDATION
    assert ADMIN_USER_ENV in captured.err
    assert ADMIN_PASSWORD_ENV in captured.err
    assert fake_server.requests == []

    # 잘못된 서버 URL
    rc = main([str(doc_path), "--server", "not-a-url"], env=admin_env)
    captured = capsys.readouterr()
    assert rc == EXIT_VALIDATION
    assert "--server" in captured.err
    assert fake_server.requests == []


def test_load_and_validate_rejects_non_finite_json_numbers(tmp_path: Path) -> None:
    bad_file = tmp_path / "nan.json"
    bad_file.write_text('{"schema_version": NaN}', encoding="utf-8")
    with pytest.raises(PublishValidationError, match="NaN"):
        load_and_validate_document(bad_file)
