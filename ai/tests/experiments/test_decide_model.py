"""kanana-2-v-decide-3b-2611 + T1-v4-ALL YOLO26x(PpeDetector) 벤치마크 단위 시험 (S15P31A101-538).

노트북 CPU 에서 가짜 모델(`FakeKananaDecideModel` + `FakePpeDetector`)과 소형 합성 입력 묶음(`synthetic_only=True`)으로
질문 스키마 검증, 상향 전용 규칙(AGENTS.md 규칙 4), PPE [YOLO26x(기준) | decide-3b | 둘 결합] 3자 비교 및
NO_HELMET 확신 불변성, Tier 0 LIVE 실행 차단, 추론 출력 형식, 비교 표 및 확률 교정 곡선(Calibration Curve)을 검증한다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

AI_DIR = Path(__file__).resolve().parents[2]
if str(AI_DIR) not in sys.path:
    sys.path.insert(0, str(AI_DIR))

from experiments.decide_model.compare import (
    compare_results,
    compute_calibration_curve,
    render_markdown_report,
)
from experiments.decide_model.prepare_inputs import prepare_bundle
from experiments.decide_model.questions import (
    CAMERA_IDS,
    CURRENT_PPE_PIPELINE_SPEC,
    RISK_RULE_CRITERIA,
    T1_V4_ALL_YOLO_SPEC,
    build_map_crop_questions,
    build_ppe_questions,
    build_risk_event_questions,
    combine_yolo_and_decide_ppe,
    noul_to_wear_state,
    score_to_proposed_severity,
    validate_question_schema,
    validate_questions_bundle,
    validate_system_one_payload,
)
from experiments.decide_model.run_decide import (
    FakeKananaDecideModel,
    FakePpeDetector,
    check_tier0_not_live,
    evaluate_person_crops_with_yolo,
    run_benchmark,
    validate_system_one_response,
)


def test_question_bundles_conform_to_kanana_decide_schema() -> None:
    """(A) 위험 이벤트, (B) PPE, (C) 맵 크롭 선택 한국어 질문 묶음이 모델 규약을 만족하는지 확인."""
    risk_q = build_risk_event_questions()
    ppe_q = build_ppe_questions()
    map_q = build_map_crop_questions()

    validate_questions_bundle(risk_q)
    validate_questions_bundle(ppe_q)
    validate_questions_bundle(map_q)

    # (A) R1~R9 모두 포함 확인
    assert set(risk_q["risk_type"]["criteria"].keys()) == {f"R{i}" for i in range(1, 10)}
    assert risk_q["upgrade"]["type"] == "score"
    assert len(risk_q["upgrade"]["criteria"]) == 3
    assert risk_q["person_in_zone"]["type"] == "noul"

    # (B) helmet, vest 모두 noul 확인 및 T1-v4-ALL YOLO26x 명세 확인
    assert set(ppe_q.keys()) == {"helmet", "vest"}
    assert ppe_q["helmet"]["type"] == "noul"
    assert ppe_q["vest"]["type"] == "noul"
    assert T1_V4_ALL_YOLO_SPEC["hf_repo"] == "simai-s15p31a101/simai-ppe-helmet-vest"
    assert T1_V4_ALL_YOLO_SPEC["sha256"].startswith("e373a1048057")
    assert CURRENT_PPE_PIPELINE_SPEC["weights_spec"]["hf_file"] == "T1-v4-ALL-seed20261021-epoch08.pt"

    # (C) cleanest choice cam_A~cam_D 확인
    assert set(map_q["cleanest"]["criteria"].keys()) == set(CAMERA_IDS)


def test_upgrade_question_forbids_downward_options() -> None:
    """AGENTS.md 규칙 4: upgrade 질문에 하향·경보 해제 선택지가 들어가면 검증에서 거부해야 한다."""
    bad_upgrade = {
        "type": "score",
        "instructions": "경보를 평가하라.",
        "criteria": [
            "오경보 취소 및 경보 해제",
            "현재 경보 등급 유지",
            "CRITICAL 상향 제안",
        ],
    }
    with pytest.raises(ValueError, match="금지어"):
        validate_question_schema("upgrade", bad_upgrade)


def test_score_to_proposed_severity_enforces_upward_only() -> None:
    """AGENTS.md 규칙 4: proposed_severity 는 현재 등급보다 높을 때만 남기고 같거나 낮으면 NOT_UPWARD 로 버린다."""
    prop, reason = score_to_proposed_severity(
        "CAUTION",
        {"score": 1.55, "probabilities": {"0": 0.05, "1": 0.35, "2": 0.60}},
    )
    assert prop == "CRITICAL"
    assert reason is None

    prop_same, reason_same = score_to_proposed_severity(
        "HIGH",
        {"score": 1.0, "probabilities": {"0": 0.20, "1": 0.60, "2": 0.20}},
    )
    assert prop_same is None
    assert reason_same == "NOT_UPWARD"

    prop_crit, reason_crit = score_to_proposed_severity(
        "CRITICAL",
        {"score": 1.9, "probabilities": {"0": 0.02, "1": 0.06, "2": 0.92}},
    )
    assert prop_crit is None
    assert reason_crit == "NOT_UPWARD"


def test_combine_yolo_and_decide_ppe_never_overturns_not_worn() -> None:
    """AGENTS.md 규칙 4: YOLO26x 의 NOT_WORN(NO_HELMET) 확신을 decide 가 절대 뒤집지 못하고, UNKNOWN/저신뢰일 때만 위험 쪽으로 보조."""
    # 1) YOLO 가 NOT_WORN 이면 decide 착용 확률이 0.99 여도 절대 뒤집지 못함
    state, reason = combine_yolo_and_decide_ppe("NOT_WORN", 0.91, 0.99)
    assert state == "NOT_WORN"
    assert reason == "YOLO_NOT_WORN_LOCKED"

    # 2) YOLO 가 UNKNOWN 일 때 decide 가 미착용(<=0.40)이면 NOT_WORN 으로 위험 방향 보조
    state_u_danger, reason_u_danger = combine_yolo_and_decide_ppe("UNKNOWN", None, 0.25)
    assert state_u_danger == "NOT_WORN"
    assert reason_u_danger == "DECIDE_ASSIST_NOT_WORN_FROM_UNKNOWN"

    # 3) YOLO 가 UNKNOWN 일 때 decide 가 착용(0.85)이어도 안전 쪽(WORN)으로 임의 승격하지 않고 UNKNOWN 유지
    state_u_safe, reason_u_safe = combine_yolo_and_decide_ppe("UNKNOWN", None, 0.85)
    assert state_u_safe == "UNKNOWN"
    assert reason_u_safe == "YOLO_UNKNOWN_KEPT"

    # 4) YOLO 가 저신뢰 WORN(conf=0.55 < 0.65)이고 decide 가 미착용(0.20)이면 NOT_WORN 으로 보조
    state_low, reason_low = combine_yolo_and_decide_ppe("WORN", 0.55, 0.20)
    assert state_low == "NOT_WORN"
    assert reason_low == "DECIDE_OVERRIDE_LOW_CONF_WORN_TO_NOT_WORN"

    # 5) YOLO 가 고신뢰 WORN(conf=0.88 >= 0.65)이면 WORN 유지
    state_hi, reason_hi = combine_yolo_and_decide_ppe("WORN", 0.88, 0.20)
    assert state_hi == "WORN"
    assert reason_hi == "YOLO_WORN_KEPT"


def test_noul_to_wear_state_matches_r9_contract() -> None:
    """noul 확률(0~1)이 -519 R9 계약의 WORN / NOT_WORN / UNKNOWN 으로 정확히 매핑되는지 확인."""
    assert noul_to_wear_state(0.85) == "WORN"
    assert noul_to_wear_state(0.15) == "NOT_WORN"
    assert noul_to_wear_state(0.50) == "UNKNOWN"


def test_payload_and_response_validation_errors() -> None:
    """잘못된 이미지 개수(>10)나 잘못된 확률 합 응답을 검증기가 잡아내는지 확인."""
    q = build_ppe_questions()
    with pytest.raises(ValueError, match="최대 10장"):
        validate_system_one_payload({"task": "test"}, q, images=[f"img_{i}.jpg" for i in range(11)])

    bad_resp = {
        "model": "fake",
        "answers": {
            "helmet": {"type": "noul", "noul": 1.5},
            "vest": {"type": "noul", "noul": 0.5},
        },
        "usage": {"billing_units": 2, "input_tokens": 100, "output_tokens": 0},
    }
    with pytest.raises(ValueError, match="0~1 범위"):
        validate_system_one_response(bad_resp, q)


def test_tier0_live_marker_blocks_execution(tmp_path: Path) -> None:
    """Tier 0 LIVE 표시 파일이 running 상태이면 벤치마크 실행을 거부해야 한다."""
    marker = tmp_path / "tier0.live"
    marker.write_text(json.dumps({"phase": "running"}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="LIVE 표시 파일"):
        check_tier0_not_live(marker)

    marker.write_text(json.dumps({"phase": "stopped"}), encoding="utf-8")
    check_tier0_not_live(marker)


def test_end_to_end_prepare_run_and_compare(tmp_path: Path) -> None:
    """합성 소형 묶음 생성 → FakePpeDetector + FakeKananaDecideModel 실행 → 3자 비교 표 및 교정 곡선 시험."""
    bundle_dir = tmp_path / "bundle"
    results_path = tmp_path / "results.json"

    manifest = prepare_bundle(out_dir=bundle_dir, synthetic_only=True)
    assert manifest["counts"] == {"total": 29, "risk_event": 9, "ppe": 12, "map_crop": 8}
    assert len(RISK_RULE_CRITERIA) == 9
    assert len(manifest["session13_collection_plan"]["session13_capture_checklist"]) >= 5

    # 모든 PPE 표본에 규칙 7(is_upper_bound=True) 표기가 있는지 확인
    ppe_entries = [s for s in manifest["samples"] if s["category"] == "ppe"]
    assert all(s["is_upper_bound"] is True for s in ppe_entries)

    results_doc = run_benchmark(bundle_dir, results_path, fake=True)
    assert results_path.is_file()
    assert results_doc["sample_count"] == 29
    assert results_doc["latency_summary"]["overall"]["count"] == 29
    assert results_doc["latency_summary"]["yolo26x_ppe"]["count"] == 12

    comp = compare_results(results_doc)
    assert comp["risk_event_comparison"]["downward_proposal_violations"] == 0
    assert comp["risk_event_comparison"]["risk_type_gt_accuracy"] == 1.0

    ppe_cmp = comp["ppe_comparison"]
    assert ppe_cmp["is_upper_bound"] is True
    three_way = ppe_cmp["three_way_summary"]
    assert three_way["combined_yolo_plus_decide"]["not_worn_overturn_violations"] == 0
    assert three_way["combined_yolo_plus_decide"]["decide_assist_triggered_count"] >= 1
    assert three_way["combined_yolo_plus_decide"]["helmet_state_gt_accuracy"] >= three_way["yolo26x_baseline"]["helmet_state_gt_accuracy"]
    assert len(ppe_cmp["combined_ppe_calibration"]["bins"]) == 5
    assert comp["map_crop_comparison"]["top1_agreement"] == 1.0

    md = render_markdown_report(comp)
    assert "[YOLO26x(기준) | decide-3b | 둘 결합]" in md
    assert "PPE 확률 교정 곡선" in md
    assert "규칙 7 주의" in md


def test_evaluate_person_crops_with_yolo_uses_ppe_judge_combine_bin(tmp_path: Path) -> None:
    """FakePpeDetector 와 PpeJudge.combine_bin 연동 검증."""
    det = FakePpeDetector()
    res = evaluate_person_crops_with_yolo(
        det,
        [tmp_path / "cam_A.jpg", tmp_path / "cam_B.jpg"],
        sample_id="ppe_s1_i00000_personA",
        world_id="w-1",
    )
    assert res["helmet_state"] == "WORN"
    assert res["vest_state"] == "WORN"
    assert res["r9_finding_raised"] is False


def test_calibration_curve_math() -> None:
    """확률 교정 곡선(Brier, ECE, 구간 개수) 수치 검증."""
    cal = compute_calibration_curve([0.1, 0.2, 0.8, 0.9], [False, False, True, True], n_bins=5)
    assert cal["sample_count"] == 4
    assert cal["brier_score"] == pytest.approx(0.025, abs=1e-4)
    assert cal["ece"] == pytest.approx(0.15, abs=1e-4)
    assert sum(b["count"] for b in cal["bins"]) == 4


def test_fake_model_direct_call() -> None:
    """FakeKananaDecideModel.system_one 직접 호출 검증."""
    model = FakeKananaDecideModel()
    q = build_ppe_questions()
    resp = model.system_one(
        state={"task": "simai_ppe_check", "scene_id": "s1_both_worn", "sample_id": "ppe_s1_both_worn"},
        questions=q,
        images=["dummy_a.jpg", "dummy_b.jpg"],
        state_format="json",
    )
    validate_system_one_response(resp, q)
    assert resp["answers"]["helmet"]["noul"] > 0.7
    assert resp["answers"]["vest"]["noul"] > 0.7
