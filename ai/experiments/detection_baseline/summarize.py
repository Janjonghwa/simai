"""runs/ 의 지연·정확도 metrics.json 을 마크다운 표로 묶는다(README 결과 절에 옮겨 적는 용도).

  python ai/experiments/detection_baseline/summarize.py --tag <git short hash>
"""
from __future__ import annotations

import argparse
import json

import det_common as C


def load(run_id: str) -> dict | None:
    p = C.RUNS / run_id / "metrics.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def latency_table(d: dict) -> list[str]:
    out = ["| 크기 | 경로 | imgsz | batch | 입력 텐서 | 벽시계 p50 | p95 | 추론 p50 | 전처리 p50 | 후처리 p50 | 호출/s | VRAM torch | 장치 사용 |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in d["rows"]:
        if r["status"] != "OK":
            out.append(f"| {r['size']} | {r['mode']} | {r['imgsz']} | {r['batch']} | {r['status']} |||||||||")
            continue
        st = r["stage_ms"]
        shape = "×".join(map(str, r["input_tensor_shape"][2:]))
        out.append(f"| {r['size']} | {r['mode']} | {r['imgsz']} | {r['batch']} | {shape} | {r['wall_ms']['p50']} | {r['wall_ms']['p95']} | "
                   f"{st['inference']['p50']} | {st['preprocess']['p50']} | {st['postprocess']['p50']} | {r['calls_per_s_p50']} | "
                   f"{r['vram_peak_alloc_mb']} | {r['vram_device_used_mb']}{' (넘침 의심)' if r.get('vram_near_full') else ''} |")
    return out


def accuracy_table(d: dict) -> list[str]:
    out = ["| 크기 | 경로 | OKS AP | AP50 | AP75 | PCK@0.1 | 무릎·발목 PCK@0.1 | 관절 저신뢰율 | 무릎·발목 저신뢰율 | 사람 재현율 |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for r in d["rows"]:
        out.append(f"| {r['size']} | {r['mode']} | {r['oks_AP']} | {r['oks_AP50']} | {r['oks_AP75']} | {r['pck@0.1']} | "
                   f"{r['pck@0.1_knee_ankle']} | {r['kpt_lowconf_rate']} | {r['kpt_lowconf_rate_knee_ankle']} | {r['person_recall']} |")
    return out


def env_line(d: dict) -> str:
    e, g = d["env"], d["env"]["gpu"]
    return (f"- `{d['run_id']}`: commit `{d['git']['commit'][:7]}` (dirty={d['git']['dirty_worktree']}), {g.get('name')} "
            f"driver {g.get('driver_version')}, AC={g.get('ac_power')}, torch {e['torch']} (CUDA {e['torch_cuda']}), "
            f"ultralytics {e['ultralytics']}, 입력 {d['input'].get('coco_image_id', d['input'].get('dataset_id'))}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    a = ap.parse_args()

    lines: list[str] = []
    for backend, name in (("pt", "PyTorch FP16"), ("trt", "TensorRT FP16")):
        if d := load(f"lat-4050-{backend}-fp16-{a.tag}"):
            lines += [f"### 지연 · {name}", "", env_line(d), "", *latency_table(d), ""]
    for backend, name in (("pt", "PyTorch FP16"), ("trt", "TensorRT FP16")):
        if d := load(f"acc-4050-coco500-{backend}-{a.tag}"):
            lines += [f"### 정확도 · {name} · COCO val2017 500장 · imgsz 640", "", env_line(d), "", *accuracy_table(d), ""]
    text = "\n".join(lines)
    out = C.RUNS / f"summary_{a.tag}.md"
    out.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
