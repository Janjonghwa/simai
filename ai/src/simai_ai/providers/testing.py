"""Provider 공통 계약 시험 묶음 (pytest 용, 명세 20.4 규칙 6, 21.3).

구현 시험에서 아래 클래스를 상속하고 `provider` 픽스처를 정의하면 공통 계약 시험이 붙는다.
이 모듈은 pytest 를 import 하지 않는다(픽스처는 상속한 쪽에서 정의한다).

    import pytest
    from simai_ai.providers.testing import CalibrationProviderContract

    class TestSimCalibration(CalibrationProviderContract):
        @pytest.fixture
        def provider(self):
            return SimCalibrationProvider(...)

FrameSource 시험은 `source` 픽스처를 쓴다. 출처 전달 시험(결과물의 input_provenance)은
`contract.check_provenance_carries` 를 결과물을 만드는 모듈의 시험에서 부른다.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from simai_ai.providers import contract
from simai_ai.providers.base import CalibrationProvider, FrameSource, WorldTrackProvider
from simai_ai.providers.provenance import build_input_provenance


def example_path(name: str) -> Path:
    """contracts/examples 아래 파일 경로 (예: "calibration.json", "invalid/calibration.dataset_calibrated.json")."""
    return contract.contracts_dir() / "examples" / name


def load_example(name: str) -> dict[str, Any]:
    """contracts/examples 의 예시를 읽는다. 밑줄로 시작하는 메타 키(_expect_error 등)는 validate_examples.py 처럼 뺀다."""
    doc = json.loads(example_path(name).read_text(encoding="utf-8"))
    return {k: v for k, v in doc.items() if not k.startswith("_")}


def assert_no_violations(violations: Iterable[str]) -> None:
    """위반 목록이 비어 있지 않으면 전부 모아 AssertionError 로 알린다."""
    violations = list(violations)
    if violations:
        raise AssertionError("계약 위반 {}건:\n- {}".format(len(violations), "\n- ".join(violations)))


class CalibrationProviderContract:
    """CalibrationProvider 공통 계약 시험. 하위 클래스는 `provider` 픽스처를 정의한다.

    클래스 속성으로 바꿀 수 있는 것:
    - `criteria`: CALIBRATED 기준 (명세 4.4 초기값)
    - `ranges`: 단위·축 검사의 경험적 범위 (교실 기준)
    - `require_upright_cameras`: True 면 바로 선 설치를 전제하는 선택 검사(check_cameras_upright)도 한다.
      카메라 roll 은 공통 계약이 제한하지 않으므로 기본은 False 다.
    """

    criteria: contract.CalibratedCriteria = contract.DEFAULT_CALIBRATED_CRITERIA
    ranges: contract.PlausibleRanges = contract.DEFAULT_RANGES
    require_upright_cameras: bool = False

    def test_is_calibration_provider(self, provider):
        assert isinstance(provider, CalibrationProvider)

    def test_schema(self, provider):
        assert_no_violations(contract.check_calibration_schema(provider.current()))

    def test_rotation_units_axes(self, provider):
        doc = provider.current()
        violations = contract.check_calibration_geometry(doc, self.ranges)
        if self.require_upright_cameras:
            violations += contract.check_cameras_upright(doc)
        assert_no_violations(violations)

    def test_calibrated_rules(self, provider):
        assert_no_violations(contract.check_calibration_state(provider.current(), self.criteria))

    def test_provenance(self, provider):
        assert_no_violations(contract.check_calibration_provenance(provider))
        prov = build_input_provenance({"source": "FILE"}, calibration=provider.provenance())
        assert_no_violations(contract.check_input_provenance(prov))


class WorldTrackProviderContract:
    """WorldTrackProvider 공통 계약 시험. 하위 클래스는 `provider` 픽스처를 정의한다.

    `max_frames` 로 긴 스트림의 앞부분만 볼 수 있다(None 이면 전부). `ranges` 로 단위·축 검사의 경험적 범위를 바꾼다.
    """

    max_frames: int | None = None
    ranges: contract.PlausibleRanges = contract.DEFAULT_RANGES

    def _docs(self, provider):
        docs = []
        for i, doc in enumerate(provider.frames()):
            if self.max_frames is not None and i >= self.max_frames:
                break
            docs.append(doc)
        return docs

    def test_is_world_track_provider(self, provider):
        assert isinstance(provider, WorldTrackProvider)

    def test_stream_order_and_source(self, provider):
        assert_no_violations(contract.check_world_track_stream(self._docs(provider)))

    def test_schema(self, provider):
        assert_no_violations(v for doc in self._docs(provider) for v in contract.check_world_tracks_schema(doc))

    def test_no_z0_fill(self, provider):
        assert_no_violations(v for doc in self._docs(provider) for v in contract.check_ground_anchor(doc))

    def test_units_axes(self, provider):
        assert_no_violations(
            v for doc in self._docs(provider) for v in contract.check_world_units_axes(doc, self.ranges)
        )

    def test_observed_inferred_semantics(self, provider):
        assert_no_violations(v for doc in self._docs(provider) for v in contract.check_world_track_semantics(doc))

    def test_provenance(self, provider):
        docs = self._docs(provider)
        mismatched = [
            f"t={d.get('t')}: ({d.get('provider_source')}, {d.get('provider_version')})"
            for d in docs
            if (d.get("provider_source"), d.get("provider_version")) != (provider.source, provider.provider_version)
        ]
        assert_no_violations(mismatched)
        assert_no_violations(contract.check_tracks_provenance(provider))
        prov = build_input_provenance({"source": "FILE"}, tracks=provider.provenance())
        assert_no_violations(contract.check_input_provenance(prov))


class FrameSourceContract:
    """FrameSource 공통 계약 시험. 하위 클래스는 `source` 픽스처를 정의한다."""

    max_frames: int | None = None

    def test_is_frame_source(self, source):
        assert isinstance(source, FrameSource)

    def test_frames(self, source):
        assert_no_violations(contract.check_frame_source(source, self.max_frames))

    def test_provenance(self, source):
        assert_no_violations(contract.check_input_provenance(build_input_provenance(source.provenance())))
