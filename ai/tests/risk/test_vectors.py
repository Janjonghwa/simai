"""공통 테스트 벡터 실행기. 저장소 벡터(tests/risk/vectors)가 배포 등록부로 모두 통과해야 한다."""

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from simai_ai.risk import (
    Channel,
    Confidence,
    NonLiveAlertError,
    Origin,
    UnknownRuleError,
    channels_for,
    default_registry,
)
from simai_ai.risk._parse import RiskInputError
from simai_ai.risk.vectors import discover, load_vector, main, parse_vector, run_paths, run_vector

VECTOR_DIR = Path(__file__).resolve().parent / "vectors"
VECTOR_FILES = discover([VECTOR_DIR])


LIFECYCLE_VECTORS = {
    "lifecycle_live_ack_resolve.json",
    "lifecycle_observation_lost.json",
    "lifecycle_stale_observed.json",
    "lifecycle_identity_revision.json",
    "lifecycle_merge.json",
    "lifecycle_planned_no_alert.json",
    "lifecycle_what_if_no_alert.json",
    "lifecycle_replay_no_alert.json",
}

# R1 시나리오(S15P31A101-71, G6-R1): 경계 진입·이탈·깜빡임, 불확실성, Z 모름, 단상 위 VOLUME_3D.
# R6·R7 시나리오(S15P31A101-73, G3): 구역 안 관측 상실 자동 해소 0건, 카메라 분리 1초 안 저하 표시, 복구 시 해소,
# 비 LIVE 출처 현장 경보 0건.
# R4 시나리오(S15P31A101-96, D-G6-02): 바닥 낙상 발생·해소, 단상 위 낙상 + UNCERTAIN 머리 근거, 급하강 직후 관측 상실,
# 발생 뒤 머리 높이 모름 중 유지, 정상 동작(빠른 앉기·바닥 앉기·단상 내려오기) 0건, 비 LIVE 경보 0건.
# R5 시나리오(S15P31A101-98, D-G6-03): 접근(발생·R1 인계·해소), 스쳐 지나감·정지, 되돌아감, 불확실 속도, 속도 상실 중 유지,
# 축마다 다른 σ, 비 LIVE 경보 0건.
# 보정 PROVISIONAL(S15P31A101-218, D-G3-03): R2·R3·R4 판정 전부에 PROVISIONAL_CALIB (음성 대조는 test_provisional_calib).
RULE_VECTORS = {
    "r2_r3_podium_provisional_calib.json",
    "r4_fall_floor_provisional_calib.json",
    "r4_fall_floor_provisional_lifecycle.json",
    "r1_boundary_entry_exit.json",
    "r1_boundary_entry_exit_what_if.json",
    "r1_boundary_entry_exit_replay.json",
    "r1_uncertain_person.json",
    "r1_volume_z_unknown.json",
    "r1_volume_podium.json",
    "r1_persistence_resume.json",
    "r1_footprint_position_or_anchor.json",
    "r2_r3_podium_live.json",
    "r2_r3_podium_replay.json",
    "r2_r3_podium_operational_profile.json",
    "r5_r2_edge_stage_approach.json",
    "r4_fall_floor.json",
    "r4_fall_floor_replay.json",
    "r4_podium_fall_uncertain_joints.json",
    "r4_lost_after_drop.json",
    "r4_head_unknown_hold.json",
    "r4_normal_postures.json",
    "r5_approach_entry.json",
    "r5_approach_entry_what_if.json",
    "r5_approach_entry_replay.json",
    "r5_approach_entry_planned.json",
    "r5_pass_by_and_stationary.json",
    "r5_turn_back.json",
    "r5_uncertain_velocity.json",
    "r5_velocity_lost_hold.json",
    "r5_anisotropic_sigma.json",
    "r6_observation_lost_in_zone.json",
    "r6_observation_lost_in_zone_what_if.json",
    "r7_camera_disconnect.json",
    "r7_sync_invalid_replay.json",
    "r7_live_without_monitoring.json",
    "r7_planned_without_monitoring.json",
}


def test_vectors_exist():
    assert {p.name for p in VECTOR_FILES} >= {
        "empty_scene.json",
        "no_rules.json",
        "replay_zone_versions.json",
    } | LIFECYCLE_VECTORS | RULE_VECTORS


def test_lifecycle_vectors_cover_every_origin():
    origins = {load_vector(VECTOR_DIR / name).origin for name in LIFECYCLE_VECTORS}
    assert origins == set(Origin)


