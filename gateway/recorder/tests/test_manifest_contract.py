"""manifest 검증이 계약 스키마(contracts/data/recording_manifest.schema.json)로 도는지 (S15P31A101-158)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from simai_recorder.cli import main
from simai_recorder.contract import CONTRACTS_DIR_ENV, ContractsNotFoundError, contracts_dir, schema_errors
from simai_recorder.manifest import validate_consent, validate_manifest, validate_storage_uri

EXAMPLES = contracts_dir() / "examples"


def _load(path: Path) -> dict:
    return {k: v for k, v in json.loads(path.read_text(encoding="utf-8")).items() if not k.startswith("_")}


@pytest.fixture
def example() -> dict:
    return _load(EXAMPLES / "recording_manifest.json")


@pytest.mark.parametrize("name", ["recording_manifest.json", "recording_manifest.test_input.json"])
def test_contract_examples_pass(name):
    assert validate_manifest(_load(EXAMPLES / name)) == []


@pytest.mark.parametrize("path", sorted((EXAMPLES / "invalid").glob("recording_manifest.*.json")), ids=lambda p: p.name)
def test_contract_violations_fail(path):
    assert validate_manifest(_load(path))


def test_secret_in_url_is_reported_without_echoing_it(example):
    example["cameras"][0]["source"]["url"] = "rtsp://simai:p%40ss@192.168.0.11:554/stream1"
    errors = validate_manifest(example)
    assert len(errors) == 1 and errors[0].startswith("cameras/0/source/url: 형식이 맞지 않다")
    assert "simai" not in errors[0] and "p%40ss" not in errors[0]


def test_camera_kind_must_be_listed_in_input_kinds(example):
    """로컬 파일 카메라가 섞였는데 input_kinds 가 RTSP 뿐이면 is_test_input: false 로 통과하지 못한다 (AGENTS 규칙 7)."""
    example["cameras"][1]["source"] = {"kind": "FILE", "file_name": "cam_B.mp4"}
    assert validate_manifest(example) == [
        'input_kinds: {"const": "FILE"} 항목이 있어야 한다 (FILE 카메라가 있으면 input_kinds 에 FILE이 있어야 한다)'
    ]
    example["input_kinds"] = ["RTSP", "FILE"]  # 종류를 맞추면 is_test_input 이 true 여야 한다
    assert validate_manifest(example) == ["is_test_input: true 이어야 한다"]
    example["is_test_input"] = True
    assert validate_manifest(example) == []


def test_reversed_time_range_is_caught_outside_the_schema(example):
    """시각 범위 역전은 JSON Schema 로 표현할 수 없어 validate_manifest 가 따로 본다."""
    seg = example["cameras"][0]["segments"][0]
    seg["capture_time_range_ms"] = list(reversed(seg["capture_time_range_ms"]))
    assert schema_errors(example) == []
    assert validate_manifest(example) == ["cameras/0/segments/0/capture_time_range_ms: 범위가 거꾸로다 (from > to)"]


def test_duplicate_camera_id_is_caught(example):
    example["cameras"].append(copy.deepcopy(example["cameras"][0]))
    assert validate_manifest(example) == ["cameras: camera_id cam_A 가 겹친다"]


def test_retention_date_must_exist(example):
    example["consent"]["retention_until"] = "2027-02-30"  # 패턴은 맞지만 없는 날
    assert validate_manifest(example) == ["consent/retention_until: date 형식이어야 한다"]


def test_consent_rules_before_recording():
    assert validate_consent(None, ["FILE", "SYNTHETIC"]) == []
    assert validate_consent(None, ["RTSP"]) == [
        "consent: 형식이 object 이어야 한다 (실제 카메라(RTSP) 녹화는 consent(보관 기한·사람 포함 여부)가 있어야 한다 (명세 11.5))"
    ]
    assert validate_consent({"contains_people": False, "retention_until": "2027-01-31"}, ["RTSP"]) == []
    people = {"contains_people": True, "retention_until": "2027-01-31", "consent_ref": "CF-1", "scope": ["nowhere"]}
    assert validate_consent(people, ["RTSP"]) == [
        (
            'consent/scope/0: ["dataset_internal", "runpod_ap_jp_1", "runpod_us", "aws_seoul", "presentation", '
            '"hmr_texture"] 중 하나여야 한다'
        )
    ]


def test_storage_uri_rule():
    assert validate_storage_uri("volume://AP-JP-1/simai/recordings") == []
    assert validate_storage_uri("s3://simai-bucket/recordings/") == []
    assert validate_storage_uri("C:/Users/someone/videos")


def test_wrong_contracts_dir_names_the_cause(monkeypatch, tmp_path):
    monkeypatch.setenv(CONTRACTS_DIR_ENV, str(tmp_path / "nowhere"))
    with pytest.raises(ContractsNotFoundError, match="nowhere"):
        contracts_dir()


def test_cli_reports_missing_contracts_without_traceback(monkeypatch, tmp_path, capsys, example):
    (tmp_path / "manifest.json").write_text(json.dumps(example), encoding="utf-8")
    monkeypatch.setenv(CONTRACTS_DIR_ENV, str(tmp_path / "nowhere"))
    assert main(["verify", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert err.startswith("설정 오류:") and CONTRACTS_DIR_ENV in err and "Traceback" not in err
