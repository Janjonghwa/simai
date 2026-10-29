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
from simai_ai.calibration.structure import check_calibration_structure
from simai_ai.calibration.versions import VersionSwitchConfig
from simai_ai.providers.base import CalibrationProvider
from simai_ai.providers.contract import DEFAULT_CALIBRATED_CRITERIA, check_calibration


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


def validate_candidate(document: Mapping[str, Any], report: Mapping[str, Any], previous: Mapping[str, Any]) -> None:
    """-343 session 보고서를 문서/기준 버전에 결속하고 부족·미상 근거는 거부한다."""
    # 이전 보정으로 시작할 때 후보 계산 스택(cv2 등)을 미리 불러오지 않는다.
    from simai_ai.calibration.extrinsics.session import DEFAULT_DATA_CRITERIA

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
    limits = DEFAULT_CALIBRATED_CRITERIA
    before = {k: v for k, v in report["holdout"]["before"]["reprojection_error_px"].items() if k != "overall"}
    after = {k: v for k, v in report["holdout"]["after"]["reprojection_error_px"].items() if k != "overall"}
    if set(before) != set(old) or set(after) != set(old):
        raise ValueError("holdout 카메라 누락")
    for values in after.values():
        if (_number(values["median"]) > limits.max_reprojection_median_px
                or _number(values["p95"]) > limits.max_reprojection_p95_px):
            raise ValueError("holdout 재투영 검증 미달")
    baseline = max(_number(v["median"]) for v in before.values())
    improved = max(_number(v["median"]) for v in after.values())
    if baseline <= 0 or improved > baseline * (1 - VersionSwitchConfig().min_relative_improvement):
        raise ValueError("holdout 개선율 미달")
    alignment = report["world_alignment"]
    anchors = alignment["anchor_cameras"]
    if alignment["usable"] is not True or len(set(anchors)) < 2 or not set(anchors) <= set(old):
        raise ValueError("World 정렬 기준 부족")
    for key, maximum in (("anchor_center_residuals_cm", limits.max_pairwise_position_disagreement_m * 100),
                         ("anchor_rotation_residuals_deg", 1.5)):
        values = alignment[key]
        if set(values) != set(anchors) or any(_number(v) > maximum for v in values.values()):
            raise ValueError(f"World 정렬 잔차 미달: {key}")


class RuntimeCalibrationStore(CalibrationProvider):
    """문서 Provider이자 영속 버전 저장소. 별도 계약 변경 없이 완성 문서를 보관한다."""

    def __init__(self, root: Path, bootstrap: Callable[[], Mapping[str, Any]]) -> None:
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

    def path(self, version: str) -> Path:
        return self.root / (hashlib.sha256(version.encode("utf-8")).hexdigest() + ".json")

    def document(self, version: str):
        return json.loads(self.path(version).read_text(encoding="utf-8"))

    def current(self):
        return self.document(self._head["active"])

    def known(self, version: str) -> bool:
        return version in self._head["versions"]

    @property
    def transitions(self):
        return _copy(self._head["transitions"])

    def prepare(self, document, report):
        doc, audit = _copy(document), _copy(report)
        validate_candidate(doc, audit, self.current())
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
