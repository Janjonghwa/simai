"""시뮬레이터 시나리오(교차, 나란히 걷기, 가림, 퇴장 재진입, 로컬 트래커 ID 전환)로 World Track Manager 의 ID 유지를 잰다 (G2 ID 유지 시간 비율의
SIM 대응, 부록 D-G2-04).

- ID 재사용 0건, 같은 사람 두 World ID 동시 확정 0건, 두 사람 한 ID 병합 0건, world_state 계약 스키마 오류 0건.
- ID 유지 비율 하한은 회귀를 잡기 위한 것이고 G2 합격 기준이 아니다. **SIM 입력의 상한 성능(is_upper_bound)이며 결론이
  아니다.** 측정표는 tests/tracking/sim_world_track.py 로 재고 docs/design/tier0-world-track.md 에 기록한다.
- id_switch(교차 중 cam1 의 두 사람 로컬 트랙 ID 맞바꿈)는 두 사람이 한 ID 로 합쳐지는 회귀를 잡는다(리뷰 F1).
- occlusion_long·exit_long_revive 는 늦은 재연결(같은 ID)과 ID 교정(병합)을 본다(S15P31A101-40, 두 카메라 근거).
  occlusion_long_single 은 옛 로컬 트랙이 카메라 하나뿐이라 잇지 않는 것을 본다(리뷰 F1). LOST 뒤 같은 ID 로 돌아온
  것은 늦은 재연결·교정 기록이 있을 때만 ID 재사용이 아니다.
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


ALL = [
    "crossing",
    "side_by_side",
    "occlusion",
    "exit_reentry",
    "exit_long",
    "id_switch",
    "occlusion_long",
    "occlusion_long_single",
    "exit_long_revive",
]


@pytest.mark.parametrize("scenario", ALL)
def test_no_reuse_no_duplicate_no_merge(results, scenario):
    r = results[f"{scenario}/{SEED}"]
    assert r["input_provenance"]["is_upper_bound"] is True
    assert r["input_provenance"]["frames"]["source"] == "SIM"
    assert r["trackable"] > 90
    assert r["schema_errors"] == 0
    assert r["id_reuse"] == 0
    assert r["duplicate_confirmed"] == 0
    assert r["merged_ids"] == 0
    # ID 교정은 옛 로컬 트랙이 되살아나는 exit_long_revive 에서만 (한 사람 두 ID → 먼저 만든 ID)
    assert r["revisions"] == (1 if scenario == "exit_long_revive" else 0)
    assert r["reassociations"] == (1 if scenario in ("occlusion_long", "exit_long_revive") else 0)
    # 클러스터 섞임(Cross-view 단계)·다수 없는 갱신: 측정표 기준 0건. 생기면 위 지표가 그 tick 을 판정하지 못한다
    assert r["impure_updates"] == 0
    assert r["undecided_updates"] == 0


@pytest.mark.parametrize(
    "scenario", ["crossing", "side_by_side", "occlusion", "exit_reentry", "id_switch", "occlusion_long"]
)
def test_id_retention_regression_floor(results, scenario):
    r = results[f"{scenario}/{SEED}"]
    assert r["id_retention"] >= MIN_RETENTION
    assert r["issued"] == 2  # 가림·소실 시간보다 짧은 퇴장 뒤에도 같은 ID


def test_long_exit_gets_new_id_without_reuse(results):
    """소실 시간(3 s)보다 오래 사라지고 로컬 트랙 근거 없이(모든 카메라 새 로컬 트랙) 돌아오면 새 ID 다(위치만으로 잇지 않음)."""
    r = results[f"exit_long/{SEED}"]
    east = r["per_person"]["east"]
    assert len(east["ids"]) == 2
    assert r["issued"] == 3
    assert r["id_reuse"] == 0


def test_long_exit_with_revived_local_track_is_corrected_to_first_id(results):
    """새 ID 를 받은 뒤 cam1 이 옛 로컬 트랙을 되살리면 옛 ID 로 교정된다(S15P31A101-40). 끝까지 보면 주 ID 는 처음 ID 다."""
    r = results[f"exit_long_revive/{SEED}"]
    east = r["per_person"]["east"]
    assert r["issued"] == 3 and r["revisions"] == 1
    assert east["main_id"] == min(east["ids"])  # 먼저 받은 ID
    assert r["id_retention"] > results[f"exit_long/{SEED}"]["id_retention"]


def test_single_camera_history_is_not_reconnected(results):
    """옛 로컬 트랙이 cam1 하나뿐이면 로컬 ID 전환과 구별할 수 없어 잇지 않는다(새 ID, 돌아온 동안 IDENTITY_UNCERTAIN)."""
    r = results[f"occlusion_long_single/{SEED}"]
    assert r["issued"] == 3 and r["reassociations"] == 0 and r["revisions"] == 0
    assert r["uncertain_track_ticks"] >= 15  # 새 ID 는 옛 Track 이 소실 후보인 동안 계속 IDENTITY_UNCERTAIN