@pytest.mark.parametrize("path", VECTOR_FILES, ids=lambda p: p.stem)
def test_repository_vector_passes(path):
    result = run_vector(load_vector(path), default_registry())
    assert result.passed, "\n".join(result.mismatches)


def test_vector_ids_are_unique():
    ids = [load_vector(p).vector_id for p in VECTOR_FILES]
    assert len(ids) == len(set(ids))


def test_default_registry_rules():
    # R1(S15P31A101-71, R6 으로 감쌈), R2·R3(S15P31A101-93), R4(S15P31A101-96), R5(S15P31A101-98), R7(S15P31A101-73),
    # R9(S15P31A101-519), R11(S15P31A101-548, 조끼) 모두 기본 꺼짐: 시드에서 빠짐. 등록부는 ID 문자열 순서라 R11 이 R1 뒤에 온다.
    # R6 은 구역 규칙을 감싸므로 따로 등록하지 않는다. 높이 규칙과 그 가장자리를 예측하는 R5 는 교실 시험 프로파일(버전 + 1000, 명세 7.4)도 등록한다.
    assert list(default_registry()) == [
        ("R1", 1),
        ("R11", 1),
        ("R2", 1),
        ("R2", 1001),
        ("R3", 1),
        ("R3", 1001),
        ("R4", 1),
        ("R5", 1),
        ("R5", 1001),
        ("R7", 1),
        ("R9", 1),
    ]


def test_rule_vectors_include_non_live_origins():
    origins = {load_vector(VECTOR_DIR / name).origin for name in RULE_VECTORS}
    assert Origin.LIVE in origins and origins - {Origin.LIVE}


# --- 실행기 자체 ---------------------------------------------------------------


@pytest.fixture
def probe_vector(world_payload, zone_json):
    """PROBE 규칙 벡터: 사람 1명 × 구역 1개 → 판정 1건."""
    return {
        "format_version": "1.0.0",
        "vector_id": "probe",
        "description": "실행기 시험용",
        "origin": "LIVE",
        "rules": [{"rule_id": "PROBE", "rule_version": 1}],
        "zones": [zone_json()],
        "steps": [
            {
                "clock_ms": 1760342400200,
                "world_state": world_payload(),
                "expect": {
                    "findings": [
                        {
                            "rule_id": "PROBE",
                            "rule_version": 1,
                            "severity": "HIGH",
                            "world_ids": ["w-0012"],
                            "zone_id": "z-04",
                            "needs_check": False,
                            "reasons": ["probe"],
                            "metrics": {"staleness_ms": 120},
                        }
                    ]
                },
            }
        ],
    }


def _write(tmp_path: Path, obj, name="v.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return path


def test_runner_passes_matching_vector(probe_vector, probe_registry):
    assert run_vector(parse_vector(probe_vector), probe_registry).passed


@pytest.mark.parametrize("origin", ["PLANNED", "REPLAY", "WHAT_IF"])
def test_runner_passes_same_findings_for_non_live(probe_vector, probe_registry, origin):
    probe_vector["origin"] = origin
    assert run_vector(parse_vector(probe_vector), probe_registry).passed


def test_metric_tolerance(probe_vector, probe_registry):
    finding = probe_vector["steps"][0]["expect"]["findings"][0]
    finding["metrics"]["staleness_ms"] = 120 + 1e-12
    assert run_vector(parse_vector(probe_vector), probe_registry).passed
    finding["metrics"]["staleness_ms"] = 120.001
    assert not run_vector(parse_vector(probe_vector), probe_registry).passed


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("severity", "CRITICAL"),
        ("world_ids", ["w-9999"]),
        ("zone_id", None),
        ("needs_check", True),
        ("reasons", ["other"]),
        ("metrics", {"staleness_ms": 120, "extra_m": 1.0}),
    ],
)
def test_runner_reports_mismatch(probe_vector, probe_registry, field, value):
    probe_vector["steps"][0]["expect"]["findings"][0][field] = value
    result = run_vector(parse_vector(probe_vector), probe_registry)
    assert not result.passed
    assert result.mismatches[0].startswith("steps[0]: findings[0]")


def test_runner_reports_missing_finding(probe_vector, probe_registry):
    probe_vector["steps"][0]["expect"]["findings"] = []
    result = run_vector(parse_vector(probe_vector), probe_registry)
    assert not result.passed
    assert "판정 1건, 기대 0건" in result.mismatches[0]


