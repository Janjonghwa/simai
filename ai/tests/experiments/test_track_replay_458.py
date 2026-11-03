"""오연결 수동 검수의 미확인 값을 0으로 오인하지 않도록 한다(-458)."""

import pytest

from experiments.track_replay.replay_tracking import compute_metrics, review_metrics


def test_unreviewed_and_partially_reviewed_connections():
    events = [{"event_id": "a", "kind": "two_view"}, {"event_id": "b", "kind": "two_view"},
              {"event_id": "c", "kind": "reconnection"}]
    empty = review_metrics(events, [])
    assert empty["two_view"]["false_connections"] is None
    assert empty["reconnection"]["reviewed_false_ratio"] is None
    result = review_metrics(events, [{"event_id": "a", "false_connection": True},
                                     {"event_id": "c", "false_connection": False}])
    assert result["two_view"] == {"candidates": 2, "reviewed": 1, "false_connections": 1,
                                  "reviewed_false_ratio": 1.0, "coverage": 0.5}
    assert result["reconnection"]["false_connections"] == 0
    assert result["reconnection"]["coverage"] == 1.0


@pytest.mark.parametrize("labels", [
    [{"event_id": "unknown", "false_connection": True}],
    [{"event_id": "a", "false_connection": None}],
    [{"event_id": "a", "false_connection": False}, {"event_id": "a", "false_connection": True}],
])
def test_review_rejects_unknown_duplicate_or_missing_judgment(labels):
    with pytest.raises(ValueError):
        review_metrics([{"event_id": "a", "kind": "two_view"}], labels)


def test_other_world_can_disable_1029_zone_proxy():
    result = compute_metrics([], {"zone_xy": []}, 0)
    assert result["zone_proxy"] == {"id_entries": None, "occupancy_episodes": None, "duplicates": None}
