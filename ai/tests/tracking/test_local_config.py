"""Local Tracker 설정: 기본 TrackTrack, 명시 키, GMC 끔, ReID 제한 (명세 5.1, 5.2 수정 7)."""

from pathlib import Path

import pytest

from simai_ai.tracking.local_config import (
    DEFAULT_PARAMS,
    DEFAULT_TRACKER_KIND,
    TrackerConfig,
    TrackerKind,
    tracker_config,
)


def test_default_is_tracktrack():
    assert DEFAULT_TRACKER_KIND is TrackerKind.TRACKTRACK
    assert tracker_config().kind is TrackerKind.TRACKTRACK


@pytest.mark.parametrize("kind", list(TrackerKind))
def test_defaults_are_valid_and_explicit(kind):
    config = tracker_config(kind)
    assert config.params["tracker_type"] == kind.value
    assert set(config.as_dict()) == set(DEFAULT_PARAMS[kind])
    if "gmc_method" in config.params:
        assert config.params["gmc_method"] == "none"


@pytest.mark.parametrize("kind", [TrackerKind.TRACKTRACK, TrackerKind.BOTSORT])
def test_camera_motion_compensation_must_be_off(kind):
    with pytest.raises(ValueError, match="gmc_method"):
        tracker_config(kind, gmc_method="sparseOptFlow")


def test_missing_key_is_rejected_not_defaulted():
    params = dict(DEFAULT_PARAMS[TrackerKind.TRACKTRACK])
    del params["lost_match_thr"]
    with pytest.raises(ValueError, match="빠짐"):
        TrackerConfig(TrackerKind.TRACKTRACK, params)


def test_unknown_key_is_rejected():
    with pytest.raises(ValueError):
        tracker_config(TrackerKind.BYTETRACK, gmc_method="none")
    params = {**DEFAULT_PARAMS[TrackerKind.BYTETRACK], "typo_thresh": 0.3}
    with pytest.raises(ValueError, match="모름"):
        TrackerConfig(TrackerKind.BYTETRACK, params)


def test_external_reid_model_is_rejected():
    assert tracker_config("botsort", with_reid=True).with_reid
    with pytest.raises(ValueError, match="ReID"):
        tracker_config("botsort", with_reid=True, model="osnet.pt")


@pytest.mark.parametrize(
    ("key", "value"),
    [("track_high_thresh", 1.5), ("track_buffer", 0), ("track_buffer", 2.5), ("fuse_score", 1), ("match_thresh", -0.1)],
)
def test_bad_values(key, value):
    with pytest.raises((ValueError, TypeError)):
        tracker_config("bytetrack", **{key: value})


def test_low_above_high_rejected():
    with pytest.raises(ValueError, match="track_low_thresh"):
        tracker_config("bytetrack", track_low_thresh=0.5, track_high_thresh=0.4)


def test_switch_by_config_mapping():
    doc = dict(DEFAULT_PARAMS[TrackerKind.BYTETRACK])
    assert TrackerConfig.from_mapping(doc).kind is TrackerKind.BYTETRACK
    with pytest.raises(ValueError, match="tracker_type"):
        TrackerConfig(TrackerKind.BOTSORT, {**DEFAULT_PARAMS[TrackerKind.BOTSORT], "tracker_type": "bytetrack"})
    with pytest.raises(ValueError):
        TrackerConfig.from_mapping({"track_buffer": 30})


def test_params_are_read_only():
    config = tracker_config()
    with pytest.raises(TypeError):
        config.params["track_buffer"] = 10  # type: ignore[index]
    copy = config.as_dict()
    copy["track_buffer"] = 10
    assert config.params["track_buffer"] == 30


@pytest.mark.parametrize("kind", list(TrackerKind))
def test_keys_cover_installed_ultralytics_yaml(kind):
    """설치된 ultralytics 의 추적기 YAML 키를 모두 명시한다 (새 키가 생기면 기본값에 기대지 않도록 여기서 잡는다)."""
    ultralytics = pytest.importorskip("ultralytics")
    yaml = pytest.importorskip("yaml")
    path = Path(ultralytics.__file__).parent / "cfg" / "trackers" / f"{kind.value}.yaml"
    packaged = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert set(packaged) == set(DEFAULT_PARAMS[kind])