def test_multi_step_uses_fixed_clock_per_step(probe_vector, probe_registry):
    second = copy.deepcopy(probe_vector["steps"][0])
    second["clock_ms"] = 1760342400600
    second["expect"]["findings"][0]["metrics"]["staleness_ms"] = 520
    probe_vector["steps"].append(second)
    assert run_vector(parse_vector(probe_vector), probe_registry).passed


def test_zone_target_finding_in_vector(probe_registry, zone_json):
    """사람 없는 구역의 시스템 판정(R7 형태)을 벡터 기대 결과로 비교할 수 있다(독립 리뷰 F1 재현 경로)."""
    vector = json.loads((VECTOR_DIR / "empty_scene.json").read_text(encoding="utf-8"))
    vector["rules"] = [{"rule_id": "ZONE_PROBE", "rule_version": 1}]
    vector["zones"] = [zone_json(zone_id="zone-review")]
    expected = {
        "rule_id": "ZONE_PROBE",
        "rule_version": 1,
        "severity": "HIGH",
        "world_ids": [],
        "zone_id": "zone-review",
        "needs_check": False,
        "reasons": ["coverage_lost"],
        "metrics": {"observing_cameras": 1},
    }
    vector["steps"][0]["expect"]["findings"] = [expected]
    assert vector["steps"][0]["world_state"]["tracks"] == []
    assert run_vector(parse_vector(vector), probe_registry).passed

    # 기대값이 틀리면 실패한다.
    expected["world_ids"] = ["w-0012"]
    assert not run_vector(parse_vector(vector), probe_registry).passed


def test_expected_finding_needs_a_target(probe_vector):
    finding = probe_vector["steps"][0]["expect"]["findings"][0]
    finding["world_ids"], finding["zone_id"] = [], None
    with pytest.raises(RiskInputError, match="판정 대상"):
        parse_vector(probe_vector)


def test_non_finite_numbers_in_vector_json_are_rejected(tmp_path, probe_vector):
    # Python json 은 표준 밖 NaN 을 받아 주므로 파서가 막아야 한다.
    text = json.dumps(probe_vector).replace("[3.4, 5.01]", "[NaN, 5.01]", 1)
    assert "NaN" in text
    path = tmp_path / "nan.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(RiskInputError, match="유한"):
        load_vector(path)


def test_unknown_rule_is_an_error(probe_vector):
    with pytest.raises(UnknownRuleError):
        run_vector(parse_vector(probe_vector), default_registry())


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda v: v.update(format_version="2.0.0"), id="format-major"),
        pytest.param(lambda v: v.update(format_version="1"), id="format-no-minor"),
        pytest.param(
            lambda v: v["steps"][0].update(monitoring={"cameras": [], "zone_cameras": {}}), id="monitoring-before-1.2"
        ),
        pytest.param(lambda v: v.update(typo=1), id="unknown-top-key"),
        pytest.param(lambda v: v.update(steps=[]), id="no-steps"),
        pytest.param(lambda v: v.update(origin="TEST"), id="bad-origin"),
        pytest.param(lambda v: v["steps"][0].update(clock_ms=1.5), id="float-clock"),
        pytest.param(lambda v: v["steps"][0]["expect"]["findings"][0].pop("reasons"), id="finding-missing-key"),
        pytest.param(
            lambda v: v["steps"][0]["world_state"]["tracks"][0].update(support_surface_validity="UNKNOWN"), id="z0-fill"
        ),
        pytest.param(lambda v: v["steps"].append(copy.deepcopy(v["steps"][0]) | {"clock_ms": 1}), id="clock-goes-back"),
    ],
)
def test_bad_vector_is_rejected(probe_vector, mutate):
    mutate(probe_vector)
    with pytest.raises(RiskInputError):
        parse_vector(probe_vector)


def test_run_paths_reports_unreadable_vector(tmp_path, probe_vector):
    probe_vector["format_version"] = "9.0.0"
    results = run_paths([_write(tmp_path, probe_vector)])
    assert len(results) == 1 and not results[0].passed
    assert "format_version" in results[0].mismatches[0]


