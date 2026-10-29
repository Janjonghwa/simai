"""보정 문서를 EC2 에 올리고 구성 묶음을 발행하는 CLI (S15P31A101-332, 명세 3.2·4.5·12.6).

회차 영상 보정(-331)·걷기 보정(`simai_ai.calibration.extrinsics.cli`)·World 고정(`simai_ai.calibration.world_frame.cli`)이
낸 보정 문서(`contracts/providers/calibration.schema.json`)를 검사한 뒤 EC2 서버에 올리고(`POST /api/calibrations`),
Tier 0 판정에 반영되도록 구성 묶음을 발행(`POST /api/config-bundles`)한다.

`ai/` 에서 실행:

    SIMAI_ADMIN_USER=admin SIMAI_ADMIN_PASSWORD=... \\
        uv run python -m simai_ai.calibration.publish <calibration.json> --server https://<ec2-host>

규칙:
- `--server` 는 기본값을 두지 않는 필수 인자다(실수로 운영 서버에 올리지 않게 한다).
- 관리자 계정은 환경 변수 `SIMAI_ADMIN_USER`·`SIMAI_ADMIN_PASSWORD` 에서만 읽는다. 명령줄 인자로 받지 않고,
  계정·비밀번호·JWT 토큰 값은 표준 출력·표준 오류·예외 문장에 남기지 않는다.
- 올리기 전 검사:
  1) 계약 스키마(`contracts/providers/calibration.schema.json`)와 기하·상태 유효성(`check_calibration`)
  2) `content_hash` 재계산(`simai_ai.calibration.document.content_hash`) 일치 여부
  3) 문서 `coordinate_frame.site_id` 존재 여부 및 서버 현장 `site_id`(`--site-id` 또는 서버 응답)와의 일치 여부
  4) 서버의 현재 보정 버전(`GET /api/calibrations/current` 의 `calibration_version`·`latest_uploaded_version`)과
     같은 버전이면 덮어쓰지 않고 멈춘다(AGENTS 규칙 6).
- 보정 문서 원문은 JSON 재직렬화로 실수 표기(`1000.0` → `1000`)가 바뀌지 않도록 `document_text` 로 글자 그대로 보낸다.
- `--dry-run` 은 로컬 검사와 서버 읽기 조회(`GET /api/calibrations/current`)까지만 수행하고 서버에 쓰지 않는다.
"""

from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from simai_ai.calibration.document import content_hash
from simai_ai.calibration.structure import check_calibration_structure
from simai_ai.providers.contract import check_calibration

ADMIN_USER_ENV = "SIMAI_ADMIN_USER"
ADMIN_PASSWORD_ENV = "SIMAI_ADMIN_PASSWORD"

LOGIN_PATH = "/api/auth/login"
CALIBRATIONS_PATH = "/api/calibrations"
CURRENT_CALIBRATION_PATH = "/api/calibrations/current"
CONFIG_BUNDLES_PATH = "/api/config-bundles"

RULE_PROFILES = ("OPERATION", "CLASSROOM_TEST")
DEFAULT_TIMEOUT_S = 10.0

EXIT_OK = 0
EXIT_SERVER = 1
EXIT_VALIDATION = 2


class PublishValidationError(ValueError):
    """올리기 전 검사(인자·환경 변수·계약 스키마·content_hash·site_id·동일 버전) 실패."""


class PublishServerError(RuntimeError):
    """서버 통신·인증·상태 코드·발행 후 확인 실패 (문장에 비밀 값을 넣지 않는다)."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class ValidatedDocument:
    """로컬 검사를 통과한 보정 문서와 원문 텍스트."""

    doc: dict[str, Any]
    raw_text: str
    calibration_version: str
    site_id: str
    state: str
    content_hash: str


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """리다이렉트를 따라가지 않는다. 따라가면 Authorization 헤더가 다른 호스트·평문 http 로 실린다."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


