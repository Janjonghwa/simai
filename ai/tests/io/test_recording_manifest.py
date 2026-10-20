"""녹화 파일 입력이 gateway/recorder manifest 를 계약 스키마로 읽는지 (S15P31A101-158, 명세 15.3·20.4)."""

import json
import shutil
from pathlib import Path

import pytest

from simai_ai.io import (
    Recording,
    VideoFileFrameSource,
    load_recording_manifest,
    recording_manifest_errors,
    recordings_from_manifest,
)
from simai_ai.providers import build_input_provenance
from simai_ai.providers.contract import CONTRACTS_DIR_ENV, contracts_dir

EXAMPLES = contracts_dir() / "examples"
EXAMPLE = EXAMPLES / "recording_manifest.json"
URI = "volume://AP-JP-1/simai/recordings/rec-20261020-090000z-walk-calibration"


class ListReader:
    def __init__(self, images, fps):
        self._images = list(images)
        self._fps = fps

    @property
    def fps(self):
        return self._fps

    def read(self):
        return self._images.pop(0) if self._images else None

    def close(self):
        pass


@pytest.fixture
def session_dir(tmp_path):
    shutil.copy(EXAMPLE, tmp_path / "manifest.json")
    return tmp_path


def _manifest(path=EXAMPLE):
    return {k: v for k, v in json.loads(path.read_text(encoding="utf-8")).items() if not k.startswith("_")}