def test_cli_exit_codes(tmp_path, probe_vector, capsys):
    assert main([str(VECTOR_DIR)]) == 0
    assert f"{len(VECTOR_FILES)}/{len(VECTOR_FILES)} 통과" in capsys.readouterr().out

    # PROBE 는 배포 등록부에 없으므로 실패해야 한다.
    assert main([str(_write(tmp_path, probe_vector))]) == 1
    # 벡터가 하나도 없으면 통과로 치지 않는다.
    empty = tmp_path / "empty"
    empty.mkdir()
    assert main([str(empty)]) == 1


# --- 생명주기 시나리오(형식 1.1) ---------------------------------------------------------


def _lifecycle_vector(name="lifecycle_live_ack_resolve.json"):
    return json.loads((VECTOR_DIR / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        pytest.param(
            lambda v: v["steps"][0]["expect"]["transitions"][0].update(severity="CRITICAL"), "전이 실제", id="severity"
        ),
        pytest.param(
            lambda v: v["steps"][0]["expect"]["transitions"][0].update(event_sequence=2), "전이 실제", id="sequence"
        ),
        pytest.param(lambda v: v["steps"][4]["expect"].update(transitions=[]), "전이 실제", id="missing-resolve"),
        pytest.param(lambda v: v["steps"][1]["expect"].update(local_signals=[]), "현장 경보 신호", id="missing-stop"),
        pytest.param(
            lambda v: v["steps"][2]["expect"]["local_signals"].append(
                {"kind": "ALERT_CLEAR", "event": "e1", "severity": "HIGH", "needs_check": False}
            ),
            "현장 경보 신호",
            id="extra-signal",
        ),
        pytest.param(lambda v: v["steps"][1]["acks"][0].update(event="e9"), "활성 이벤트가 아니다", id="ack-unknown"),
    ],
)
def test_lifecycle_runner_reports_mismatch(mutate, message):
    vector = _lifecycle_vector()
    mutate(vector)
    result = run_vector(parse_vector(vector))
    assert not result.passed
    assert any(message in m for m in result.mismatches), result.mismatches


def test_lifecycle_runner_rejects_non_live_alert(monkeypatch):
    """채널 표가 잘못돼 비 LIVE 에 현장 경보를 고르더라도 신호 자체를 만들 수 없다(이중 장치)."""
    from simai_ai.risk import alerting, lifecycle

    def broken(severity, confidence, origin):
        return alerting.ChannelPlan(channels=frozenset({alerting.Channel.LOCAL_ALERT}), needs_check=False)

    monkeypatch.setattr(lifecycle, "channels_for", broken)
    with pytest.raises(NonLiveAlertError):
        run_vector(parse_vector(_lifecycle_vector("lifecycle_replay_no_alert.json")))


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda v: v["steps"][0]["expect"].pop("transitions"), id="transitions-required"),
        pytest.param(lambda v: v["steps"][0]["expect"].pop("local_signals"), id="signals-required"),
        pytest.param(lambda v: v["lifecycle"].update(typo=1), id="lifecycle-unknown-key"),
        pytest.param(lambda v: v["lifecycle"]["policy"].update(clear_hold_ms=-1), id="negative-hold"),
        pytest.param(lambda v: v["steps"][1]["acks"][0].update(actor="WATCH"), id="bad-actor"),
        pytest.param(lambda v: v["steps"][0]["expect"]["transitions"][0].pop("changes"), id="transition-missing-key"),
        pytest.param(lambda v: v["steps"][0]["inject_findings"][0].update(holding="yes"), id="holding-not-bool"),
        pytest.param(lambda v: v.pop("lifecycle"), id="lifecycle-fields-without-lifecycle"),
    ],
)
def test_bad_lifecycle_vector_is_rejected(mutate):
    vector = _lifecycle_vector()
    mutate(vector)
    with pytest.raises(RiskInputError):
        parse_vector(vector)


def test_holding_in_expected_finding_is_compared(probe_vector, probe_registry):
    probe_vector["steps"][0]["expect"]["findings"][0]["holding"] = True
    assert not run_vector(parse_vector(probe_vector), probe_registry).passed