class ServerClient:
    """표준 라이브러리 urllib 기반 EC2 REST 클라이언트."""

    def __init__(
        self,
        server_url: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        base = (server_url or "").strip()
        parts = urllib.parse.urlsplit(base)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise PublishValidationError("--server 는 http(s)://<호스트> 형식의 주소여야 한다")
        if parts.path not in ("", "/"):
            raise PublishValidationError("--server 에는 경로를 제외한 서버 기본 주소만 지정한다")
        if timeout_s <= 0 or not math.isfinite(timeout_s):
            raise PublishValidationError("--timeout 은 0 보다 큰 유한한 초 단위 값이어야 한다")
        self._base = f"{parts.scheme}://{parts.netloc}"
        self._timeout_s = float(timeout_s)
        context = ssl_context if ssl_context is not None else ssl.create_default_context()
        self._opener = urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=context))

    def __repr__(self) -> str:
        return f"ServerClient(base={self._base!r})"

    def request_json(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        headers: dict[str, str] = {"Accept": "application/json"}
        data: bytes | None = None
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")

        req = urllib.request.Request(self._base + path, data=data, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=self._timeout_s) as resp:
                status = int(resp.status)
                raw = resp.read()
        except urllib.error.HTTPError as e:
            with e:
                status = int(e.code)
                raw = e.read()
            code, detail = _parse_error_body(raw)
            suffix = f" ({code}: {detail})" if code and detail else f" ({code or detail})" if (code or detail) else ""
            raise PublishServerError(
                f"{method} {path} 실패: HTTP {status}{suffix}",
                status=status,
                code=code,
            ) from None
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            raise PublishServerError(f"{method} {path} 연결 실패: {type(e).__name__}") from None

        if not (200 <= status < 300):
            raise PublishServerError(f"{method} {path} 비정상 상태: HTTP {status}", status=status)
        if not raw:
            raise PublishServerError(f"{method} {path} 응답 본문이 비었다", status=status)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise PublishServerError(f"{method} {path} 응답이 올바른 JSON 이 아니다", status=status) from e


def _parse_error_body(raw: bytes) -> tuple[str | None, str | None]:
    if not raw:
        return None, None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, None
    if not isinstance(parsed, Mapping):
        return None, None
    code = parsed.get("code")
    detail = parsed.get("detail") or parsed.get("message")
    return (
        code if isinstance(code, str) and code else None,
        detail if isinstance(detail, str) and detail else None,
    )


def _reject_non_finite(constant: str) -> Any:
    raise PublishValidationError(f"보정 문서에 유효하지 않은 JSON 수치({constant})가 있다")


def load_and_validate_document(
    path: Path,
    *,
    expected_site_id: str | None = None,
) -> ValidatedDocument:
    """보정 문서 파일을 읽어 계약 스키마·기하·상태·content_hash·site_id 를 검사한다."""
    try:
        raw_bytes = Path(path).read_bytes()
    except OSError as e:
        raise PublishValidationError(f"보정 문서를 읽지 못했다 ({Path(path).name}): {e.strerror or e}") from e

    try:
        raw_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        raise PublishValidationError(f"보정 문서가 UTF-8 형식이 아니다: {Path(path).name}") from e

    try:
        doc = json.loads(raw_text, parse_constant=_reject_non_finite)
    except json.JSONDecodeError as e:
        raise PublishValidationError(f"보정 문서 JSON 파싱 실패 ({Path(path).name}:{e.lineno}): {e.msg}") from e

    if not isinstance(doc, Mapping):
        raise PublishValidationError("보정 문서 최상위는 JSON 객체여야 한다")

    structure_errors = check_calibration_structure(doc)
    if structure_errors:
        raise PublishValidationError("보정 문서 계약 스키마 위반: " + "; ".join(structure_errors))

    contract_errors = check_calibration(doc)
    if contract_errors:
        raise PublishValidationError("보정 문서 계약 검사 위반: " + "; ".join(contract_errors))

    declared_hash = doc.get("content_hash")
    if not isinstance(declared_hash, str) or not declared_hash:
        raise PublishValidationError("보정 문서에 content_hash 가 없다")
    computed_hash = content_hash(doc)
    if declared_hash != computed_hash:
        raise PublishValidationError(
            f"보정 문서 content_hash 불일치: 문서={declared_hash} 재계산={computed_hash}"
        )

    coord = doc.get("coordinate_frame")
    site_id = coord.get("site_id") if isinstance(coord, Mapping) else None
    if not isinstance(site_id, str) or not site_id.strip():
        raise PublishValidationError("보정 문서 coordinate_frame.site_id 가 비어 있다")

    if expected_site_id is not None and site_id != expected_site_id:
        raise PublishValidationError(
            f"보정 문서 site_id({site_id!r})가 지정한 현장({expected_site_id!r})과 다르다"
        )

    return ValidatedDocument(
        doc=dict(doc),
        raw_text=raw_text,
        calibration_version=str(doc["calibration_version"]),
        site_id=site_id,
        state=str(doc["state"]),
        content_hash=computed_hash,
    )


def _read_admin_credentials(env: Mapping[str, str]) -> tuple[str, str]:
    """환경 변수에서 관리자 계정을 읽는다. 값 자체는 오류 메시지에 넣지 않는다."""
    missing = [name for name in (ADMIN_USER_ENV, ADMIN_PASSWORD_ENV) if not (env.get(name) or "").strip()]
    if missing:
        raise PublishValidationError(f"관리자 계정 환경 변수가 없다: {', '.join(missing)}")
    return env[ADMIN_USER_ENV].strip(), env[ADMIN_PASSWORD_ENV]


def _login_admin(client: ServerClient, username: str, password: str) -> str:
    resp = client.request_json("POST", LOGIN_PATH, body={"username": username, "password": password})
    if not isinstance(resp, Mapping):
        raise PublishServerError("로그인 응답 형식이 올바르지 않다")
    user = resp.get("user")
    if isinstance(user, Mapping):
        role = user.get("role")
        if role is not None and role != "ADMIN":
            raise PublishServerError(f"관리자(ADMIN) 권한이 필요하다 (현재 역할: {role})")
    token = resp.get("accessToken")
    if not isinstance(token, str) or not token.strip():
        raise PublishServerError("로그인 응답에 accessToken 이 없다")
    return token.strip()


def _check_pre_upload_server_state(
    current: Mapping[str, Any],
    validated: ValidatedDocument,
) -> None:
    """서버 현재 보정 상태와 문서의 site_id·버전 중복을 올리기 전에 대조한다."""
    server_site = current.get("site_id")
    if isinstance(server_site, str) and server_site.strip() and server_site != validated.site_id:
        raise PublishValidationError(
            f"서버 현장 site_id({server_site!r})와 보정 문서 site_id({validated.site_id!r})가 다르다"
        )

    current_version = current.get("calibration_version")
    latest_uploaded = current.get("latest_uploaded_version")
    if validated.calibration_version in (current_version, latest_uploaded):
        raise PublishValidationError(
            f"서버의 현재 보정 버전({validated.calibration_version!r})과 같은 버전이다 (덮어쓰지 않는다)"
        )


def publish_calibration(
    validated: ValidatedDocument,
    client: ServerClient,
    *,
    username: str,
    password: str,
    dry_run: bool = False,
    reason: str | None = None,
    rule_profile: str | None = None,
) -> dict[str, Any]:
    """로그인 → 올리기 전 서버 조회 → (dry-run 이 아니면) 업로드·묶음 발행·확인 조회 → 요약 반환."""
    if rule_profile is not None and rule_profile not in RULE_PROFILES:
        raise PublishValidationError(f"rule_profile 은 {RULE_PROFILES} 중 하나여야 한다: {rule_profile!r}")

    token = _login_admin(client, username, password)
    current_before = client.request_json("GET", CURRENT_CALIBRATION_PATH, token=token)
    if not isinstance(current_before, Mapping):
        raise PublishServerError("GET /api/calibrations/current 응답 형식이 올바르지 않다")

    _check_pre_upload_server_state(current_before, validated)

    if dry_run:
        return {
            "dry_run": True,
            "calibration_version": validated.calibration_version,
            "config_version": None,
            "site_id": validated.site_id,
            "state": validated.state,
            "content_hash": validated.content_hash,
            "server": {
                "current_calibration": _summarize_current_calibration(current_before),
            },
        }

    upload_body: dict[str, Any] = {"document_text": validated.raw_text}
    if reason is not None and reason.strip():
        upload_body["reason"] = reason.strip()

    try:
        upload_resp = client.request_json("POST", CALIBRATIONS_PATH, token=token, body=upload_body)
    except PublishServerError as e:
        if e.code == "CALIBRATION_SITE_MISMATCH":
            raise PublishValidationError(
                f"서버 현장과 보정 문서 site_id({validated.site_id!r})가 다르다 ({e.code})"
            ) from None
        if e.code == "CALIBRATION_VERSION_CONFLICT":
            raise PublishValidationError(
                f"보정 버전 {validated.calibration_version!r} 충돌 ({e.code})"
            ) from None
        raise

    if not isinstance(upload_resp, Mapping):
        raise PublishServerError("POST /api/calibrations 응답 형식이 올바르지 않다")
    if upload_resp.get("calibration_version") != validated.calibration_version:
        raise PublishServerError(
            f"업로드된 보정 버전({upload_resp.get('calibration_version')!r})이 문서 버전"
            f"({validated.calibration_version!r})과 다르다"
        )

    publish_body: dict[str, Any] = {}
    if rule_profile is not None:
        publish_body["rule_profile"] = rule_profile

    bundle_resp = client.request_json("POST", CONFIG_BUNDLES_PATH, token=token, body=publish_body)
    if not isinstance(bundle_resp, Mapping):
        raise PublishServerError("POST /api/config-bundles 응답 형식이 올바르지 않다")
    config_version = bundle_resp.get("config_version")
    if not isinstance(config_version, str) or not config_version:
        raise PublishServerError("구성 묶음 발행 응답에 config_version 이 없다")
    if bundle_resp.get("calibration_version") != validated.calibration_version:
        raise PublishServerError(
            f"발행된 구성 묶음({config_version})의 calibration_version"
            f"({bundle_resp.get('calibration_version')!r})이 업로드한 버전({validated.calibration_version!r})과 다르다"
        )

    current_after = client.request_json("GET", CURRENT_CALIBRATION_PATH, token=token)
    if not isinstance(current_after, Mapping):
        raise PublishServerError("발행 후 GET /api/calibrations/current 응답 형식이 올바르지 않다")
    if validated.calibration_version not in (
        current_after.get("calibration_version"),
        current_after.get("latest_uploaded_version"),
    ):
        raise PublishServerError(
            "발행 후 현재 보정 조회에서 업로드한 calibration_version 이 확인되지 않는다"
        )

    bundles_after = client.request_json("GET", CONFIG_BUNDLES_PATH, token=token)
    confirmed_bundle = _extract_bundle(bundles_after, config_version)
    if confirmed_bundle.get("calibration_version") != validated.calibration_version:
        raise PublishServerError(
            f"구성 묶음 조회({config_version})의 calibration_version"
            f"({confirmed_bundle.get('calibration_version')!r})이 업로드한 버전({validated.calibration_version!r})과 다르다"
        )

    return {
        "dry_run": False,
        "calibration_version": validated.calibration_version,
        "config_version": config_version,
        "site_id": validated.site_id,
        "state": validated.state,
        "content_hash": validated.content_hash,
        "server": {
            "upload": {
                "calibration_version": upload_resp.get("calibration_version"),
                "sha256": upload_resp.get("sha256"),
                "state": upload_resp.get("state"),
                "uploaded_at": upload_resp.get("uploaded_at"),
            },
            "bundle": {
                "config_version": confirmed_bundle.get("config_version"),
                "calibration_version": confirmed_bundle.get("calibration_version"),
                "bundle_hash": confirmed_bundle.get("bundle_hash"),
                "rule_profile": confirmed_bundle.get("rule_profile"),
                "status": confirmed_bundle.get("status"),
                "issued_at": confirmed_bundle.get("issued_at"),
            },
            "current_calibration": _summarize_current_calibration(current_after),
        },
    }


def _summarize_current_calibration(current: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "calibration_version": current.get("calibration_version"),
        "calibration_state": current.get("calibration_state"),
        "latest_uploaded_version": current.get("latest_uploaded_version"),
        "document_uploaded": current.get("document_uploaded"),
    }


def _extract_bundle(bundles_resp: Any, expected_version: str) -> Mapping[str, Any]:
    if isinstance(bundles_resp, Mapping):
        if bundles_resp.get("config_version") == expected_version:
            return bundles_resp
        raise PublishServerError(f"조회된 구성 묶음 버전이 {expected_version!r} 이 아니다")
    if isinstance(bundles_resp, list) and bundles_resp:
        for item in bundles_resp:
            if isinstance(item, Mapping) and item.get("config_version") == expected_version:
                return item
        raise PublishServerError(f"최근 구성 묶음 목록에 발행한 버전({expected_version!r})이 없다")
    raise PublishServerError("GET /api/config-bundles 응답이 비었거나 형식이 올바르지 않다")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m simai_ai.calibration.publish",
        description="보정 문서를 검사해 EC2 에 올리고 구성 묶음을 발행한다 (관리자 계정은 환경 변수에서 읽음)",
    )
    p.add_argument(
        "document",
        nargs="?",
        type=Path,
        help="보정 문서(contracts/providers/calibration.schema.json) 경로",
    )
    p.add_argument(
        "--document",
        "--file",
        "-f",
        dest="document_opt",
        type=Path,
        help="보정 문서 경로 (위치 인자 대신 지정 가능)",
    )
    p.add_argument(
        "--server",
        required=True,
        help="EC2 서버 주소 (예: https://simai.live, 필수 인자·기본값 없음)",
    )
    p.add_argument(
        "--site-id",
        default=None,
        help="기대하는 현장 site_id (주면 문서·서버 site_id 와 일치하는지 추가로 확인)",
    )
    p.add_argument(
        "--reason",
        default=None,
        help="보정 업로드 사유 (POST /api/calibrations reason, 생략 시 서버 기본값)",
    )
    p.add_argument(
        "--rule-profile",
        choices=RULE_PROFILES,
        default=None,
        help="구성 묶음 발행 규칙 프로필 (생략 시 서버가 이전 묶음 프로필을 이어받음)",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help=f"HTTP 요청 시간 제한(초, 기본 {DEFAULT_TIMEOUT_S:g})",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="계약·content_hash·서버 현재 보정 조회 검사만 수행하고 서버에 쓰지 않는다",
    )
    return p


def _resolve_document_path(args: argparse.Namespace) -> Path:
    if args.document is not None and args.document_opt is not None and args.document != args.document_opt:
        raise PublishValidationError("보정 문서 경로를 위치 인자와 --document 에 서로 다르게 지정했다")
    target = args.document_opt or args.document
    if target is None:
        raise PublishValidationError("보정 문서 경로가 필요하다")
    return target


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    args = build_parser().parse_args(argv)
    eff_env = os.environ if env is None else env
    try:
        doc_path = _resolve_document_path(args)
        validated = load_and_validate_document(doc_path, expected_site_id=args.site_id)
        username, password = _read_admin_credentials(eff_env)
        client = ServerClient(args.server, timeout_s=args.timeout)
        result = publish_calibration(
            validated,
            client,
            username=username,
            password=password,
            dry_run=bool(args.dry_run),
            reason=args.reason,
            rule_profile=args.rule_profile,
        )
    except PublishValidationError as e:
        print(f"검사 실패: {e}", file=sys.stderr)
        return EXIT_VALIDATION
    except PublishServerError as e:
        print(f"서버 오류: {e}", file=sys.stderr)
        return EXIT_SERVER

    print(json.dumps(result, ensure_ascii=False))
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
