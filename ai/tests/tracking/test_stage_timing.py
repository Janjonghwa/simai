from simai_ai.tracking.stage_timing import StageTiming


def test_wall_cpu_people_buckets_are_bounded_and_use_injected_clocks():
    wall, cpu = [0.], [0.]
    timing = StageTiming(lambda: wall[0], lambda: cpu[0], capacity=2)
    for i in range(3):
        with timing.measure("tick", 4):
            wall[0] += .1 * (i + 1)
            cpu[0] += .01
    timing.record("decode", None, 2)
    timing.record("tick", 20, 1)
    summary = timing.summary()
    assert summary["tick"]["4"] == {
        "count": 3, "window_samples": 2,
        "wall_ms": {"mean": 250., "p50": 200., "p95": 300., "max": 300.},
        "thread_cpu_ms": {"mean": 10., "p50": 10., "p95": 10., "max": 10.},
    }
    assert "unknown" in summary["decode"] and "7+" in summary["tick"]
