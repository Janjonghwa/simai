"""시뮬레이터 40 cm 단상 시나리오 3회 반복(시드 1·2·3)으로 ContactEstimator 를 본다 (부록 D-G2-03·05·07, 명세 15.1 G2).

- 단상 위 접지를 바닥으로 오인 0건(D-G2-05), 한 카메라에만 보이는 단상 위 사람이 Z=0 으로 내려앉는 경우 0건(D-G2-03).
- 작업면 미등록이면 단상 위 사람은 UNKNOWN(바닥 아님), 유지 상한이 지나면 UNKNOWN(바닥 아님).
- world_state 계약 스키마 오류 0건(시드 1).
- **SIM 입력의 상한 성능(is_upper_bound)이며 G2 합격이나 결론이 아니다.** 측정표는 tests/contact/sim_contact.py 로 잰다.
  UNKNOWN 비율(D-G2-07)은 측정·기록 항목이라 회귀 상한만 둔다.
"""

import importlib.util
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SEEDS = (1, 2, 3)
SCHEMA_SEED = 1
SCENARIOS = ("podium40", "single_view", "long_hide", "short_hold", "unregistered")


@pytest.fixture(scope="module")
def sim_contact():
    spec = importlib.util.spec_from_file_location("sim_contact", HERE / "sim_contact.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def results(sim_contact, tmp_path_factory, contract_errors):
    outputs = sim_contact.make_outputs(tmp_path_factory.mktemp("sim-contact"), seeds=SEEDS)
    # 계약 스키마 검증은 느려서(tick 마다 jsonschema) 시드 1 에서만 한다
    return {
        f"{s}/{seed}": sim_contact.evaluate(out, s, contract_errors if seed == SCHEMA_SEED else None)
        for s in SCENARIOS
        for seed, out in outputs.items()
    }


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_podium_never_floor_never_z0(results, scenario, seed):
    r = results[f"{scenario}/{seed}"]
    assert r["input_provenance"]["is_upper_bound"] is True  # SIM 상한
    assert r["input_provenance"]["frames"]["source"] == "SIM"
    assert r["podium_ticks"] > 100
    assert r["floor_misread"] == 0  # D-G2-05
    assert r["z0_collapse"] == 0  # D-G2-03
    assert r["wrong_surface"] == 0
    assert r["anchor_z_max_error_m"] == 0.0  # 접지점 Z 는 등록 상판 높이(또는 바닥 0)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_world_state_matches_contract(results, scenario):
    assert results[f"{scenario}/{SCHEMA_SEED}"]["schema_errors"] == 0


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("scenario", ["single_view", "long_hide", "short_hold"])
def test_single_view_podium_person_holds_height(results, scenario, seed):
    r = results[f"{scenario}/{seed}"]
    assert r["single_view_podium_ticks"] > 10  # 한 카메라에만 보인 단상 위 tick 이 실제로 있었다
    assert r["single_view_z0_collapse"] == 0
    assert r["held"] > 10


@pytest.mark.parametrize("seed", SEEDS)
def test_hold_ends_in_unknown_not_floor(results, seed):
    r = results[f"short_hold/{seed}"]
    assert r["hold_expired"] >= 1
    assert r["unknown"] > results[f"long_hide/{seed}"]["unknown"]  # 만료 뒤 UNKNOWN 으로 남는다(바닥 아님, 위 시험)


@pytest.mark.parametrize("seed", SEEDS)
def test_unregistered_podium_is_unknown(results, seed):
    r = results[f"unregistered/{seed}"]
    assert r["unknown"] >= r["podium_ticks"] - 5
    assert r["floor_misread"] == 0 and r["z0_collapse"] == 0


@pytest.mark.parametrize("seed", SEEDS)
def test_unknown_ratio_regression_ceiling(results, seed):
    """D-G2-07 는 측정·기록 항목이다. 측정표(0.034~0.053)보다 넉넉한 회귀 상한만 둔다."""
    assert results[f"podium40/{seed}"]["unknown_ratio"] <= 0.10