def _write(session_dir, manifest):
    (session_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_recordings_from_manifest(session_dir):
    manifest = load_recording_manifest(session_dir)
    cam_a, cam_b = (c for c in manifest["cameras"])
    recs = recordings_from_manifest(session_dir, manifest, epochs={"cam_A": 1})
    assert recs == [
        Recording(
            camera_id="cam_A",
            path=session_dir / "cam_A/epoch_0001.h264",
            start_time_ms=cam_a["segments"][1]["capture_time_range_ms"][0],
            file_uri=f"{URI}/cam_A/epoch_0001.h264",
            fps=25.0,
            timestamp_uncertainty_ms=20.0,
        ),
        Recording(
            camera_id="cam_B",
            path=session_dir / "cam_B/epoch_0000.h264",
            start_time_ms=cam_b["segments"][0]["capture_time_range_ms"][0],
            file_uri=f"{URI}/cam_B/epoch_0000.h264",
            fps=25.0,
            timestamp_uncertainty_ms=20.0,
        ),
    ]


def test_camera_with_several_epochs_needs_a_choice(session_dir):
    with pytest.raises(ValueError, match="cam_A"):
        recordings_from_manifest(session_dir)
    with pytest.raises(ValueError, match="cam_Z"):
        recordings_from_manifest(session_dir, epochs={"cam_A": 0, "cam_Z": 0})


def test_from_manifest_frames_and_provenance(session_dir):
    src = VideoFileFrameSource.from_manifest(
        session_dir, open_reader=lambda p: ListReader([p.as_posix()] * 2, 0.0), epochs={"cam_A": 0}
    )
    frames = list(src.frames())
    assert [(f.camera_id, f.frame_ref["frame_index"]) for f in frames] == [
        ("cam_A", 0), ("cam_A", 1), ("cam_B", 0), ("cam_B", 1),
    ]  # fmt: skip
    t0 = _manifest()["started_at_ms"]
    assert [f.estimated_capture_time - t0 for f in frames] == [212, 252, 305, 345]  # nominal_fps 25
    assert frames[0].timestamp_source == "FILE" and frames[0].timestamp_uncertainty_ms == 20.0
    assert frames[0].frame_ref["file_uri"] == f"{URI}/cam_A/epoch_0000.h264"
    prov = src.provenance()
    assert prov == {
        "source": "FILE",
        "dataset_id": "recording/rec-20261020-090000z-walk-calibration",
        "camera_ids": ["cam_A", "cam_B"],
    }
    assert build_input_provenance(prov)["is_upper_bound"] is False


def test_manifest_violating_contract_is_rejected(session_dir):
    manifest = _manifest()
    manifest["consent"] = None  # RTSP 녹화인데 동의·보관 기한이 없다 (명세 11.5)
    (session_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="계약 위반"):
        load_recording_manifest(session_dir)
    with pytest.raises(ValueError, match="계약 위반"):
        VideoFileFrameSource.from_manifest(session_dir, open_reader=lambda p: ListReader([], 25.0))


def test_example_files_point_inside_session_dir(session_dir):
    for rec in recordings_from_manifest(session_dir, epochs={"cam_A": 0}):
        assert Path(rec.path).resolve().is_relative_to(session_dir.resolve())


# ------------------------------------------------------------------ 녹화 도구(validate_manifest)와 같은 판정


@pytest.mark.parametrize("name", ["recording_manifest.json", "recording_manifest.test_input.json"])
def test_contract_examples_pass(name):
    assert recording_manifest_errors(_manifest(EXAMPLES / name)) == []


@pytest.mark.parametrize("path", sorted((EXAMPLES / "invalid").glob("recording_manifest.*.json")), ids=lambda p: p.name)
def test_contract_violations_are_rejected(session_dir, path):
    _write(session_dir, _manifest(path))
    with pytest.raises(ValueError, match="계약 위반"):
        load_recording_manifest(session_dir)


def test_secret_in_url_is_not_echoed(session_dir):
    """RTSP 계정이 든 주소를 받아도 예외 문장에는 경로와 위반 규칙만 나온다 (비밀 값은 .env 에만)."""
    manifest = _manifest()
    manifest["cameras"][0]["source"]["url"] = "rtsp://user:SECRET@192.168.0.11:554/stream1"
    _write(session_dir, manifest)
    with pytest.raises(ValueError) as exc:
        load_recording_manifest(session_dir)
    message = str(exc.value)
    assert "스키마 cameras/0/source/url: pattern" in message
    assert "SECRET" not in message and "user:" not in message and "192.168.0.11" not in message


def test_unknown_field_name_is_not_echoed():
    manifest = _manifest()
    manifest["cameras"][0]["source"]["password"] = "SECRET"
    assert recording_manifest_errors(manifest) == ["스키마 cameras/0/source: additionalProperties"]


def test_camera_kind_must_be_listed_in_input_kinds(session_dir):
    """로컬 파일 카메라가 섞였는데 input_kinds 가 RTSP 뿐이면 시험 입력이 제품 입력으로 읽히지 않게 거절한다 (AGENTS 규칙 7)."""
    manifest = _manifest()
    manifest["cameras"][1]["source"] = {"kind": "FILE", "file_name": "cam_B.mp4"}
    assert recording_manifest_errors(manifest) == ['스키마 input_kinds: contains {"const": "FILE"}']
    _write(session_dir, manifest)
    with pytest.raises(ValueError, match="input_kinds"):
        VideoFileFrameSource.from_manifest(session_dir, open_reader=lambda p: ListReader([], 25.0), epochs={"cam_A": 0})


def test_retention_date_must_exist():
    """format: date 를 녹화 도구와 같이 검사한다(패턴은 맞지만 없는 날)."""
    manifest = _manifest()
    manifest["consent"]["retention_until"] = "2027-02-30"
    assert recording_manifest_errors(manifest) == ['스키마 consent/retention_until: format "date"']


def test_reversed_time_range_is_rejected(session_dir):
    manifest = _manifest()
    seg = manifest["cameras"][0]["segments"][0]
    seg["capture_time_range_ms"] = list(reversed(seg["capture_time_range_ms"]))
    assert recording_manifest_errors(manifest) == [
        "cameras/0/segments/0/capture_time_range_ms: 범위가 거꾸로다 (from > to)"
    ]
    _write(session_dir, manifest)
    with pytest.raises(ValueError, match="범위가 거꾸로다"):
        load_recording_manifest(session_dir)
    with pytest.raises(ValueError, match="범위가 거꾸로다"):
        recordings_from_manifest(session_dir, manifest, epochs={"cam_A": 0})


def test_duplicate_camera_id_is_rejected_without_loading(session_dir):
    """manifest 를 직접 넘겨도(스키마 검증 없이) camera_id 중복은 거절한다."""
    manifest = _manifest()
    manifest["cameras"].append(json.loads(json.dumps(manifest["cameras"][1])))
    with pytest.raises(ValueError, match="camera_id cam_B 가 겹친다"):
        recordings_from_manifest(session_dir, manifest, epochs={"cam_A": 0})


def test_wrong_contracts_dir_names_the_cause(session_dir, monkeypatch, tmp_path):
    monkeypatch.setenv(CONTRACTS_DIR_ENV, str(tmp_path / "nowhere"))
    with pytest.raises(FileNotFoundError, match=CONTRACTS_DIR_ENV) as exc:
        load_recording_manifest(session_dir)
    assert "nowhere" in str(exc.value)


def _provenance_of(session_dir, purpose):
    manifest = _manifest()
    manifest["consent"] = {"contains_people": True, "purpose": purpose, "retention_until": "2027-01-31"}
    if purpose == "dataset":
        manifest["consent"].update(consent_ref="CF-1", scope=["dataset_internal"])
    _write(session_dir, manifest)
    return VideoFileFrameSource.from_manifest(session_dir, epochs={"cam_A": 0}).provenance()["dataset_id"]


def test_dev_test_recording_is_distinguishable_from_consented_dataset(session_dir):
    sid = _manifest()["session_id"]
    assert _provenance_of(session_dir, "dev_test") == f"recording-dev_test/{sid}"
    assert _provenance_of(session_dir, "dataset") == f"recording/{sid}"
