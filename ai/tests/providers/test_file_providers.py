"""참조 구현(파일 Provider)이 공통 계약 시험 묶음을 통과하는지 본다.

다른 구현(SIM -46 등)도 같은 방식으로 계약 시험 클래스를 상속해 쓴다.
"""

import copy
import json

import pytest

from simai_ai.providers import FileCalibrationProvider, FileWorldTrackProvider
from simai_ai.providers.testing import (
    CalibrationProviderContract,
    WorldTrackProviderContract,
    example_path,
    load_example,
)


class TestFileCalibrationExample(CalibrationProviderContract):
    @pytest.fixture
    def provider(self):
        return FileCalibrationProvider(example_path("calibration.json"))


class TestFileCalibrationCalibrated(CalibrationProviderContract):
    """독립 검증 기준(수정 6)을 통과한 G1_AUTO 보정은 CALIBRATED 가 될 수 있다."""

    @pytest.fixture
    def provider(self, tmp_path):
        doc = load_example("calibration.json")
        doc.update(provider_source="G1_AUTO", state="CALIBRATED")
        path = tmp_path / "calibration.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        return FileCalibrationProvider(path)


class TestFileCalibrationUpright(CalibrationProviderContract):
    """바로 선 설치를 전제하는 구현은 선택 검사를 켤 수 있다."""

    require_upright_cameras = True

    @pytest.fixture
    def provider(self):
        return FileCalibrationProvider(example_path("calibration.json"))


class TestFileWorldTracksExample(WorldTrackProviderContract):
    @pytest.fixture
    def provider(self):
        return FileWorldTrackProvider(example_path("world_tracks.json"))


def _write_jsonl(path, docs, blank_lines=False):
    sep = "\n\n" if blank_lines else "\n"
    path.write_text(sep.join(json.dumps(d) for d in docs) + "\n", encoding="utf-8")
    return path


def _stream(n=3):
    base = load_example("world_tracks.json")
    docs = []
    for i in range(n):
        doc = copy.deepcopy(base)
        doc["t"] = base["t"] + 100 * i
        for tr in doc["tracks"]:
            tr["t_last_valid"] = doc["t"]
        docs.append(doc)
    return docs


class TestFileWorldTracksJsonl(WorldTrackProviderContract):
    """계약의 저장 형식(시각 순서 JSONL)."""

    @pytest.fixture
    def provider(self, tmp_path):
        return FileWorldTrackProvider(_write_jsonl(tmp_path / "tracks.jsonl", _stream(), blank_lines=True))


def test_calibration_current_is_a_copy():
    provider = FileCalibrationProvider(example_path("calibration.json"))
    provider.current()["state"] = "CALIBRATED"
    assert provider.current()["state"] == "PROVISIONAL"


def test_calibration_provenance_matches_document():
    provider = FileCalibrationProvider(example_path("calibration.json"))
    assert provider.provenance() == {"source": "G1_AUTO", "calibration_version": "cal-0"}


def test_calibration_rejects_non_object(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(TypeError):
        FileCalibrationProvider(path)


def test_world_tracks_jsonl_reads_in_order(tmp_path):
    docs = _stream(4)
    provider = FileWorldTrackProvider(_write_jsonl(tmp_path / "tracks.jsonl", docs))
    assert [d["t"] for d in provider.frames()] == [d["t"] for d in docs]
    assert provider.source == "DATASET_GT"
    assert provider.provenance() == {"source": "DATASET_GT", "provider_version": "panoptic-adapter-0.1.0"}


def test_world_tracks_json_array(tmp_path):
    docs = _stream(2)
    path = tmp_path / "tracks.json"
    path.write_text(json.dumps(docs), encoding="utf-8")
    assert len(list(FileWorldTrackProvider(path).frames())) == 2


def test_world_tracks_frames_can_be_read_twice(tmp_path):
    provider = FileWorldTrackProvider(_write_jsonl(tmp_path / "tracks.jsonl", _stream(2)))
    assert len(list(provider.frames())) == len(list(provider.frames())) == 2


def test_world_tracks_empty_file_fails(tmp_path):
    path = tmp_path / "tracks.jsonl"
    path.write_text("\n", encoding="utf-8")
    with pytest.raises(ValueError):
        FileWorldTrackProvider(path)


def test_file_provider_does_not_fill_unknown_ground_anchor(tmp_path):
    """접지를 모르는 트랙은 파일 그대로 null 로 나온다 (Z=0 대체 금지)."""
    doc = load_example("world_tracks.json")
    tr = doc["tracks"][0]
    tr.update(ground_anchor_xyz=None, support_surface_validity="UNKNOWN", z_validity="UNAVAILABLE")
    provider = FileWorldTrackProvider(_write_jsonl(tmp_path / "tracks.jsonl", [doc]))
    assert next(provider.frames())["tracks"][0]["ground_anchor_xyz"] is None
