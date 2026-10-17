"""설정만 바꿔 SIM·DATASET_GT·TIER0_LOG WorldTrackProvider 를 바꿔 쓰는지 본다 (명세 20.4 입력 대체 계약).

소비 코드(`_consume`)는 한 벌이고 설정의 source 만 다르다. 출처는 input_provenance 로 그대로 가고,
정답·시뮬레이션 입력만 is_upper_bound 가 참이다(규칙 2·3).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from simai_ai.providers import WorldTrackProvider, build_input_provenance, contract
from simai_ai.providers.select import world_track_provider_from_config
from simai_ai.providers.testing import assert_no_violations, example_path, load_example

AI_DIR = Path(__file__).resolve().parents[2]
PANOPTIC_MINI = Path(__file__).resolve().parent / "dataset_gt" / "panoptic_mini"


@pytest.fixture(scope="module")
def sim_out(tmp_path_factory) -> Path:
    """내장 시나리오 basic 을 `python -m sim.run` 으로 돌린 출력 (sim 은 설치 대상이 아니라 실행 명령으로 쓴다)."""
    out = tmp_path_factory.mktemp("sim-basic")
    result = subprocess.run(
        [sys.executable, "-m", "sim.run", "--scenario", "basic", "--out", str(out)],
        cwd=AI_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=dict(os.environ, PYTHONUTF8="1"),
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return out


@pytest.fixture
def tier0_log(tmp_path) -> Path:
    """contracts/examples 의 world_state·observation_batch (예시가 줄여 적은 관절 배열만 COCO17 길이로 늘림)."""
    ws = load_example("world_state.json")
    for tr in ws["payload"]["tracks"]:
        sk = tr.get("skeleton")
        if sk:
            n = 17 - len(sk["joints"])
            sk["joints"] += [None] * n
            sk["visible"] += [False] * n
            sk["validity"] += ["UNAVAILABLE"] * n
            sk["joint_source"] += ["observed"] * n
    ob = load_example("observation_batch.json")
    for f in ob["payload"]["frames"]:
        for det in f["detections"]:
            det["keypoints_2d"] += [None] * (17 - len(det["keypoints_2d"]))
    path = tmp_path / "i1.jsonl"
    path.write_text("\n".join(json.dumps(m) for m in (ws, ob)) + "\n", encoding="utf-8")
    return path


def _configs(sim_out: Path, tier0_log: Path) -> dict[str, dict]:
    return {
        "SIM": {"source": "SIM", "sim": {"path": str(sim_out)}},
        "DATASET_GT": {"source": "DATASET_GT", "dataset_gt": {"dataset": "panoptic", "root": str(PANOPTIC_MINI)}},
        "TIER0_LOG": {"source": "TIER0_LOG", "tier0_log": {"log_paths": [str(tier0_log)]}},
    }


def _consume(provider: WorldTrackProvider) -> dict:
    """EXP 쪽 소비 코드 흉내: 출처를 모르고 문서를 읽어 결과물의 input_provenance 를 만든다."""
    docs = list(provider.frames())
    provenance = build_input_provenance({"source": "FILE"}, tracks=provider.provenance())
    return {"docs": docs, "input_provenance": provenance}


@pytest.mark.parametrize(("source", "upper_bound"), [("SIM", True), ("DATASET_GT", True), ("TIER0_LOG", False)])
def test_switch_by_config(sim_out, tier0_log, source, upper_bound):
    provider = world_track_provider_from_config(_configs(sim_out, tier0_log)[source])
    assert provider.source == source
    assert_no_violations(contract.check_world_track_provider(provider))
    result = _consume(provider)
    assert result["docs"]
    assert {d["provider_source"] for d in result["docs"]} == {source}
    assert result["input_provenance"]["tracks"] == provider.provenance()
    assert result["input_provenance"]["is_upper_bound"] is upper_bound
    assert_no_violations(contract.check_provenance_carries(result["input_provenance"], track_provider=provider))


def test_sim_accepts_file_path(sim_out):
    provider = world_track_provider_from_config({"source": "SIM", "sim": {"path": str(sim_out / "world_tracks.jsonl")}})
    assert provider.source == "SIM"


def test_dataset_gt_file(tmp_path):
    provider = world_track_provider_from_config(
        {"source": "DATASET_GT", "dataset_gt": {"dataset": "file", "path": str(example_path("world_tracks.json"))}}
    )
    assert provider.source == "DATASET_GT"


def test_file_source_must_match_config(tmp_path):
    """DATASET_GT 파일을 SIM 이라고 설정하면 거부한다 (출처가 틀리면 상한 표시가 틀어진다)."""
    with pytest.raises(ValueError, match="provider_source"):
        world_track_provider_from_config({"source": "SIM", "sim": {"path": str(example_path("world_tracks.json"))}})
    doc = load_example("world_tracks.json")
    doc.update(provider_source="TIER0_LOG")
    path = tmp_path / "t.jsonl"
    path.write_text(json.dumps(doc) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="provider_source"):
        world_track_provider_from_config({"source": "DATASET_GT", "dataset_gt": {"dataset": "file", "path": str(path)}})


@pytest.mark.parametrize(
    "cfg",
    [
        {"source": "TIER0_LIVE", "tier0_live": {}},
        {"source": "SIM"},
        {"source": "SIM", "sim": {"path": "x"}, "tier0_log": {}},
        {"source": "SIM", "sim": {"path": "x", "extra": 1}},
        {"source": "DATASET_GT", "dataset_gt": {"dataset": "mpii"}},
        {"source": "DATASET_GT", "dataset_gt": {"dataset": "panoptic"}},
    ],
)
def test_rejects_bad_config(cfg):
    with pytest.raises(ValueError):
        world_track_provider_from_config(cfg)
