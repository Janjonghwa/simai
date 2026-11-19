"""Tier 0 Re-ID 경로 설정: 설정 파일 값이 먼저, 없으면 환경변수(Pod), 둘 다 없으면 None (S15P31A101-471)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from services.tier0.settings import Tier0ConfigError, load_settings


def _config(tmp_path: Path, reid: dict | None = None) -> Path:
    doc: dict = {"input": {"mode": "sim", "run_dir": "."}}
    if reid is not None:
        doc["reid"] = reid
    p = tmp_path / "tier0.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def test_reid_paths_fall_back_to_env(tmp_path):
    reid = tmp_path / "reid"  # 운영 체제마다 절대 경로 표기가 달라 임시 폴더 기준으로
    env = {
        "SIMAI_COPE_ROOT": str(reid / "cope"),
        "SIMAI_COPE_WEIGHTS": str(reid / "weights" / "cope_msmt17.pth"),
        "SIMAI_COPE_CLIP_PATH": str(reid / "clip-cache" / "ViT-B-16.pt"),
    }
    r = load_settings(_config(tmp_path), env=env).reid
    assert r.cope_root == reid / "cope"
    assert r.weights_path == reid / "weights" / "cope_msmt17.pth"
    assert r.clip_model_path == reid / "clip-cache" / "ViT-B-16.pt"


def test_reid_config_file_wins_over_env(tmp_path):
    in_file = tmp_path / "opt" / "cope"
    cfg = _config(tmp_path, {"cope_root": str(in_file)})
    r = load_settings(cfg, env={"SIMAI_COPE_ROOT": str(tmp_path / "env" / "cope")}).reid
    assert r.cope_root == in_file


def test_reid_paths_none_without_config_or_env(tmp_path):
    r = load_settings(_config(tmp_path), env={}).reid
    assert r.cope_root is None and r.weights_path is None and r.clip_model_path is None


def test_reid_face_aux_switch(tmp_path):
    assert not load_settings(_config(tmp_path), env={}).reid.use_face_aux
    assert load_settings(_config(tmp_path), env={"SIMAI_TIER0_REID_FACE": "1"}).reid.use_face_aux
    assert load_settings(_config(tmp_path, {"use_face_aux": True}), env={}).reid.use_face_aux
    assert not load_settings(_config(tmp_path, {"use_face_aux": True}), env={"SIMAI_TIER0_REID_FACE": "0"}).reid.use_face_aux


def test_reid_demand_sampling_defaults_and_overrides(tmp_path):
    r = load_settings(_config(tmp_path)).reid
    assert (r.stable_update_interval_ms, r.max_crops_per_second) == (3000, 8)
    r = load_settings(_config(tmp_path, {"stable_update_interval_ms": 5000, "max_crops_per_second": 4})).reid
    assert (r.stable_update_interval_ms, r.max_crops_per_second) == (5000, 4)


def test_reid_environment_overrides_and_validation(tmp_path):
    cfg = _config(tmp_path, {"fp16": False, "update_interval_ms": 800, "max_crops_per_second": 7})
    r = load_settings(cfg, env={"SIMAI_TIER0_REID_FP16": "1", "SIMAI_TIER0_REID_UPDATE_INTERVAL_MS": "1200",
                                "SIMAI_TIER0_REID_MAX_CROPS_PER_SECOND": "5"}).reid
    assert (r.fp16, r.update_interval_ms, r.max_crops_per_second) == (True, 1200, 5)
    for env in ({"SIMAI_TIER0_REID_FP16": "yes"}, {"SIMAI_TIER0_REID_MAX_CROPS_PER_SECOND": "0"},
                {"SIMAI_TIER0_REID_UPDATE_INTERVAL_MS": "bad"}, {"SIMAI_TIER0_REID_UPDATE_INTERVAL_MS": "3001"}):
        with pytest.raises(Tier0ConfigError):
            load_settings(cfg, env=env)


def test_collector_passes_fp16_to_cope():
    pytest.importorskip("numpy")
    from simai_ai.reid.collector import AsyncReidCollector, ReidCollectorConfig

    collector = AsyncReidCollector(ReidCollectorConfig(enabled=False, fp16=True), start_worker=False)
    assert collector._cope_config().fp16 is True
    assert ReidCollectorConfig.from_env({"SIMAI_REID_FP16": "1"}).fp16 is True
    assert not ReidCollectorConfig().fp16