def test_cli_module_entrypoint():
    result = subprocess.run(
        [sys.executable, "-m", "simai_ai.risk.vectors", str(VECTOR_DIR)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_r7_vectors_use_control_screen_notice_only():
    """R7 은 CAUTION(시스템): 관제 화면 안내만 쓰고 현장 경보·관제 화면 경보·반장 PWA 로 보내지 않는다(7.2, 7.3)."""
    for name in sorted(n for n in RULE_VECTORS if n.startswith("r7_")):
        vector = load_vector(VECTOR_DIR / name)
        for step in vector.steps:
            assert not step.expected_signals, f"{name}: R7 은 현장 경보 신호를 내지 않는다"
            for finding in step.expected:
                confidence = Confidence.LOW if finding.needs_check else Confidence.HIGH
                plan = channels_for(finding.severity, confidence, Origin.LIVE)
                assert Channel.CONTROL_SCREEN_NOTICE in plan.channels, name
                assert not plan.channels & {Channel.LOCAL_ALERT, Channel.CONTROL_SCREEN_ALERT, Channel.FOREMAN_PWA}
        assert run_vector(vector, default_registry()).passed, name


# --- 발생 지속 조건·재개·확인 필요 이유(형식 1.3) ------------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda v: v["lifecycle"]["policy"].update(resume_window_ms=1000), id="resume-key-in-1.1"),
        pytest.param(
            lambda v: v["lifecycle"]["policy"].update(persistence={"min_frames": 2, "window_frames": 3}),
            id="persistence-key-in-1.1",
        ),
        pytest.param(
            lambda v: v["steps"][0]["inject_findings"][0].update(confidence_reasons=["PREDICTED"]),
            id="confidence-reasons-in-1.1",
        ),
    ],
)
def test_v13_keys_are_rejected_below_1_3(mutate):
    vector = _lifecycle_vector()
    assert vector["format_version"].startswith("1.1")
    mutate(vector)
    with pytest.raises(RiskInputError):
        parse_vector(vector)


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda v: v["lifecycle"]["policy"].update(resume_window_ms=-1), id="negative-resume"),
        pytest.param(
            lambda v: v["lifecycle"]["policy"].update(persistence={"min_frames": 3, "window_frames": 2}),
            id="min-over-window",
        ),
        pytest.param(
            lambda v: v["lifecycle"]["policy"].update(persistence={"min_frames": 2}), id="persistence-missing-key"
        ),
        pytest.param(
            lambda v: v["steps"][0]["inject_findings"][0].update(confidence_reasons=["LOW_SCORE"]),
            id="unknown-confidence-reason",
        ),
        pytest.param(
            lambda v: v["steps"][0]["expect"]["transitions"][0].pop("confidence_reasons"),
            id="transition-needs-confidence-reasons",
        ),
    ],
)
def test_bad_v13_vector_is_rejected(mutate):
    vector = _lifecycle_vector("lifecycle_persistence_boundary.json")
    vector["steps"][0]["inject_findings"][0].setdefault("confidence_reasons", [])
    mutate(vector)
    with pytest.raises(RiskInputError):
        parse_vector(vector)


def test_v13_vectors_use_product_defaults_and_old_vectors_stay_immediate():
    """1.3 미만은 지속 조건·재개 없는 설정, 1.3 이상은 제품 기본값(3프레임 중 2, R7 즉시, 재개 10초)."""
    old = parse_vector(_lifecycle_vector())
    assert old.lifecycle.policy.persistence.immediate and old.lifecycle.policy.resume_window_ms is None
    new = parse_vector(_lifecycle_vector("lifecycle_resume_window.json"))
    policy = new.lifecycle.policy
    assert (policy.persistence.min_frames, policy.persistence.window_frames) == (2, 3)
    assert policy.persistence_for("R7").immediate and policy.resume_window_ms == 10_000


@pytest.mark.parametrize(
    ("name", "mutate"),
    [
        pytest.param(
            "lifecycle_resume_window.json",
            lambda v: v["lifecycle"]["policy"].update(resume_window_ms=None),
            id="resume-disabled-makes-new-event",
        ),
        pytest.param(
            "lifecycle_persistence_boundary.json",
            lambda v: v["lifecycle"]["policy"].update(persistence={"min_frames": 1, "window_frames": 1}),
            id="immediate-raises-spike",
        ),
        pytest.param(
            "lifecycle_confidence_reasons.json",
            lambda v: v["steps"][1]["expect"]["transitions"][0].update(confidence_reasons=["SINGLE_VIEW", "Z_UNKNOWN"]),
            id="reason-order",
        ),
    ],
)
def test_v13_vectors_detect_behavior_change(name, mutate):
    vector = _lifecycle_vector(name)
    mutate(vector)
    assert not run_vector(parse_vector(vector)).passed
