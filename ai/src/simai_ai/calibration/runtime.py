"""검증된 문서 후보의 로컬 보관과 tick 경계 활성화(-344, 명세 4.5).

호출자는 판정 lock으로 prepare/activate를 직렬화한다. 문서는 추가만 하며
head와 전환 이력은 한 파일을 fsync 후 원자적으로 교체한다. 실패하면 메모리 head도 유지한다.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from simai_ai.calibration.document import content_hash
from simai_ai.calibration.monitor import STREAM_REASONS, MonitorReport
from simai_ai.calibration.structure import check_calibration_structure
from simai_ai.providers.base import CalibrationProvider
from simai_ai.providers.contract import NEVER_CALIBRATED_SOURCES, check_calibration


def _copy(value):
    return json.loads(json.dumps(value, allow_nan=False))


def _validate(doc, *, verify_content_hash=True):
    errors = check_calibration_structure(doc)
    if not errors:
        errors = check_calibration(doc)
    if errors:
        raise ValueError("보정 계약 위반: " + "; ".join(errors))
    if verify_content_hash and doc.get("content_hash") != content_hash(doc):
        raise ValueError("content_hash 불일치")


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("검증 지표가 없거나 유효한 비음수 수가 아니다")
    return value


class CandidateValidationError(ValueError):
    """거부 근거와 같은 holdout의 지표 비교를 함께 전달한다."""

    def __init__(self, reason, comparison):
        super().__init__(reason)
        self.comparison = comparison


def validate_candidate(document: Mapping[str, Any], report: Mapping[str, Any], previous: Mapping[str, Any]) -> dict:
    """운영 후보 보고서를 문서/기준 버전에 결속하고 부족·미상 근거는 거부한다."""
    # 이전 보정으로 시작할 때 후보 계산 스택(cv2 등)을 미리 불러오지 않는다.
    from simai_ai.calibration.extrinsics.session import DEFAULT_DATA_CRITERIA
    from simai_ai.calibration.live_candidate import evaluate_improvement, summarize_holdout_metrics

    _validate(document)
    if document["state"] != "PROVISIONAL":
        raise ValueError("자동 후보 상태는 PROVISIONAL이어야 한다(관리자 확정 필요)")
    site = previous["coordinate_frame"].get("site_id")
    if not site or document["coordinate_frame"].get("site_id") != site:
        raise ValueError("site_id 불일치 또는 미상")
    old = {c["camera_id"]: c for c in previous["cameras"]}
    new = {c["camera_id"]: c for c in document["cameras"]}
    if set(old) != set(new):
        raise ValueError("카메라 집합 불일치")
    for cid in old:
        for key in ("image_size", "K", "distortion"):
            if old[cid][key] != new[cid][key]:
                raise ValueError(f"내부 파라미터 변경: {cid}/{key}")
    if report["previous_version"] != previous["calibration_version"]:
        raise ValueError("낡은 후보 기준 버전")
    cal = report["calibration"]
    if (cal["version"], cal["content_hash"], cal["state"]) != (
        document["calibration_version"], document["content_hash"], "PROVISIONAL"
    ) or cal.get("schema_errors") != [] or report.get("failure_reason"):
        raise ValueError("보고서와 후보 문서 불일치 또는 계산 실패")
    sufficiency = report["data_sufficiency"]
    if sufficiency["usable"] is not True or report["split"]["overlap_ticks"] != 0:
        raise ValueError("fit/holdout 데이터 부족 또는 중첩")
    counts = sufficiency["counts"]
    for field, minimum in vars(DEFAULT_DATA_CRITERIA).items():
        if field == "min_camera_3plus_observations":
            values = counts["camera_3plus_observations"]
            if set(values) != set(old) or any(_number(v) < minimum for v in values.values()):
                raise ValueError("카메라별 3대 이상 관측 부족")
        elif _number(counts[field.removeprefix("min_")]) < minimum:
            raise ValueError(f"최소 데이터 기준 미달: {field}")
    before = {k: v for k, v in report["holdout"]["before"]["reprojection_error_px"].items() if k != "overall"}
    after = {k: v for k, v in report["holdout"]["after"]["reprojection_error_px"].items() if k != "overall"}
    if set(before) != set(old) or set(after) != set(old):
        raise ValueError("holdout 카메라 누락")
    # -345와 동일 함수·기준으로 같은 holdout을 비교한다. 원시 session 보고서도 지원한다.
    summaries = {}
    for key in ("before", "after"):
        values = report["holdout"][key]
        for metric in ("fusion_3plus_ratio", "fusion_4_ratio"):
            if _number(values[metric]) > 1:
                raise ValueError(f"holdout 검증 지표 범위 초과: {metric}")
        # 실행기 요약의 중복 파생 필드를 믿지 않고 원시 재투영/쌍 통과율에서 다시 만든다.
        if "pair_pass_ratio" in values:
            values = {**values, "pairs": {pair: {"pass_ratio": ratio}
                                          for pair, ratio in values["pair_pass_ratio"].items()}}
        summaries[key] = summarize_holdout_metrics(values)
        if set(summaries[key]["per_camera_pair_pass_mean"]) != set(old):
            raise ValueError("holdout 카메라별 쌍 통과율 누락")
    common = report["holdout"].get("common_reprojection")
    if not isinstance(common, Mapping):
        raise ValueError("공통 관측 검증 지표 누락")
    delta, decision = evaluate_improvement(summaries["before"], summaries["after"],
                                         common_reprojection=common,
                                         world_alignment=report["world_alignment"])
    comparison = {"before": summaries["before"], "after": summaries["after"], "delta": delta, **decision}
    if not decision["accepted"]:
        raise CandidateValidationError("holdout 개선율/검증 미달: " + "; ".join(decision["rejected_reasons"]), comparison)
    alignment = report["world_alignment"]
    anchors = alignment["anchor_cameras"]
    if alignment["usable"] is not True or len(set(anchors)) < 2 or not set(anchors) <= set(old):
        raise CandidateValidationError("World 정렬 기준 부족", comparison)
    # 보고서가 주장한 잔차와 별개로, 이미 World 정렬된 문서의 실제 변화도 확인한다.
    from simai_ai.calibration.extrinsics.session import camera_changes
    from simai_ai.calibration.live_candidate import evaluate_world_consistency
    import numpy as np

    changes = camera_changes(previous, document)
    centers = lambda cams: {cid: -np.asarray(c["R"]).T @ np.asarray(c["t"]) for cid, c in cams.items()}
    old_centers, new_centers = centers(old), centers(new)
    ratios = []
    for i, a in enumerate(anchors):
        for b in anchors[i + 1:]:
            baseline = float(np.linalg.norm(old_centers[a] - old_centers[b]))
            if baseline <= 1e-12:
                raise CandidateValidationError("World 정렬 기준 카메라 중심 간 거리 미상", comparison)
            ratios.append(abs(float(np.linalg.norm(new_centers[a] - new_centers[b])) / baseline - 1.))
    actual = evaluate_world_consistency({
        "usable": True, "anchor_cameras": anchors,
        "anchor_center_residuals_cm": {c: changes[c]["position_cm"] for c in anchors},
        "anchor_rotation_residuals_deg": {c: changes[c]["rotation_deg"] for c in anchors},
        "scale_change_ratio": max(ratios),
    })
    comparison["document_world_consistency"] = actual
    if not actual["accepted"]:
        comparison["accepted"] = False
        comparison["world_consistency"] = actual
        comparison["rejected_reasons"] = [f"(나) World 일관성: {r}" for r in actual["rejected_reasons"]]
        raise CandidateValidationError("(나) World 일관성: " + "; ".join(actual["rejected_reasons"]), comparison)
    return comparison


class RuntimeCalibrationStore(CalibrationProvider):
    """문서 Provider이자 영속 버전 저장소. 별도 계약 변경 없이 완성 문서를 보관한다."""

    def __init__(self, root: Path, bootstrap: Callable[[], Mapping[str, Any]]) -> None:
        self.last_validation: dict | None = None
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self._head_file = root / "active.json"
        if self._head_file.exists():
            self._head = json.loads(self._head_file.read_text(encoding="utf-8"))
            if self._head["active"] not in self._head["versions"]:
                raise ValueError("활성 보정이 로컬 버전 목록에 없다")
            for version in self._head["versions"]:
                doc = self.document(version)
                if doc["calibration_version"] != version or self._digest(doc) != self._head["document_hashes"][version]:
                    raise ValueError("보관 보정 문서 식별자 또는 파일 해시 불일치")
                _validate(doc, verify_content_hash=version != self._head["bootstrap"])
            self.current()  # 활성 문서 누락도 기동 거부
        else:
            doc = _copy(bootstrap())
            # 시작 파일은 기존 Provider가 쓰던 해시를 보존한다(SIM 등 구형 생산자는 다른 해시 규칙).
            # 새 후보는 반드시 document.content_hash로 검사하며, 시작 문서도 저장 시 파일 해시로 결속한다.
            _validate(doc, verify_content_hash=False)
            self._write_document(doc)
            self._head = {"active": doc["calibration_version"], "bootstrap": doc["calibration_version"],
                          "versions": [doc["calibration_version"]], "transitions": [],
                          "document_hashes": {doc["calibration_version"]: self._digest(doc)}}
            self._save(self._head)
        self._state = self.current()["state"]
        for record in self._head["transitions"]:
            payload = record["payload"]
            if payload["calibration_version"] == self._head["active"]:
                self._state = payload["calibration_state"]

    def path(self, version: str) -> Path:
        return self.root / (hashlib.sha256(version.encode("utf-8")).hexdigest() + ".json")

    def document(self, version: str):
        return json.loads(self.path(version).read_text(encoding="utf-8"))

    def current_report(self):
        """재시작 뒤에도 중심을 고치지 않은 빠른 회전 카메라 정보를 이어받는다."""
        path = self.path(self._head["active"]).with_suffix(".report.json")
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def current(self):
        return self.document(self._head["active"])

    def known(self, version: str) -> bool:
        return version in self._head["versions"]

    @property
    def state(self) -> str:
        """문서 원본을 바꾸지 않고 활성 버전의 마지막 운영 상태를 읽는다."""
        return self._state

    def report_drift(self, report: MonitorReport, *, effective_at: int, msg_id: str) -> bool:
        """판정 lock 안에서 호출. 동일 버전의 DRIFTED 전이를 추가하고 문서는 보존한다."""
        if report.calibration_version != self._head["active"] or not report.findings:
            return False
        doc = self.current()
        stream_changed = any(f.reason in STREAM_REASONS for f in report.findings)
        if doc["provider_source"] in NEVER_CALIBRATED_SOURCES:
            return False
        if self.state not in ("CALIBRATED", "PROVISIONAL") and not (self.state == "DRIFTED" and stream_changed):
            return False
        previous = None
        for record in self._head["transitions"]:
            payload = record["payload"]
            if payload["calibration_version"] == report.calibration_version and payload["reason"] == "RECALIBRATED":
                previous = payload["previous_calibration_version"]
        payload = {
            "calibration_version": report.calibration_version, "previous_calibration_version": previous,
            "calibration_state": "DRIFTED", "provider_source": doc["provider_source"],
            "reason": "STREAM_CHANGED" if stream_changed else "DRIFT_DETECTED",
            "effective_at": effective_at, "calibration_ref": self.path(report.calibration_version).resolve().as_uri(),
            "calibration_hash": doc["content_hash"],
        }
        cameras = report.reprojection_errors()
        if cameras:
            payload["cameras"] = cameras
        head = _copy(self._head)
        head["transitions"].append({"msg_id": msg_id, "payload": payload,
                                    "findings": [{"camera_id": f.camera_id, "reason": str(f.reason),
                                                  "message": f.message} for f in report.findings]})
        self._save(head)
        self._head = head
        self._state = "DRIFTED"
        return True

    @property
    def transitions(self):
        return _copy(self._head["transitions"])

    def prepare(self, document, report):
        doc, audit = _copy(document), _copy(report)
        self.last_validation = validate_candidate(doc, audit, self.current())
        audit["runtime_validation"] = self.last_validation
        version = doc["calibration_version"]
        if self.known(version):
            raise ValueError("이미 저장한 보정 버전")
        self._write_document(doc)
        # 보고서도 문서와 함께 불변 보관한다. 비활성 고아 파일은 재시작 때 채택하지 않는다.
        audit_path = self.path(version).with_suffix(".report.json")
        self._write_once(audit_path, audit)
        return doc

    def activate(self, doc, *, effective_at: int, msg_id: str):
        version = doc["calibration_version"]
        payload = {
            "calibration_version": version, "previous_calibration_version": self._head["active"],
            "calibration_state": doc["state"], "provider_source": doc["provider_source"],
            "reason": "RECALIBRATED", "effective_at": effective_at,
            "calibration_ref": self.path(version).resolve().as_uri(), "calibration_hash": doc["content_hash"],
        }
        head = _copy(self._head)
        head["active"] = version
        head["versions"].append(version)
        head["document_hashes"][version] = self._digest(doc)
        head["transitions"].append({"msg_id": msg_id, "payload": payload})
        self._save(head)
        self._head = head
        self._state = doc["state"]

    def _write_document(self, doc):
        self._write_once(self.path(doc["calibration_version"]), doc)

    @staticmethod
    def _digest(doc):
        return hashlib.sha256(json.dumps(doc, sort_keys=True, allow_nan=False).encode("utf-8")).hexdigest()

    def _write_once(self, path, value):
        if path.exists():
            if json.loads(path.read_text(encoding="utf-8")) != value:
                raise ValueError("저장된 버전을 덮어쓸 수 없다")
            return
        with path.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())

    def _save(self, head):
        tmp = self._head_file.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as stream:
            json.dump(head, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, self._head_file)
