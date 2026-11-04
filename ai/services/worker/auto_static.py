"""사람 지운 배경 묶음(-363)의 static_reconstruct 자동 제출(S15P31A101-403, 명세 3.2·5.3·12.5).

Tier 0 가 관제 중 `state_dir/clean-background/` 에 완성 묶음을 공개하면, 분석 워커 프로세스의 보조 스레드가 관제가 비었을 때
-372 세션 도구(simai_ai.scene.clean_bg_job)의 검사·업로드·입력 만들기를 그대로 써서 묶음을 올리고
EC2 `POST /api/worker/scene-jobs/static-reconstruct`(워커 토큰, 계약 static_reconstruct_submit)로 작업을 등록한다.
결과 Scene 은 지금처럼 EC2 가 CANDIDATE 로 붙이고(-365) 활성화는 관리자가 한다. Tier 0 는 이 일을 하지 않는다(가볍게 둔다).

제출 조건(모두 만족해야 한다. 하나라도 모르면 내지 않는다):
 1. status.json 이 complete 이고 그 묶음 폴더에 COMPLETE 가 있으며, 묶음의 보정 버전이 Tier 0 의 지금 활성 보정
    (state_dir/calibrations/active.json, simai_ai.calibration.runtime)이고 그 상태가 PROVISIONAL·CALIBRATED 다.
 2. LIVE 가 아니다: 대여와 같은 AdmissionGate 판단(Tier 0 표시 파일 신선도)을 이 스레드 전용 인스턴스로 다시 본다.
    표시 파일이 없을 때 관제 없음으로 보는 것은 Tier 0 가 남긴 정상 종료 기록(`<표시 파일>.stopped`)이 있을 때(운영 흐름,
    simai-bootstrap after-live)나 개발 모드 SIMAI_WORKER_DEV_NO_LIVE=1 일 때뿐이다. 기록 없음·끊김·모름은 LIVE 다.
 3. 마지막으로 LIVE(또는 모름)를 본 뒤 quiet_after_live_s(기본 300초)가 지났다. 프로세스 시작 시각을 마지막 LIVE 로 본다(모르면 기다린다).
 4. 이 묶음을 낸 적이 없다(추가만 하는 기록 파일, 재시작 뒤에도 같다).
 5. 자동 제출한 작업 중 끝나지 않은 것(QUEUED·LEASED)이 없다(한 번에 하나). 끝났는지는 같은 요청을 다시 보내 EC2 가 돌려주는
    지금 상태로 안다(같은 bundle_id 는 같은 작업을 돌려준다).
업로드·등록 실패는 지수 백오프(기본 60초에서 2배씩, 상한 1시간)로 다시 해 본다. 작업이 FAILED·TIMED_OUT·CANCELLED 로 끝난 묶음은
자동으로 다시 내지 않고 기록만 남긴다(운영자는 -372 도구로 손으로 낼 수 있다). 업로드 도중 LIVE 가 되면 바로 멈추고 실패로 세지 않는다.
배경 사진은 정적 3D(Visual 층) 입력이고 Tier 0 위치·위험 판정에 들어가지 않는다(규칙 1).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from simai_ai.gpu.admission import Admission

from .client import Backoff
from .transport import STATIC_SUBMIT_PATH, Transport, TransportError

log = logging.getLogger("services.worker.auto_static")

ENV_ENABLED = "SIMAI_AUTO_STATIC_RECONSTRUCT"
SUBMIT_SCHEMA_VERSION = "1.1.0"  # 계약 static_reconstruct_submit
RESULT_MAJOR = "1"
BUNDLE_RE = re.compile(r"^bundle-[0-9a-f]{32}$")
PENDING_STATUSES = frozenset({"QUEUED", "LEASED"})
TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED"})
CALIBRATION_STATES = frozenset({"PROVISIONAL", "CALIBRATED"})
LEDGER_NAME = "auto_static_reconstruct.jsonl"
UPLOAD_PURPOSE = "STATIC_RECONSTRUCT_INPUT"  # 서버 업로드 목적: 워커 토큰, 키 static-inputs/<bundle_id>/<이름>
# exit_when_done 일 때 '낼 일이 끝남'으로 보는 이유(대기 시간이 지난 뒤에만 나온다). 실패·백오프·진행 중·LIVE 는 계속 돈다
DONE_REASONS = frozenset({"FINISHED", "ALREADY_SUBMITTED", "NO_BUNDLE", "INCOMPLETE", "NO_COMPLETE_MARK", "BAD_STATUS",
                          "STATUS_UNREADABLE", "NOT_ACTIVE_CALIBRATION", "NO_ACTIVE_CALIBRATION", "CALIBRATION_STATE"})


class LiveStarted(Exception):
    """업로드 도중 LIVE(또는 모름)가 됐다. 실패로 세지 않고 멈춘다."""


class SubmitError(Exception):
    """EC2 등록 요청이 받아들여지지 않았거나 응답을 쓸 수 없다. 다음 주기에 백오프로 다시 해 본다."""


class Gate(Protocol):
    def check(self) -> Admission: ...


# (clean-background 묶음 폴더, 보정 문서 경로, 계속해도 되는지) → static_reconstruct 작업 입력
Prepare = Callable[[Path, Path, Callable[[], bool]], Mapping[str, Any]]


def _flag(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    if raw in ("1", "0"):
        return raw == "1"
    raise ValueError(f"{key} 는 1 또는 0 이다")


def _positive(env: Mapping[str, str], key: str, default: float) -> float:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{key} 는 양수(초)다") from None
    if not value > 0 or value == float("inf"):
        raise ValueError(f"{key} 는 양수(초)다")
    return value


@dataclass(frozen=True)
class AutoStaticSettings:
    """환경 변수:

    SIMAI_AUTO_STATIC_RECONSTRUCT   1(기본) 켬, 0 끔
    SIMAI_STATE_DIR                 Tier 0 상태 루트(기본 $SIMAI_ROOT/state). 묶음은 clean-background/, 활성 보정은 calibrations/
    SIMAI_AUTO_STATIC_QUIET_S       LIVE 를 마지막으로 본 뒤 기다리는 초(기본 300)
    SIMAI_AUTO_STATIC_POLL_S        조건을 다시 보는 주기 초(기본 30)
    SIMAI_AUTO_STATIC_EXIT_WHEN_DONE 1 이면 낼 일이 끝나면(자동 제출 작업이 끝났거나 낼 묶음이 없음) 워커 프로세스를 끝낸다(기본 0).
                                    simai-bootstrap after-live 가 켠다(관제 뒤 배경 복원만 하고 Pod 를 정리하는 흐름)
    """

    state_dir: Path
    enabled: bool = True
    quiet_after_live_s: float = 300.0
    poll_s: float = 30.0
    exit_when_done: bool = False
    backoff: Backoff = field(default_factory=lambda: Backoff(initial_s=60.0, factor=2.0, max_s=3600.0))

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> AutoStaticSettings:
        root = Path(env.get("SIMAI_ROOT") or "/workspace/simai")
        state_dir = Path((env.get("SIMAI_STATE_DIR") or "").strip() or root / "state")
        return cls(
            state_dir=state_dir,
            enabled=_flag(env, ENV_ENABLED, True),
            quiet_after_live_s=_positive(env, "SIMAI_AUTO_STATIC_QUIET_S", 300.0),
            poll_s=_positive(env, "SIMAI_AUTO_STATIC_POLL_S", 30.0),
            exit_when_done=_flag(env, "SIMAI_AUTO_STATIC_EXIT_WHEN_DONE", False),
        )

    @property
    def clean_background_dir(self) -> Path:
        return self.state_dir / "clean-background"

    @property
    def calibrations_dir(self) -> Path:
        return self.state_dir / "calibrations"

    @property
    def ledger_path(self) -> Path:
        return self.state_dir / "worker" / LEDGER_NAME


def calibration_document_path(calibrations_dir: Path, version: str) -> Path:
    """Tier 0 보정 버전 저장소(simai_ai.calibration.runtime.RuntimeCalibrationStore.path)와 같은 파일 이름 규칙."""
    return calibrations_dir / (hashlib.sha256(version.encode("utf-8")).hexdigest() + ".json")


class SubmitLedger:
    """제출 기록(JSON 한 줄씩 추가만 한다, 규칙 6). 사건: SUBMITTED·SUBMIT_FAILED·FINISHED.

    쓰기는 줄 단위 추가 + fsync 다. 끝 줄이 깨졌으면(쓰다가 꺼짐) 그 줄만 건너뛴다. 기록을 못 읽으면 모름 → 내지 않는다.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for n, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                doc = json.loads(line)
            except json.JSONDecodeError:
                log.warning("자동 제출 기록 %s 의 %d번째 줄을 읽지 못해 건너뛴다", self.path, n)
                continue
            if isinstance(doc, dict):
                out.append(doc)
        return out

    def append(self, record: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(dict(record), ensure_ascii=False, sort_keys=True) + "\n"
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def pending(records: list[dict[str, Any]]) -> dict[str, Any] | None:
        """끝났다는 기록이 없는 마지막 SUBMITTED. 한 번에 하나만 내므로 많아야 하나다."""
        finished = {r.get("bundle_id") for r in records if r.get("event") == "FINISHED"}
        for r in reversed(records):
            if r.get("event") == "SUBMITTED" and r.get("bundle_id") not in finished:
                return r
        return None

    @staticmethod
    def submitted(records: list[dict[str, Any]], bundle_id: str) -> bool:
        return any(r.get("event") == "SUBMITTED" and r.get("bundle_id") == bundle_id for r in records)

    @staticmethod
    def failures(records: list[dict[str, Any]], bundle_id: str) -> tuple[int, int | None]:
        """이 묶음의 이어진 실패 수와 마지막 실패 시각(UTC ms)."""
        count, last = 0, None
        for r in records:
            if r.get("bundle_id") != bundle_id:
                continue
            if r.get("event") == "SUBMIT_FAILED":
                count, last = count + 1, int(r.get("at_ms") or 0)
            elif r.get("event") == "SUBMITTED":
                count, last = 0, None
        return count, last


@dataclass(frozen=True)
class Candidate:
    bundle_id: str
    calibration_version: str
    bundle_dir: Path
    calibration_path: Path


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


class AutoStaticSubmitter:
    """조건을 poll_s 마다 본다(tick). run 은 보조 스레드 본체다. 시계·문·전송·업로드는 주입한다."""

    def __init__(
        self,
        settings: AutoStaticSettings,
        gate: Gate,
        transport: Transport,
        worker_id: str,
        *,
        prepare: Prepare,
        clock_ms: Callable[[], int],
        monotonic: Callable[[], float],
        new_msg_id: Callable[[], str] = lambda: str(uuid.uuid4()),
        request_timeout_s: float = 30.0,
    ) -> None:
        if not worker_id:
            raise ValueError("워커 ID 가 비었다")
        self.settings = settings
        self.gate = gate
        self.transport = transport
        self.worker_id = worker_id
        self.prepare = prepare
        self.ledger = SubmitLedger(settings.ledger_path)
        self._clock_ms = clock_ms
        self._monotonic = monotonic
        self._new_msg_id = new_msg_id
        self._timeout_s = request_timeout_s
        self._last_live = monotonic()  # 시작 전은 모른다: 시작 시각을 마지막 LIVE 로 본다
        self._last_reason: str | None = None

    # ------------------------------------------------------------------ 루프

    def run(self, stop: threading.Event) -> None:
        log.info("배경 묶음 자동 제출을 켠다: 묶음 %s, 기록 %s, LIVE 뒤 %.0f초",
                 self.settings.clean_background_dir, self.ledger.path, self.settings.quiet_after_live_s)
        while not stop.is_set():
            try:
                reason = self.tick()
            except Exception:  # 보조 스레드가 죽으면 워커 대여에는 영향이 없지만 다시 해 볼 수 없다
                log.exception("배경 묶음 자동 제출 주기 오류(다음 주기에 다시 본다)")
                reason = None
            if self.settings.exit_when_done and reason in DONE_REASONS:
                log.info("배경 묶음 자동 제출: 낼 일이 끝났다(%s). 워커를 끝낸다(SIMAI_AUTO_STATIC_EXIT_WHEN_DONE=1)", reason)
                stop.set()
                return
            stop.wait(self.settings.poll_s)

    def tick(self) -> str:
        """조건을 한 번 보고 한 일을 돌려준다(시험·로그용 이유 문자열)."""
        reason = self._tick()
        if reason != self._last_reason:
            self._last_reason = reason
            log.info("배경 묶음 자동 제출: %s", reason)
        return reason

    def _tick(self) -> str:
        if self._live():
            return "LIVE"
        quiet_left = self.settings.quiet_after_live_s - (self._monotonic() - self._last_live)
        if quiet_left > 0:
            return "QUIET"
        try:
            records = self.ledger.records()
        except (OSError, UnicodeDecodeError) as e:
            log.error("자동 제출 기록을 읽지 못해 내지 않는다: %s (%s)", self.ledger.path, e)
            return "LEDGER_UNREADABLE"
        pending = SubmitLedger.pending(records)
        if pending is not None:
            return self._poll(pending)
        found = self._candidate()
        if isinstance(found, str):
            return found
        if SubmitLedger.submitted(records, found.bundle_id):
            return "ALREADY_SUBMITTED"
        fails, last_at = SubmitLedger.failures(records, found.bundle_id)
        if fails and last_at is not None and self._clock_ms() < last_at + self.settings.backoff.delay(fails) * 1000:
            return "BACKOFF"
        return self._submit(found)

    def _live(self) -> bool:
        try:
            adm = self.gate.check()
            live = bool(adm.live)
        except Exception as e:  # noqa: BLE001 - 문을 못 보면 모름 → LIVE
            log.warning("접수 문 확인 실패 → LIVE 로 본다: %s: %s", type(e).__name__, e)
            live = True
        if live:
            self._last_live = self._monotonic()
        return live

    # ------------------------------------------------------------------ 조건 1

    def _candidate(self) -> Candidate | str:
        root = self.settings.clean_background_dir
        status_path = root / "status.json"
        if not status_path.is_file():
            return "NO_BUNDLE"
        try:
            status = _read_json(status_path)
        except (OSError, ValueError):
            return "STATUS_UNREADABLE"
        if not isinstance(status, dict) or status.get("complete") is not True or not isinstance(status.get("bundle"), str):
            return "INCOMPLETE"
        version = status.get("calibration_version")
        bundle_id = Path(status["bundle"]).name
        if not isinstance(version, str) or not version or not BUNDLE_RE.match(bundle_id):
            return "BAD_STATUS"
        bundle_dir = root / version / bundle_id
        if not (bundle_dir / "COMPLETE").is_file():
            return "NO_COMPLETE_MARK"
        active = self._active_calibration()
        if isinstance(active, str):
            return active
        active_version, cal_path, state = active
        if active_version != version:
            return "NOT_ACTIVE_CALIBRATION"
        if state not in CALIBRATION_STATES:
            return "CALIBRATION_STATE"
        return Candidate(bundle_id, version, bundle_dir, cal_path)

    def _active_calibration(self) -> tuple[str, Path, str] | str:
        """Tier 0 보정 버전 저장소의 활성 버전·문서·상태. 상태는 저장소와 같이 전환 이력의 마지막 값이 문서 값을 이긴다."""
        cal_dir = self.settings.calibrations_dir
        try:
            head = _read_json(cal_dir / "active.json")
            version = head["active"]
            path = calibration_document_path(cal_dir, version)
            state = _read_json(path)["state"]
            for record in head.get("transitions") or []:
                payload = record.get("payload") or {}
                if payload.get("calibration_version") == version and payload.get("calibration_state"):
                    state = payload["calibration_state"]
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return "NO_ACTIVE_CALIBRATION"
        if not isinstance(version, str):
            return "NO_ACTIVE_CALIBRATION"
        return version, path, state

    # ------------------------------------------------------------------ 제출·상태

    def _submit(self, cand: Candidate) -> str:
        base = {"bundle_id": cand.bundle_id, "calibration_version": cand.calibration_version}
        try:
            job_input = dict(self.prepare(cand.bundle_dir, cand.calibration_path, lambda: not self._live()))
            if self._live():
                raise LiveStarted
            payload = {"bundle_id": cand.bundle_id, "input": job_input}
            result = self._post(payload)
        except LiveStarted:
            log.warning("배경 묶음 %s 업로드 중 LIVE(또는 모름)가 됐다. 멈추고 관제가 빈 뒤 다시 본다", cand.bundle_id)
            return "LIVE"
        except Exception as e:  # noqa: BLE001 - 검사·업로드·등록 실패는 모두 백오프로 다시 해 본다
            message = f"{type(e).__name__}: {e}"[:500]
            self.ledger.append({"event": "SUBMIT_FAILED", "at_ms": self._clock_ms(), **base, "error": message})
            log.warning("배경 묶음 %s 제출 실패(백오프 뒤 다시): %s", cand.bundle_id, message)
            return "SUBMIT_FAILED"
        self.ledger.append({"event": "SUBMITTED", "at_ms": self._clock_ms(), **base, "job_id": result["job_id"],
                            "created": result["created"], "job_status": result["job_status"], "request": payload})
        log.info("배경 묶음 %s → static_reconstruct 작업 %s (%s, 새로 만듦 %s)", cand.bundle_id, result["job_id"],
                 result["job_status"], result["created"])
        if result["job_status"] in TERMINAL_STATUSES:
            self._finish(cand.bundle_id, result)
        return "SUBMITTED"

    def _poll(self, pending: dict[str, Any]) -> str:
        request = pending.get("request")
        if not isinstance(request, dict):
            log.error("자동 제출 기록의 SUBMITTED 에 요청이 없어 상태를 볼 수 없다: %s", pending.get("bundle_id"))
            return "PENDING_UNKNOWN"
        try:
            result = self._post(request)
        except Exception as e:  # noqa: BLE001 - 상태 조회 실패는 다음 주기에 다시 본다
            log.warning("작업 %s 상태 조회 실패: %s: %s", pending.get("job_id"), type(e).__name__, e)
            return "PENDING"
        if result["job_status"] in TERMINAL_STATUSES:
            self._finish(str(pending.get("bundle_id")), result)
            return "FINISHED"
        return "PENDING"

    def _finish(self, bundle_id: str, result: Mapping[str, Any]) -> None:
        self.ledger.append({"event": "FINISHED", "at_ms": self._clock_ms(), "bundle_id": bundle_id,
                            "job_id": result["job_id"], "job_status": result["job_status"],
                            "job_error_code": result.get("job_error_code")})
        (log.info if result["job_status"] == "SUCCEEDED" else log.warning)(
            "배경 묶음 %s 작업 %s 끝: %s %s (실패해도 자동으로 다시 내지 않는다)", bundle_id, result["job_id"],
            result["job_status"], result.get("job_error_code") or "")

    def _post(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        body = {
            "schema_version": SUBMIT_SCHEMA_VERSION,
            "msg_type": "static_reconstruct_submit",
            "msg_id": self._new_msg_id(),
            "sent_at": int(self._clock_ms()),
            "source_id": self.worker_id,
            "payload": dict(payload),
        }
        try:
            resp = self.transport.post(STATIC_SUBMIT_PATH, body, self._timeout_s)
        except TransportError as e:
            raise SubmitError(f"등록 요청 연결 실패: {e}") from None
        if resp.status not in (200, 201) or resp.body is None:
            code = (resp.body or {}).get("code") or ""
            raise SubmitError(f"등록 거부 HTTP {resp.status} {code}".rstrip())
        msg = resp.body
        p = msg.get("payload") if isinstance(msg.get("payload"), dict) else {}
        version = msg.get("schema_version")
        if (msg.get("msg_type") != "static_reconstruct_submit_result" or not isinstance(version, str)
                or version.split(".")[0] != RESULT_MAJOR or p.get("bundle_id") != payload.get("bundle_id")
                or not isinstance(p.get("job_id"), str) or not p["job_id"]
                or p.get("job_status") not in PENDING_STATUSES | TERMINAL_STATUSES):
            raise SubmitError("등록 응답이 static_reconstruct_submit_result 1.x 가 아니다")
        return {"job_id": p["job_id"], "created": bool(p.get("created")), "job_status": p["job_status"],
                "job_error_code": p.get("job_error_code")}


def clean_bg_uploader(base_url: str, token: str, *, timeout_s: float = 30.0) -> Prepare:
    """-372 세션 도구의 검사·서명 업로드·입력 만들기를 그대로 쓴다. 업로드 목적은 정적 3D 입력(STATIC_RECONSTRUCT_INPUT, 워커 토큰)이고
    키는 static-inputs/<bundle_id>/<카메라 ID>.jpg·calibration.json 으로 묶음마다 정해진다(다시 내도 같은 키·같은 내용이라 객체가 늘지 않는다).

    clean_bg_job 은 cv2 를 쓰므로 실제로 낼 때 처음 import 한다(CI 기본 설치·다른 처리기에 영향 없음).
    """

    def prepare(bundle_dir: Path, calibration_path: Path, may_continue: Callable[[], bool]) -> Mapping[str, Any]:
        from simai_ai.scene import clean_bg_job as cbj

        cal, cal_hash, cal_bytes = cbj.load_calibration(calibration_path)
        prepared = cbj.prepare_from_bundle(bundle_dir, cal)
        # 올리기 전에 로컬 계약(출처·옵션·보정 일치)부터 본다(run_cli 와 같은 사전 검사)
        bucket = cbj.DEFAULT_DRY_RUN_BUCKET
        cbj.build_job_input(prepared=prepared, cal=cal, calibration_ref=f"s3://{bucket}/preflight/calibration.json",
                            calibration_hash=cal_hash,
                            image_uris={img.camera_id: f"s3://{bucket}/preflight/{img.camera_id}.jpg" for img in prepared.images})

        bundle_id = bundle_dir.name

        def upload(raw: bytes, content_type: str, name: str) -> str:
            if not may_continue():
                raise LiveStarted
            return cbj.sign_and_upload_object(server_url=base_url, token=token, purpose=UPLOAD_PURPOSE,
                                              content_type=content_type, raw_bytes=raw,
                                              sha256_hex=hashlib.sha256(raw).hexdigest(), timeout_s=timeout_s, label=name,
                                              extra={"bundle_id": bundle_id, "file_name": name})

        calibration_ref = upload(cal_bytes, "application/json", "calibration")
        image_uris = {img.camera_id: upload(img.raw_bytes, img.content_type, img.camera_id) for img in prepared.images}
        return cbj.build_job_input(prepared=prepared, cal=cal, calibration_ref=calibration_ref, calibration_hash=cal_hash,
                                   image_uris=image_uris)

    return prepare
