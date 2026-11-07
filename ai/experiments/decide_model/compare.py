"""kanana-2-v-decide-3b-2611 및 T1-v4-ALL YOLO26x 비교·교정 곡선 산출 (S15P31A101-538).

`run_decide.py` 가 만든 `results.json` 을 읽어 다음 세 가지를 정량 비교하고
JSON(`comparison.json`)과 한국어 마크다운 표(`comparison.md`)로 출력한다:
1. (A) 위험 이벤트(`risk_event`): Cosmos3-Nano VLM 실측/기준선 및 정답 대비
   - `risk_type`(R1~R9) 일치율, `proposed_severity` 상향 의견 일치율(하향 0건 검증), `person_in_zone` 정확도, 지연시간 비교
2. (B) PPE(`ppe`): **[T1-v4-ALL YOLO26x(기준, `simai_ai.ppe.detector.PpeDetector`) | decide-3b | 둘 결합]** 3자 비교
   - 같은 사람 크롭에서 `YOLO26x` 상태 vs `decide-3b` noul 확률 vs `둘 결합`(YOLO 가 UNKNOWN/저신뢰일 때만 위험 방향 `NOT_WORN` 으로 보조, `NO_HELMET` 확신은 절대 뒤집지 못함 — 규칙 4)
   - Brier 점수, ECE, 5구간 확률 교정 곡선(Calibration Curve), 지연·GPU 메모리 나란히 비교
   - 규칙 7 경고: `rec-20261028-023200z-ppe-training` 은 `T1-v4-ALL` 세 세션 전체 재학습에 포함된 세션이므로 파이프라인 점검용 상한(`is_upper_bound=True`)으로만 기록하고 제품 일반화 성능으로 쓰지 않음
3. (C) 맵 크롭 선택(`map_crop`): `vdt/cameras.json` 입사각·선명도(Laplacian) 기준선 대비
   - `cleanest`(cam_A~cam_D) Top-1/Top-2 일치율 및 카메라별 선택 확률 비교 표
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

FORMAT = "simai.decide_benchmark_comparison"
FORMAT_VERSION = "0.2.0"
DEFAULT_RESULTS_PATH = Path(__file__).resolve().parent / "runs" / "results_fake.json"
DEFAULT_OUT_JSON = Path(__file__).resolve().parent / "runs" / "comparison_fake.json"
DEFAULT_OUT_MD = Path(__file__).resolve().parent / "runs" / "comparison_fake.md"


def compute_calibration_curve(
    probs: Sequence[float],
    labels: Sequence[bool],
    *,
    n_bins: int = 5,
) -> dict[str, Any]:
    """이진 확률 예측(`noul`)에 대한 Brier Score, ECE, 구간별 확률 교정 곡선(Calibration Curve)을 계산한다."""
    if len(probs) != len(labels):
        raise ValueError("probs 와 labels 길이가 같아야 한다.")
    if n_bins < 2:
        raise ValueError("n_bins 는 최소 2 이상이어야 한다.")

    n = len(probs)
    if n == 0:
        return {"sample_count": 0, "brier_score": 0.0, "ece": 0.0, "bins": []}

    brier = sum((float(p) - (1.0 if y else 0.0)) ** 2 for p, y in zip(probs, labels)) / n
    bins: list[dict[str, Any]] = []
    ece = 0.0

    for b_idx in range(n_bins):
        lo = b_idx / n_bins
        hi = (b_idx + 1) / n_bins
        in_bin = [
            (float(p), 1.0 if y else 0.0)
            for p, y in zip(probs, labels)
            if (lo <= float(p) < hi) or (b_idx == n_bins - 1 and lo <= float(p) <= hi)
        ]
        cnt = len(in_bin)
        if cnt > 0:
            mean_pred = sum(p for p, _ in in_bin) / cnt
            emp_pos = sum(y for _, y in in_bin) / cnt
            gap = abs(mean_pred - emp_pos)
            ece += (cnt / n) * gap
        else:
            mean_pred = None
            emp_pos = None
            gap = None

        bins.append(
            {
                "bin_index": b_idx,
                "range": f"[{lo:.1f}, {hi:.1f}{']' if b_idx == n_bins - 1 else ')'}",
                "lower": round(lo, 2),
                "upper": round(hi, 2),
                "count": cnt,
                "mean_predicted_prob": round(mean_pred, 4) if mean_pred is not None else None,
                "empirical_positive_rate": round(emp_pos, 4) if emp_pos is not None else None,
                "abs_calibration_gap": round(gap, 4) if gap is not None else None,
            }
        )

    return {
        "sample_count": n,
        "brier_score": round(brier, 4),
        "ece": round(ece, 4),
        "bins": bins,
    }


def _compare_risk_events(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    rule_gt_match = 0
    rule_vlm_match = 0
    prop_gt_match = 0
    prop_vlm_match = 0
    zone_gt_match = 0
    zone_vlm_match = 0
    downward_violations = 0
    zone_probs: list[float] = []
    zone_labels: list[bool] = []
    vlm_latencies: list[float] = []
    decide_latencies: list[float] = []

    for s in samples:
        gt = s["ground_truth"]
        vlm = s.get("baseline_vlm") or {}
        interp = s["interpreted"]
        cur_sev = str(s["state"]["current_severity"])

        pred_rule = interp["predicted_rule_id"]
        pred_prop = interp["proposed_severity"]
        pred_zone = bool(interp["person_in_zone_pred"])
        zone_prob = float(interp["person_in_zone_prob"])

        if pred_rule == gt["risk_type"]:
            rule_gt_match += 1
        if pred_rule == vlm.get("predicted_rule_id"):
            rule_vlm_match += 1

        if pred_prop == gt.get("expected_proposed_severity"):
            prop_gt_match += 1
        if pred_prop == vlm.get("kept_proposed_severity"):
            prop_vlm_match += 1

        if pred_zone == bool(gt["person_in_zone"]):
            zone_gt_match += 1
        if pred_zone == bool(vlm.get("person_in_zone")):
            zone_vlm_match += 1

        sev_rank = {"INFO": 0, "CAUTION": 1, "HIGH": 2, "CRITICAL": 3}
        if pred_prop is not None and sev_rank[pred_prop] <= sev_rank[cur_sev]:
            downward_violations += 1

        zone_probs.append(zone_prob)
        zone_labels.append(bool(gt["person_in_zone"]))
        decide_latencies.append(float(s["latency_ms"]))
        if vlm.get("total_ms") is not None:
            vlm_latencies.append(float(vlm["total_ms"]))

        rows.append(
            {
                "sample_id": s["sample_id"],
                "current_severity": cur_sev,
                "gt_rule": gt["risk_type"],
                "vlm_rule": vlm.get("predicted_rule_id"),
                "decide_rule": pred_rule,
                "decide_rule_prob": round(float(interp["predicted_rule_prob"]), 4),
                "gt_proposed": gt.get("expected_proposed_severity"),
                "vlm_proposed": vlm.get("kept_proposed_severity"),
                "decide_proposed": pred_prop,
                "decide_upgrade_score": round(float(interp["upgrade_score"]), 3),
                "gt_in_zone": bool(gt["person_in_zone"]),
                "decide_in_zone_prob": round(zone_prob, 4),
                "vlm_total_ms": vlm.get("total_ms"),
                "decide_latency_ms": float(s["latency_ms"]),
            }
        )

    n = max(len(samples), 1)
    return {
        "sample_count": len(samples),
        "risk_type_gt_accuracy": round(rule_gt_match / n, 4),
        "risk_type_vlm_agreement": round(rule_vlm_match / n, 4),
        "proposed_severity_gt_accuracy": round(prop_gt_match / n, 4),
        "proposed_severity_vlm_agreement": round(prop_vlm_match / n, 4),
        "person_in_zone_gt_accuracy": round(zone_gt_match / n, 4),
        "person_in_zone_vlm_agreement": round(zone_vlm_match / n, 4),
        "downward_proposal_violations": downward_violations,
        "person_in_zone_calibration": compute_calibration_curve(zone_probs, zone_labels, n_bins=5),
        "cosmos_vlm_mean_ms": round(sum(vlm_latencies) / max(len(vlm_latencies), 1), 2),
        "decide_mean_ms": round(sum(decide_latencies) / max(len(decide_latencies), 1), 2),
        "rows": rows,
    }


def _compare_ppe(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """(B) PPE 3자 비교: [YOLO26x(기준, PpeDetector) | decide-3b | 둘 결합]."""
    rows: list[dict[str, Any]] = []

    yolo_helmet_gt_match = 0
    decide_helmet_gt_match = 0
    combined_helmet_gt_match = 0

    yolo_vest_gt_match = 0
    decide_vest_gt_match = 0
    combined_vest_gt_match = 0

    decide_vs_yolo_helmet_agree = 0
    combined_vs_yolo_helmet_agree = 0
    not_worn_overturn_violations = 0
    decide_assist_triggered_count = 0

    helmet_probs: list[float] = []
    helmet_labels: list[bool] = []
    vest_probs: list[float] = []
    vest_labels: list[bool] = []

    yolo_latencies: list[float] = []
    decide_latencies: list[float] = []

    for s in samples:
        gt = s["ground_truth"]
        r9 = s.get("baseline_ppe_r9") or {}
        yolo = s.get("yolo26x_ppe") or r9
        interp = s["interpreted"]

        gt_h_state = str(gt["helmet_wear_state"])
        gt_v_state = str(gt["vest_wear_state"])

        yolo_h_state = str(yolo.get("helmet_state", "UNKNOWN"))
        yolo_h_conf = yolo.get("helmet_confidence")
        yolo_v_state = str(yolo.get("vest_state", "UNKNOWN"))
        yolo_v_conf = yolo.get("vest_confidence")

        h_prob = float(interp["helmet_prob"])
        v_prob = float(interp["vest_prob"])
        decide_h_state = str(interp["helmet_wear_state"])
        decide_v_state = str(interp["vest_wear_state"])

        comb_h_state = str(interp.get("combined_helmet_state", yolo_h_state))
        comb_h_reason = str(interp.get("combined_helmet_reason", ""))
        comb_v_state = str(interp.get("combined_vest_state", yolo_v_state))

        if yolo_h_state == gt_h_state:
            yolo_helmet_gt_match += 1
        if decide_h_state == gt_h_state:
            decide_helmet_gt_match += 1
        if comb_h_state == gt_h_state:
            combined_helmet_gt_match += 1

        if yolo_v_state == gt_v_state:
            yolo_vest_gt_match += 1
        if decide_v_state == gt_v_state:
            decide_vest_gt_match += 1
        if comb_v_state == gt_v_state:
            combined_vest_gt_match += 1

        if decide_h_state == yolo_h_state:
            decide_vs_yolo_helmet_agree += 1
        if comb_h_state == yolo_h_state:
            combined_vs_yolo_helmet_agree += 1

        # 규칙 4 불변식 검증: YOLO 가 NOT_WORN 인데 결합 결과가 NOT_WORN 이 아니면 위반
        if yolo_h_state == "NOT_WORN" and comb_h_state != "NOT_WORN":
            not_worn_overturn_violations += 1
        if yolo_v_state == "NOT_WORN" and comb_v_state != "NOT_WORN":
            not_worn_overturn_violations += 1

        if comb_h_reason.startswith("DECIDE_"):
            decide_assist_triggered_count += 1

        helmet_probs.append(h_prob)
        helmet_labels.append(bool(gt["helmet"]))
        vest_probs.append(v_prob)
        vest_labels.append(bool(gt["vest"]))

        if yolo.get("latency_ms") is not None:
            yolo_latencies.append(float(yolo["latency_ms"]))
        decide_latencies.append(float(s["latency_ms"]))

        rows.append(
            {
                "sample_id": s["sample_id"],
                "is_upper_bound": bool(s.get("is_upper_bound", True)),
                "gt_helmet_state": gt_h_state,
                "yolo_helmet_state": yolo_h_state,
                "yolo_helmet_conf": yolo_h_conf,
                "decide_helmet_prob": round(h_prob, 4),
                "decide_helmet_state": decide_h_state,
                "combined_helmet_state": comb_h_state,
                "combined_helmet_reason": comb_h_reason,
                "gt_vest_state": gt_v_state,
                "yolo_vest_state": yolo_v_state,
                "yolo_vest_conf": yolo_v_conf,
                "decide_vest_prob": round(v_prob, 4),
                "decide_vest_state": decide_v_state,
                "combined_vest_state": comb_v_state,
                "yolo_latency_ms": yolo.get("latency_ms"),
                "decide_latency_ms": float(s["latency_ms"]),
            }
        )

    n = max(len(samples), 1)
    combined_probs = [*helmet_probs, *vest_probs]
    combined_labels = [*helmet_labels, *vest_labels]

    return {
        "sample_count": len(samples),
        "is_upper_bound": True,
        "split_warning": (
            "노트북의 session4/egate1-frames-ppe-20261028(rec-20261028-023200z-ppe-training)은 "
            "T1-v4-ALL YOLO26x(T1-v4-ALL-seed20261021-epoch08.pt) 세 세션 전체 재학습에 포함된 분할이다. "
            "따라서 아래 수치는 파이프라인 점검용 상한(is_upper_bound=True)이며 제품 성능 지표로 쓰지 않는다(AGENTS.md 규칙 7)."
        ),
        "three_way_summary": {
            "yolo26x_baseline": {
                "helmet_state_gt_accuracy": round(yolo_helmet_gt_match / n, 4),
                "vest_state_gt_accuracy": round(yolo_vest_gt_match / n, 4),
                "mean_latency_ms": round(sum(yolo_latencies) / max(len(yolo_latencies), 1), 2),
            },
            "decide_3b": {
                "helmet_state_gt_accuracy": round(decide_helmet_gt_match / n, 4),
                "vest_state_gt_accuracy": round(decide_vest_gt_match / n, 4),
                "agreement_with_yolo_helmet": round(decide_vs_yolo_helmet_agree / n, 4),
                "mean_latency_ms": round(sum(decide_latencies) / max(len(decide_latencies), 1), 2),
            },
            "combined_yolo_plus_decide": {
                "helmet_state_gt_accuracy": round(combined_helmet_gt_match / n, 4),
                "vest_state_gt_accuracy": round(combined_vest_gt_match / n, 4),
                "agreement_with_yolo_helmet": round(combined_vs_yolo_helmet_agree / n, 4),
                "decide_assist_triggered_count": decide_assist_triggered_count,
                "not_worn_overturn_violations": not_worn_overturn_violations,
            },
        },
        "helmet_gt_accuracy": round(decide_helmet_gt_match / n, 4),
        "vest_gt_accuracy": round(decide_vest_gt_match / n, 4),
        "helmet_r9_state_agreement": round(decide_vs_yolo_helmet_agree / n, 4),
        "helmet_calibration": compute_calibration_curve(helmet_probs, helmet_labels, n_bins=5),
        "vest_calibration": compute_calibration_curve(vest_probs, vest_labels, n_bins=5),
        "combined_ppe_calibration": compute_calibration_curve(combined_probs, combined_labels, n_bins=5),
        "rows": rows,
    }


def _compare_map_crop(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    top1_match = 0
    top2_match = 0

    for s in samples:
        gt = s["ground_truth"]
        interp = s["interpreted"]
        probs = s["answers"]["cleanest"]["probabilities"]
        sel = str(interp["selected_camera"])
        gt_best = str(gt["cleanest"])
        gt_top2 = list(gt.get("ranking", [gt_best]))[:2]

        if sel == gt_best:
            top1_match += 1
        if sel in gt_top2:
            top2_match += 1

        rows.append(
            {
                "sample_id": s["sample_id"],
                "region_label": s["state"].get("region_label", ""),
                "gt_cleanest": gt_best,
                "gt_top2": gt_top2,
                "decide_cleanest": sel,
                "decide_prob": round(float(interp["selected_prob"]), 4),
                "decide_confidence": round(float(interp["confidence"]), 4),
                "probabilities": {k: round(float(v), 4) for k, v in probs.items()},
                "top1_matched": bool(sel == gt_best),
                "top2_matched": bool(sel in gt_top2),
            }
        )

    n = max(len(samples), 1)
    return {
        "sample_count": len(samples),
        "top1_agreement": round(top1_match / n, 4),
        "top2_agreement": round(top2_match / n, 4),
        "rows": rows,
    }


def compare_results(results_doc: Mapping[str, Any]) -> dict[str, Any]:
    """`results.json` 문서를 받아 3개 과업 비교 지표와 교정 곡선을 계산한다."""
    samples = results_doc.get("samples", [])
    risk_samples = [s for s in samples if s["category"] == "risk_event"]
    ppe_samples = [s for s in samples if s["category"] == "ppe"]
    map_samples = [s for s in samples if s["category"] == "map_crop"]

    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "jira_key": "S15P31A101-538",
        "target_model": results_doc.get("target_model"),
        "is_fake_run": bool(results_doc.get("is_fake_run", False)),
        "gpu": results_doc.get("gpu", {}),
        "yolo26x_ppe_summary": results_doc.get("yolo26x_ppe_summary", {}),
        "latency_summary": results_doc.get("latency_summary", {}),
        "risk_event_comparison": _compare_risk_events(risk_samples),
        "ppe_comparison": _compare_ppe(ppe_samples),
        "map_crop_comparison": _compare_map_crop(map_samples),
    }


def render_markdown_report(comp: Mapping[str, Any]) -> str:
    """비교 결과 사전을 한국어 마크다운 보고서 표로 렌더링한다."""
    re_cmp = comp["risk_event_comparison"]
    ppe_cmp = comp["ppe_comparison"]
    three_way = ppe_cmp["three_way_summary"]
    map_cmp = comp["map_crop_comparison"]
    lat = comp.get("latency_summary", {}).get("overall", {})
    yolo_lat = comp.get("latency_summary", {}).get("yolo26x_ppe", {})
    gpu = comp.get("gpu", {})
    yolo_gpu = comp.get("yolo26x_ppe_summary", {})

    lines: list[str] = [
        "# kanana-2-v-decide-3b-2611 벤치마크 비교 보고서 (S15P31A101-538)",
        "",
        f"- **Decide 모델**: `{comp.get('target_model')}` (`is_fake_run={comp.get('is_fake_run')}`)",
        f"- **PPE 기준 모델**: `{yolo_gpu.get('model_version', 'T1-v4-ALL-seed20261021-e08-e373a1048057')}` (`simai_ai.ppe.detector.PpeDetector`)",
        f"- **디바이스 / 정밀도**: `{gpu.get('device_name', 'N/A')}` (`{gpu.get('device')}`, `{gpu.get('dtype')}`)",
        "",
        "## 0. 모델별 지연시간 및 GPU 메모리 나란히 비교",
        "",
        "| 모델 | 역할 | 가중치 VRAM (MB) | 피크 VRAM (MB) | 평균 지연 (ms) | p50 (ms) | p95 (ms) | max (ms) |",
        "|---|---|---|---|---|---|---|---|",
        f"| **T1-v4-ALL YOLO26x** (`PpeDetector`) | PPE 기준 검출 (4클래스, FP32 640) | {yolo_gpu.get('weight_allocated_mb', 0.0)} | {yolo_gpu.get('peak_allocated_mb', 0.0)} | {yolo_lat.get('mean_ms', 0.0)} | {yolo_lat.get('p50_ms', 0.0)} | {yolo_lat.get('p95_ms', 0.0)} | {yolo_lat.get('max_ms', 0.0)} |",
        f"| **kanana-2-v-decide-3b-2611** | 결정형 VLM (`system_one`, 3.67B) | {gpu.get('weight_allocated_mb', 0.0)} | {gpu.get('peak_allocated_mb', 0.0)} | {lat.get('mean_ms', 0.0)} | {lat.get('p50_ms', 0.0)} | {lat.get('p95_ms', 0.0)} | {lat.get('max_ms', 0.0)} |",
        "",
        "## 1. (A) 위험 이벤트(`risk_event`) — Cosmos3-Nano VLM vs Decide 모델",
        "",
        "| 지표 | 값 | 비고 |",
        "|---|---|---|",
        f"| 표본 수 | {re_cmp['sample_count']} | R1~R9 대표 이벤트 |",
        f"| `risk_type` 정답 일치율 | {re_cmp['risk_type_gt_accuracy'] * 100:.1f}% | 규칙 엔진 R1~R9 기준 |",
        f"| `risk_type` Cosmos3-Nano 일치율 | {re_cmp['risk_type_vlm_agreement'] * 100:.1f}% | |",
        f"| `proposed_severity` 상향 의견 일치율 (vs VLM) | {re_cmp['proposed_severity_vlm_agreement'] * 100:.1f}% | `NOT_UPWARD` 강제 적용 |",
        f"| 하향 제안 위반 건수 | {re_cmp['downward_proposal_violations']}건 | AGENTS.md 규칙 4 (반드시 0건) |",
        f"| `person_in_zone` 정확도 | {re_cmp['person_in_zone_gt_accuracy'] * 100:.1f}% | Brier={re_cmp['person_in_zone_calibration']['brier_score']} |",
        f"| 평균 지연시간 비교 | Cosmos3 `{re_cmp['cosmos_vlm_mean_ms']} ms` vs Decide `{re_cmp['decide_mean_ms']} ms` | 단일 포워드 패스(`output_tokens=0`) |",
        "",
        "| 표본 ID | 현재 등급 | 정답 규칙 | VLM 규칙 | Decide 규칙(확률) | VLM 상향 | Decide 상향(점수) | 구역 내 사람 확률 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in re_cmp["rows"]:
        lines.append(
            f"| `{r['sample_id']}` | {r['current_severity']} | {r['gt_rule']} | {r['vlm_rule']} | "
            f"{r['decide_rule']} ({r['decide_rule_prob']:.2f}) | {r['vlm_proposed'] or '유지(null)'} | "
            f"{r['decide_proposed'] or '유지(null)'} ({r['decide_upgrade_score']:.2f}) | {r['decide_in_zone_prob']:.2f} |"
        )

    lines.extend(
        [
            "",
            "## 2. (B) PPE(`ppe`) — `[YOLO26x(기준) | decide-3b | 둘 결합]` 3자 비교",
            "",
            f"> **규칙 7 주의(`is_upper_bound=True`)**: {ppe_cmp['split_warning']}",
            "",
            "| 비교 항목 | **YOLO26x (기준, `PpeDetector`)** | **decide-3b (`noul`)** | **둘 결합 (`YOLO + decide 보조`)** | 비고 |",
            "|---|---|---|---|---|",
            f"| 안전모(`helmet`) 상태 일치율 (vs 장면 시각표) | {three_way['yolo26x_baseline']['helmet_state_gt_accuracy'] * 100:.1f}% | {three_way['decide_3b']['helmet_state_gt_accuracy'] * 100:.1f}% | **{three_way['combined_yolo_plus_decide']['helmet_state_gt_accuracy'] * 100:.1f}%** | 전환·가림 구간에서 YOLO UNKNOWN 을 decide 가 위험 쪽 보조 |",
            f"| 안전조끼(`vest`) 상태 일치율 (vs 장면 시각표) | {three_way['yolo26x_baseline']['vest_state_gt_accuracy'] * 100:.1f}% | {three_way['decide_3b']['vest_state_gt_accuracy'] * 100:.1f}% | **{three_way['combined_yolo_plus_decide']['vest_state_gt_accuracy'] * 100:.1f}%** | |",
            f"| YOLO26x 안전모 판정과의 일치율 | 100.0% | {three_way['decide_3b']['agreement_with_yolo_helmet'] * 100:.1f}% | {three_way['combined_yolo_plus_decide']['agreement_with_yolo_helmet'] * 100:.1f}% | |",
            f"| `NO_HELMET` 확신 뒤집힘 위반 건수 | 0건 | - | **{three_way['combined_yolo_plus_decide']['not_worn_overturn_violations']}건** | 규칙 4: YOLO `NOT_WORN` 은 절대 뒤집지 못함 |",
            f"| Decide 위험 방향 보조 발동 건수 | - | - | {three_way['combined_yolo_plus_decide']['decide_assist_triggered_count']}건 | YOLO UNKNOWN/저신뢰 → `NOT_WORN` 보조 |",
            f"| 평균 추론 지연 (사람 1명 4카메라 크롭) | {three_way['yolo26x_baseline']['mean_latency_ms']} ms | {three_way['decide_3b']['mean_latency_ms']} ms | 조건부(UNKNOWN/저신뢰 시에만 호출) | |",
            "",
            "### PPE 확률 교정 곡선 (Calibration Curve — `decide-3b` 합산 24문항)",
            "",
            f"- **안전모 Brier / ECE**: `{ppe_cmp['helmet_calibration']['brier_score']:.4f}` / `{ppe_cmp['helmet_calibration']['ece']:.4f}`",
            f"- **안전조끼 Brier / ECE**: `{ppe_cmp['vest_calibration']['brier_score']:.4f}` / `{ppe_cmp['vest_calibration']['ece']:.4f}`",
            f"- **합산 Brier / ECE**: `{ppe_cmp['combined_ppe_calibration']['brier_score']:.4f}` / `{ppe_cmp['combined_ppe_calibration']['ece']:.4f}`",
            "",
            "| 확률 구간 | 표본 수 | 평균 예측 확률 | 실제 착용 비율 | 절대 교정 오차(`|gap|`) |",
            "|---|---|---|---|---|",
        ]
    )
    for b in ppe_cmp["combined_ppe_calibration"]["bins"]:
        mp = f"{b['mean_predicted_prob']:.3f}" if b["mean_predicted_prob"] is not None else "-"
        ep = f"{b['empirical_positive_rate']:.3f}" if b["empirical_positive_rate"] is not None else "-"
        gp = f"{b['abs_calibration_gap']:.3f}" if b["abs_calibration_gap"] is not None else "-"
        lines.append(f"| `{b['range']}` | {b['count']} | {mp} | {ep} | {gp} |")

    lines.extend(
        [
            "",
            "| 표본 ID | 시각표(헬멧/조끼) | **YOLO26x**(헬멧/조끼, conf) | **decide-3b**(확률, 상태) | **둘 결합**(헬멧/조끼, 사유) |",
            "|---|---|---|---|---|",
        ]
    )
    for r in ppe_cmp["rows"]:
        yc = f"{r['yolo_helmet_conf']:.2f}" if r["yolo_helmet_conf"] is not None else "-"
        lines.append(
            f"| `{r['sample_id']}` | {r['gt_helmet_state']}/{r['gt_vest_state']} | "
            f"{r['yolo_helmet_state']}/{r['yolo_vest_state']} ({yc}) | "
            f"{r['decide_helmet_prob']:.2f}/{r['decide_vest_prob']:.2f} ({r['decide_helmet_state']}/{r['decide_vest_state']}) | "
            f"**{r['combined_helmet_state']}/{r['combined_vest_state']}** (`{r['combined_helmet_reason']}`) |"
        )

    lines.extend(
        [
            "",
            "## 3. (C) 맵 크롭 선택(`map_crop`) — 기하·선명도 기준선 vs Decide 모델 (`choice`)",
            "",
            f"- **Top-1 일치율**: `{map_cmp['top1_agreement'] * 100:.1f}%` ({map_cmp['sample_count']}개 3D 영역)",
            f"- **Top-2 포함율**: `{map_cmp['top2_agreement'] * 100:.1f}%`",
            "",
            "| 영역 ID | 설명 | 기준선 최적(Top-2) | Decide 선택(확률, 마진) | cam_A | cam_B | cam_C | cam_D |",
            "|---|---|---|---|---|---|---|---|",
        ]
    )
    for r in map_cmp["rows"]:
        p = r["probabilities"]
        lines.append(
            f"| `{r['sample_id']}` | {r['region_label']} | `{r['gt_cleanest']}` ({','.join(r['gt_top2'])}) | "
            f"`{r['decide_cleanest']}` ({r['decide_prob']:.2f}, conf={r['decide_confidence']:.2f}) | "
            f"{p.get('cam_A', 0.0):.2f} | {p.get('cam_B', 0.0):.2f} | {p.get('cam_C', 0.0):.2f} | {p.get('cam_D', 0.0):.2f} |"
        )

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="kanana-2-v-decide-3b-2611 + YOLO26x 결과 비교 및 교정 곡선 산출")
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS_PATH, help="run_decide.py 결과 JSON 경로")
    parser.add_argument("--out-json", type=Path, default=DEFAULT_OUT_JSON, help="비교 요약 JSON 저장 경로")
    parser.add_argument("--out-md", type=Path, default=DEFAULT_OUT_MD, help="비교 마크다운 표 저장 경로")
    args = parser.parse_args(argv)

    results_doc = json.loads(args.results.read_text(encoding="utf-8"))
    comp = compare_results(results_doc)
    md = render_markdown_report(comp)

    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(comp, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.out_md.parent.mkdir(parents=True, exist_ok=True)
    args.out_md.write_text(md, encoding="utf-8")

    print(
        json.dumps(
            {
                "out_json": str(args.out_json),
                "out_md": str(args.out_md),
                "risk_type_gt_accuracy": comp["risk_event_comparison"]["risk_type_gt_accuracy"],
                "ppe_three_way": comp["ppe_comparison"]["three_way_summary"],
                "ppe_brier": comp["ppe_comparison"]["combined_ppe_calibration"]["brier_score"],
                "map_crop_top1_agreement": comp["map_crop_comparison"]["top1_agreement"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
