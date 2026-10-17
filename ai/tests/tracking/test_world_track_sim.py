"""시뮬레이터 시나리오(교차, 나란히 걷기, 가림, 퇴장 재진입, 로컬 트래커 ID 전환)로 World Track Manager 의 ID 유지를 잰다 (G2 ID 유지 시간 비율의
SIM 대응, 부록 D-G2-04).

- ID 재사용 0건, 같은 사람 두 World ID 동시 확정 0건, 두 사람 한 ID 병합 0건, world_state 계약 스키마 오류 0건.
- ID 유지 비율 하한은 회귀를 잡기 위한 것이고 G2 합격 기준이 아니다. **SIM 입력의 상한 성능(is_upper_bound)이며 결론이
  아니다.** 측정표는 tests/tracking/sim_world_track.py 로 재고 docs/design/tier0-world-track.md 에 기록한다.
- id_switch(교차 중 cam1 의 두 사람 로컬 트랙 ID 맞바꿈)는 두 사람이 한 ID 로 합쳐지는 회귀를 잡는다(리뷰 F1).
- 시드 1 만 돈다(시드 2·3 은 측정 스크립트).
"""

import importlib.util
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SEED = 1
# 회귀 하한 (SIM, 상한). 설계 문서 측정표(모두 1.000)보다 조금 낮게 둔다
MIN_RETENTION = 0.95


@pytest.fixture(scope="module")
def sim_world_track():
    spec = importlib.util.spec_from_file_location("sim_world_track", HERE / "sim_world_track.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def results(sim_world_track, tmp_path_factory, contract_errors):
    outputs = sim_world_track.make_outputs(tmp_path_factory.mktemp("sim-world-track"), seeds=(SEED,))
    return sim_world_track.evaluate_all(outputs, seeds=(SEED,), validator=contract_errors)


@pytest.mark.parametrize(
    "scenario", ["crossing", "side_by_side", "occlusion", "exit_reentry", "exit_long", "id_switch"]
)
def test_no_reuse_no_duplicate_no_merge(results, scenario):
    r = results[f"{scenario}/{SEED}"]
    assert r["input_provenance"]["is_upper_bound"] is True
    assert r["input_provenance"]["frames"]["source"] == "SIM"
    assert r["trackable"] > 90
    assert r["schema_errors"] == 0
    assert r["id_reuse"] == 0
    assert r["duplicate_confirmed"] == 0
    assert r["merged_ids"] == 0
    assert r["revisions"] == 0  # 자동 병합·교정을 하지 않는다 (S15P31A101-40)
    # 클러스터 섞임(Cross-view 단계)·다수 없는 갱신: 측정표 기준 0건. 생기면 위 지표가 그 tick 을 판정하지 못한다
    assert r["impure_updates"] == 0
    assert r["undecided_updates"] == 0


@pytest.mark.parametrize("scenario", ["crossing", "side_by_side", "occlusion", "exit_reentry", "id_switch"])
def test_id_retention_regression_floor(results, scenario):
    r = results[f"{scenario}/{SEED}"]
    assert r["id_retention"] >= MIN_RETENTION
    assert r["issued"] == 2  # 가림·소실 시간보다 짧은 퇴장 뒤에도 같은 ID


def test_long_exit_gets_new_id_without_reuse(results):
    """소실 시간(3 s)보다 오래 사라지면 새 ID 다. 늦은 재연결(S15P31A101-40) 전의 지금 동작이다."""
    r = results[f"exit_long/{SEED}"]
    east = r["per_person"]["east"]
    assert len(east["ids"]) == 2
    assert r["issued"] == 3
    assert r["id_reuse"] == 0
