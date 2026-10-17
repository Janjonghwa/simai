"""시뮬레이터 고정 시나리오(교차, 나란히 걷기) × 시드 3개로 Cross-view Association 을 돌린다 (부록 D-G2-04 의 SIM 대응).

- 같은 카메라의 두 사람이 한 클러스터에 묶이는 경우 0건, 서로 다른 사람이 섞인 클러스터 0건.
- 음성 대조: 같은 카메라 금지 제약을 끄면 나란히 걷기의 모든 시드에서 위 시험이 실패해야 한다(시험이 제약을 실제로
  검사한다는 근거).
- 측정은 tests/tracking/sim_cross_view.py 가 하고 docs/design/tier0-cross-view.md 에 기록한다. 재현율·완전 연결 비율
  하한은 회귀를 잡기 위한 것이고 G2 합격 기준이 아니다. SIM 입력의 상한 성능이다(is_upper_bound).
"""

import importlib.util
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent

# 회귀 하한 (SIM, 상한). 설계 문서 측정표의 최솟값보다 조금 낮게 둔다
MIN_PAIR_RECALL = 0.95
MIN_FULL_ASSOCIATION = 0.90


@pytest.fixture(scope="module")
def sim_cross_view():
    spec = importlib.util.spec_from_file_location("sim_cross_view", HERE / "sim_cross_view.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def outputs(sim_cross_view, tmp_path_factory):
    """시나리오 × 시드 3개의 시뮬레이터 출력. 정상·음성 대조가 함께 쓴다."""
    return sim_cross_view.make_outputs(tmp_path_factory.mktemp("sim-cross-view"))


@pytest.fixture(scope="module")
def results(sim_cross_view, outputs):
    """정상(제약 켬): stable 은 시드 3개 모두, fragmented(트랙 ID 조각남)는 시드 1 만. 전체 격자는 측정 스크립트로 잰다."""
    stable = sim_cross_view.evaluate_all(outputs, True, ("stable",))
    seed1 = {k: v for k, v in outputs.items() if k[1] == 1}
    return stable | sim_cross_view.evaluate_all(seed1, True, ("fragmented",))


@pytest.fixture(scope="module")
def negative(sim_cross_view, outputs):
    side = {k: v for k, v in outputs.items() if k[0] == "side_by_side"}
    return sim_cross_view.evaluate_all(side, False, ("stable",))


def test_grid_is_complete(sim_cross_view, outputs):
    assert set(outputs) == {(s, seed) for s in sim_cross_view.SCENARIOS for seed in sim_cross_view.SEEDS}
    assert len(sim_cross_view.SEEDS) == 3


CASES = [(s, seed, "stable") for s in ("crossing", "side_by_side") for seed in (1, 2, 3)] + [
    (s, 1, "fragmented") for s in ("crossing", "side_by_side")
]


@pytest.mark.parametrize(("scenario", "seed", "variant"), CASES)
def test_no_false_merge(results, scenario, seed, variant):
    r = results[f"{scenario}/{seed}/{variant}"]
    assert r["input_provenance"]["is_upper_bound"] is True
    assert r["input_provenance"]["frames"]["source"] == "SIM"
    assert r["same_camera_exclusive"] is True
    assert r["person_ticks"] > 100
    assert r["same_camera_merges"] == 0
    assert r["impure_clusters"] == 0
    assert r["pair_fp"] == 0
    assert r["pair_recall"] >= MIN_PAIR_RECALL
    assert r["full_association_ratio"] >= MIN_FULL_ASSOCIATION


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_negative_control_fails_without_constraint(negative, seed):
    """같은 카메라 금지 제약을 빼면 나란히 걷기에서 같은 카메라 두 사람 오병합이 생긴다 (위 시험이 실패한다)."""
    r = negative[f"side_by_side/{seed}/stable"]
    assert r["same_camera_exclusive"] is False
    assert r["same_camera_merges"] > 0
    assert r["impure_clusters"] > 0
