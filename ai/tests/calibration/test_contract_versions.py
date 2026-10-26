"""보정 출처·스케일 근거 값과 그 계약 버전의 회귀 시험 (contracts 2.0.0, 명세 4.3·20.4).

v1.3 에서 뺀 값(마커 보정·평면도 대응점 출처, 마커 보드·실측 평면도 스케일 근거)이 계약이나 코드에 다시 들어오거나,
공통 enum 을 쓰는 계약의 버전이 메이저로 맞춰지지 않으면 실패한다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from simai_ai.calibration.criteria import ScaleBasis
from simai_ai.providers import contract
from simai_ai.providers.testing import load_example

AI_DIR = Path(__file__).resolve().parents[2]

CALIBRATION_SOURCES = {"G1_AUTO", "DATASET_GT", "SIM", "MODEL_ESTIMATE"}
SCALE_BASES = {
    "MEASURED_OBJECT",
    "MEASURED_CAMERA_HEIGHT",
    "KNOWN_PERSON_HEIGHT",
    "HUMAN_HEIGHT_STATS",
    "STANDARD_OBJECT",
    "MODEL_ESTIMATE",
    "DATASET_GT",
}
CALIBRATION_MAJOR = 2

# calibration_source(common/defs)를 직접·input_provenance 로 쓰는 계약의 정상 예시
EXAMPLES_USING_CALIBRATION_SOURCE = [
    "calibration.json",
    "calibration.g1_auto.json",
    "calibration_update.json",
    "run_record.json",
    "run_record.image_only.json",
    "run_record.known_model_estimate.json",
    "scene_output.json",
    "human_mesh_output.avatar_asset.json",
    "human_mesh_output.pose_frame.json",
]


def _schema(rel: str) -> dict:
    return json.loads((contract.contracts_dir() / rel).read_text(encoding="utf-8"))


def _major(version: str) -> int:
    return int(version.split(".")[0])


def test_calibration_source_enum_is_v13_set():
    assert set(_schema("common/defs.schema.json")["$defs"]["calibration_source"]["enum"]) == CALIBRATION_SOURCES
    assert contract.CALIBRATED_SOURCES | contract.NEVER_CALIBRATED_SOURCES == CALIBRATION_SOURCES


def test_scale_basis_enum_matches_code():
    enum = set(_schema("providers/calibration.schema.json")["$defs"]["scale_basis"]["enum"])
    assert enum == SCALE_BASES == {b.value for b in ScaleBasis}
    assert contract.PRIMARY_SCALE_BASES <= enum and contract.CROSS_CHECK_SCALE_BASES <= enum


@pytest.mark.parametrize("name", EXAMPLES_USING_CALIBRATION_SOURCE)
def test_examples_using_calibration_source_are_major_2(name):
    assert _major(load_example(name)["schema_version"]) >= CALIBRATION_MAJOR


@pytest.mark.parametrize(
    ("rel", "const"),
    [
        ("sim/simulate.py", "CALIBRATION_SCHEMA_VERSION"),
        ("experiments/gpu_queue/run_record.py", "SCHEMA_VERSION"),
    ],
)
def test_generators_emit_major_2(rel, const):
    """보정 문서·Run 기록을 만드는 코드의 계약 버전. 공용 CONTRACTS_VERSION(world_tracks 등)은 따라 올리지 않는다."""
    assert _major(contract.CALIBRATION_SCHEMA_VERSION) == CALIBRATION_MAJOR
    text = (AI_DIR / rel).read_text(encoding="utf-8")
    m = re.search(rf'^{const} = "(\d+\.\d+\.\d+)"', text, re.MULTILINE)
    assert m, f"{rel} 에 {const} 가 없다"
    assert _major(m.group(1)) == CALIBRATION_MAJOR


def test_world_axes_source_enum_matches_code():
    """World 축 출처(2.1.0)는 계약 enum 과 코드 상수가 같다. 생략할 수 있는 선택 필드다."""
    schema = _schema("providers/calibration.schema.json")
    assert tuple(schema["properties"]["world_axes_source"]["enum"]) == contract.WORLD_AXES_SOURCES
    assert "world_axes_source" not in schema["required"]
