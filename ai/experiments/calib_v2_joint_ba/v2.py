"""S15P31A101-449 measurement entry point; native dependency failures are recorded.

Run: <venv python> <this file> [--solver {joint,projected}] [--from-tracks <tracks.json>].
Artifacts go to a new timestamped folder under codex-tmp/449, never production.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
OUTPUT_ROOT = Path("C:/Users/SSAFY/simai-local/codex-tmp/449")


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def git(*args):
    return subprocess.check_output(["git", *args], cwd=REPO, encoding="utf-8").strip()


def report(result):
    lines = ["# V2 공동 BA 측정", "", "판정: HOLD / 채택 금지. 독립 실측 검증 미완료.", "",
             f"솔버: {result['solver']}. 전체 수렴: {result['all_solves_converged']}. "
             f"채택 차단 사유: {', '.join(result['adoption_blockers'])}.", "",
             f"기준 카메라: {result['anchor']} ({result['anchor_reason']})", "",
             "홀드아웃은 후보 자세로 다시 DLT 삼각측량한 두 관측의 기하 일관성이다. 독립 3D/metric 정확도가 아니다.", "",
             "| 단계 | 구분 | 대상 | n(관측) | 중앙값 px | p90 px | p95 px | 초기 대비 악화 |",
             "|---|---|---|---:|---:|---:|---:|---|"]
    base = result["stages"]["initial"]["holdout"]
    from control import worsened
    for name, stage in result["stages"].items():
        for kind in ("cameras", "pairs"):
            for item, stats in stage["holdout"][kind].items():
                bad = worsened(base[kind][item], stats) if name != "initial" else False
                def fmt(key):
                    return f"{stats[key]:.3f}" if stats[key] is not None else "미정/유효하지 않음"
                lines.append(f"| {name} | {kind} | {item} | {stats['n']} | {fmt('median')} | {fmt('p90')} | {fmt('p95')} | {'악화' if bad else ''} |")
    lines += ["", "| 쌍 | 학습 트랙 | 홀드아웃 트랙 | 홀드아웃 부족(판단 제외) |",
              "|---|---:|---:|---|"]
    for pair, counts in result["input"]["pairs"].items():
        lines.append(f"| {pair} | {counts['train']} | {counts['holdout']} | {counts['holdout_insufficient']} |")
    lines += ["", "| 단계 | 초기 비용 | 최종 비용 | 초기→최종 픽셀 Huber | 초기→최종 이차 prior | nfev/njev | optimality | 종료 |",
              "|---|---:|---:|---|---|---|---:|---|"]
    for name, stage in result["stages"].items():
        if "initial_cost" not in stage:
            continue
        a, b = stage["initial_cost"], stage["final_cost"]
        lines.append(f"| {name} | {a['total']:.6f} | {b['total']:.6f} | "
                     f"{a['pixel_huber']:.6f}→{b['pixel_huber']:.6f} | "
                     f"{a['quadratic_priors']:.6f}→{b['quadratic_priors']:.6f} | "
                     f"{stage['nfev']}/{stage['njev']} | {stage['optimality']:.6g} | {stage['message']} |")
    lines += ["", "| 단계 | 변수 | 수렴 | nfev/njev | 실제 residual 호출 | 솔버 s | 진단 s | 단계 전체 s | 10s 목표 | 채택 차단 |",
              "|---|---:|---|---|---:|---:|---:|---:|---|---|"]
    for name, stage in result["stages"].items():
        if "success" not in stage:
            continue
        lines.append(f"| {name} | {stage['variables']} | {stage['success']} | {stage['nfev']}/{stage['njev']} | "
                     f"{stage['residual_calls']} | {stage['seconds']:.3f} | {stage['observability_seconds']:.3f} | "
                     f"{stage['stage_seconds']:.3f} | {stage['within_time_target']} | "
                     f"{'채택 금지: ' + ', '.join(stage['adoption_blockers']) if stage['adoption_forbidden'] else '독립 검증 필요'} |")
    lines += ["", "nfev는 SciPy 집계이며 수치 Jacobian 평가를 포함한 실제 residual 호출 수와 다르다. "
              "단계 전체 시간은 풀이·홀드아웃·관측성 진단을 포함한다. 전체 Run에는 민감도 풀이 4회도 포함된다.",
              "", "| 단계 | 카메라 | 높이 σ m | 높이 잔차 σ | 높이 비용(이차) | 최적화 가능 |",
              "|---|---|---:|---:|---:|---|"]
    for name, stage in result["stages"].items():
        if "height_prior" not in stage:
            continue
        prior = stage["height_prior"]
        for cid, row in prior["cameras"].items():
            lines.append(f"| {name} | {cid} | {prior['sigma_m']} | {row['residual_sigma']:.4f} | "
                         f"{row['cost']:.6f} | {row['optimized']} |")
    lines += ["", "높이 prior는 Huber 없이 이차 손실로 적용되는 soft constraint다. "
              "Stage A와 anchor는 높이를 움직일 수 없으며 ±1cm를 강제하는 bound가 아니다."]
    lines += ["", "| 단계 | 점 정밀화 수렴 | 최대 점 gradient | 허용값 | 반복/한도 |",
              "|---|---|---:|---:|---|"]
    for name, stage in result["stages"].items():
        if "point_projection" in stage:
            p = stage["point_projection"]
            lines.append(f"| {name} | {p['converged']} | {p['max_point_gradient']:.6g} | "
                         f"{p['gradient_tolerance']:.6g} | {p['iterations']}/{p['max_iterations']} |")
    lines += ["", "| 단계 | 카메라 | 회전 ° | 중심 이동 cm | 높이 잔차 cm | 솔버 시간 s |",
              "|---|---|---:|---:|---:|---:|"]
    for name, stage in result["stages"].items():
        for cid, pose in stage["pose_change"].items():
            lines.append(f"| {name} | {cid} | {pose['rotation_deg']:.4f} | {pose['center_cm']:.3f} | {pose['height_residual_cm']:.3f} | {stage['seconds']:.3f} |")
    lines += ["", "| 단계 | 특징점만 최소 특이값 | 상대 최소 특이값 | 약한 모드 수 | prior 포함 최소 특이값 |",
              "|---|---:|---:|---:|---:|"]
    for name, stage in result["stages"].items():
        if "observability" not in stage:
            continue
        raw = stage["observability"]["data_only_schur"]
        augmented = stage["observability"]["with_quadratic_priors_schur"]
        lines.append(f"| {name} | {raw['smallest']:.6g} | {raw['smallest_relative']:.6g} | {raw['weak_modes']} | {augmented['smallest']:.6g} |")
    lines += ["", "| 변화 조건 | 중심 차이 최댓값 cm | baseline 축척비 중앙값 | baseline 비율 변화 최댓값 | 수렴 | nfev/njev | 솔버 s | 채택 차단 |",
              "|---|---:|---:|---:|---|---|---:|---|"]
    for row in result["sensitivity"]:
        lines.append(f"| {row['label']} | {row['max_center_difference_cm']:.3f} | "
                     f"{row['median_baseline_scale_ratio']:.6f} | {max(abs(v) for v in row['baseline_relative_change']):.6f} | "
                     f"{row['success']} | {row['nfev']}/{row['njev']} | {row['seconds']:.3f} | "
                     f"{'채택 금지' if row['adoption_forbidden'] else '진단 전용'} |")
    lines += ["", f"이동 불안정: {result['translation_unstable']}. 데이터 Jacobian 약한 모드: {result['data_jacobian_weak']}.",
              f"총 소요 시간: {result['total_seconds']:.3f} s "
              f"({'트랙 재사용·솔버·진단' if result['input'].get('feature_extraction_reused') else '배경·매칭·솔버·진단'} 포함).", "",
              "카메라별 홀드아웃 수는 위 표의 n이며 작은 표본으로 꼬리 오차를 안정적으로 판단하기 어렵다. "
              "C–D 홀드아웃 부족 표시는 학습 제외가 아니며 해당 쌍의 검증 판단을 보류한다.",
              "이동 결과 채택 금지. 확인된 바닥 점 0개, 독립 정물 치수 검증 없음. Stage C는 완화 영향 측정용이며 자동 채택 불가.",
              "채택 재검토 조건: 모든 단계 수렴/앞쪽 깊이, 쌍별 악화 검토, 초기값·가중 안정성, 약한 특이값 해석 및 독립 실측 검증.",
              "명세 §4.4 목표(중앙값 2px·p95 5px, 높이 ±5cm 등)를 검토하되 본 두 시점 잔차만으로 CALIBRATED를 주장하지 않는다."]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=HERE / "config.json")
    parser.add_argument("--from-tracks", type=Path, help="Reuse saved undistorted observations/splits; no torch, av or lightglue")
    parser.add_argument("--solver", choices=("joint", "projected"), default="projected",
                        help="joint: camera+point BA; projected (default): camera-only variable projection")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    config["solver"] = args.solver
    output = Path(config["output_root"]).resolve()
    if output != OUTPUT_ROOT.resolve():
        raise ValueError("Output must stay in the assigned codex-tmp/449 directory")
    if config["device"] not in ("cpu", "cuda"):
        raise ValueError("Device must be 'cpu' or 'cuda'")
    started = time.time_ns() // 1_000_000
    run_id = "val01-calib-v2-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    out = output / run_id
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / "config.json", config)
    gpu_meta = {"model": "", "driver": "", "cuda": "", "provider": "LOCAL", "shared_with_tier0": False}
    if config["device"] == "cuda" and args.from_tracks is None:
        try:
            smi = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"],
                encoding="utf-8").strip().splitlines()[0].split(",")
            gpu_meta["model"], gpu_meta["driver"] = smi[0].strip(), smi[1].strip()
        except Exception:
            pass
    record = {
        "schema_version": "2.2.0", "run_id": run_id, "experiment_id": "VAL-01",
        "jira_key": "S15P31A101-449", "owner": "미지정 (Lead 확인 필요)",
        "git_commit": git("rev-parse", "HEAD"), "branch": git("branch", "--show-current"),
        "dirty_worktree": bool(git("status", "--porcelain")), "env_hash": "",
        "model_version": "ALIKED+LightGlue (package metadata in environment.json; weights not loaded if ABORTED)",
        "config": {"path": Path(os.path.relpath(args.config.resolve(), REPO)).as_posix(), "sha256": sha256(args.config)},
        "input_provenance": {"frames": {"source": "FILE", "dataset_id": "session11/rec-calwalk",
                                         "camera_ids": [f"cam_{c}" for c in "ABCD"]},
                             "calibration": {"source": "MODEL_ESTIMATE", "calibration_version":
                                 json.loads(Path(config["calibration"]).read_text(encoding="utf-8"))["calibration_version"]},
                             "is_upper_bound": False},
        "gpu": gpu_meta,
        "started_at": started, "status": "ABORTED", "experiment_state": "HOLD", "metrics": {}, "results": [],
        "notes": "VAL-01: offline calibration validation, no e_gate/adoption claim. Human owner needs Lead assignment. "
                 f"env_hash empty: no Docker digest. Device={config['device']}. dirty_worktree runs cannot be review/adoption evidence. "
                 "MODEL_ESTIMATE: existing estimated rotfix calibration reused as input. No GT; not product performance.",
    }
    status_code = 1
    try:
        manifest = []
        if args.from_tracks is None:
            for cid in "ABCD":
                path = Path(config["recording"]) / f"cam_{cid}" / "epoch_0000.h264"
                manifest.append({"path": str(path), "bytes": path.stat().st_size, "sha256": sha256(path)})
        else:
            path = args.from_tracks.resolve()
            manifest.append({"path": str(path), "bytes": path.stat().st_size, "sha256": sha256(path)})
            record["notes"] += f" Solver-only replay from {path}; no feature extraction or decoding."
            record["model_version"] = "Saved ALIKED+LightGlue tracks; no model loaded in this replay"
            # Saved pixels are undistorted. Refuse another calibration when original hashes exist.
            source_manifest = path.with_name("input_manifest.json")
            if source_manifest.exists():
                original = json.loads(source_manifest.read_text(encoding="utf-8"))
                calibration = Path(config["calibration"])
                entries = [e for e in original if Path(e["path"]).name == calibration.name]
                if not entries or entries[0]["sha256"] != sha256(calibration):
                    raise ValueError("Replay calibration does not match original track input manifest")
        for key in ("calibration", "v0_results"):
            path = Path(config[key])
            manifest.append({"path": str(path), "bytes": path.stat().st_size, "sha256": sha256(path)})
        write_json(out / "input_manifest.json", manifest)
        versions = {}
        for name in ("numpy", "scipy", "opencv-python", "opencv-python-headless", "av", "torch", "lightglue", "simai-ai"):
            try:
                versions[name] = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                versions[name] = None
        write_json(out / "environment.json", {"python": sys.version, "executable": sys.executable,
                   "packages": versions, "source_sha256": {p.name: sha256(p) for p in sorted(HERE.glob("*.py"))}})
        # Import NumPy explicitly first so sandbox DLL failures precede any model/download activity.
        import numpy  # noqa: F401
        if config["device"] == "cuda" and args.from_tracks is None:
            import torch
            record["gpu"]["cuda"] = str(torch.version.cuda or "")
        from numeric import run
        result = run(config, out, from_tracks=args.from_tracks, solver=args.solver)
        (out / "RESULTS.md").write_text(report(result), encoding="utf-8")
        record["status"] = "SUCCEEDED"
        record["metrics"] = {"total_seconds": result["total_seconds"],
                             "solver": result["solver"], "all_solves_converged": result["all_solves_converged"],
                             "adoption_forbidden": result["adoption_forbidden"],
                             "translation_unstable": result["translation_unstable"],
                             "translation_adopted": False, "data_jacobian_weak": result["data_jacobian_weak"]}
        status_code = 0
    except Exception as exc:
        record["status"] = "ABORTED" if isinstance(exc, (ImportError, OSError)) else "FAILED"
        record["failure_reason"] = f"{type(exc).__name__}: {exc}"
        (out / "failure.log").write_text(traceback.format_exc(), encoding="utf-8")
        (out / "RESULTS.md").write_text(
            "# V2 공동 BA 측정\n\n판정: HOLD / 미측정.\n\n" + record["failure_reason"] +
            "\n\n수치·자세 변화·관측성·소요 시간은 측정하지 못했다. failure.log 및 저장소 RESULTS.md의 재실행 절차 참조.\n",
            encoding="utf-8")
        detail = next((line.strip() for line in reversed(str(exc).splitlines()) if line.strip()), str(exc))
        print(f"{type(exc).__name__}: {detail}", file=sys.stderr)
    finally:
        record["finished_at"] = time.time_ns() // 1_000_000
        for path in sorted(out.iterdir()):
            if path.is_file() and path.name != "run_record.json":
                record["results"].append({"kind": "REPORT" if path.suffix == ".md" else "OTHER",
                                           "uri": path.as_uri(), "sha256": sha256(path)})
        write_json(out / "run_record.json", record)
        print(f"{record['status']}: {out}")
    return status_code


if __name__ == "__main__":
    raise SystemExit(main())
