"""3차 GPU 세션 MapAnything 보정 입력 큐 항목 (S15P31A101-102). gpu_queue Job 파일을 만든다.

교실 사진을 노트북에서 `simai_ai.calibration.world_frame.cli prepare` 로 펴고(펴진 K·사람 마스크) Pod 의 datasets 에 올린 뒤,
MapAnything 이미지 Pod 에서 `deploy/scripts/gpu_session1.py run --exp-jobs <이 파일>` 로 돈다(LIVE 보호가 켜진 별도 큐).
항목은 둘이다: w1 기본 가중치(map-anything, CC-BY-NC-4.0), w2 apache 판(map-anything-apache, Apache-2.0). 같은 입력을 known-K
(`run_mapanything.py --mode prepared`)로 넣고 결과를 노트북에서 `world_frame.cli fix` 로 비교한다.

    python ai/experiments/static_scene/session3_jobs.py --root /srv/simai --repo /srv/simai/setup/session1/repo \\
        --py /opt/mapanything/.venv/bin/python3 --prepared /srv/simai/datasets/classroom4-prepared \\
        --dataset-id classroom-stills-20261021 --session 20261023-s3 -o exp-jobs-102.json
    uv run python ai/experiments/gpu_queue/gpu_queue.py check exp-jobs-102.json   # 노트북 형식 확인

Run 기록은 운영 경로 검증(`VAL-01`, e_gate 없음)이다. 결과 폴더는 `<root>/runs/exp01/<run_id>` 이고 exp_job.py 가 끝난 결과를
덮어쓰지 않는다. 사람이 찍힌 입력이므로 결과 묶음은 노트북으로만 가져오고 커밋하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

MODELS = (
    ("w1", "map-anything", "CC-BY-NC-4.0"),
    ("w2", "map-anything-apache", "Apache-2.0"),
)
MA_COMMIT = "3d10cf7"


def build(root: str, repo: str, py: str, prepared: str, dataset_id: str, session: str) -> dict:
    sfx = re.sub(r"[^A-Za-z0-9_.-]", "_", session)
    runs = f"{root}/runs/exp01"
    jobs = []
    for key, model, license_ in MODELS:
        run_id = f"CAL102_{key}_{model.replace('-', '_')}_known_k_{sfx}"
        out = f"{runs}/{run_id}"
        jobs.append({
            "name": f"{key}_mapanything_known_k_{'apache' if 'apache' in model else 'default'}",
            "command": [py, "deploy/scripts/exp_job.py", "--out", out, "--",
                        py, "ai/experiments/static_scene/run_mapanything.py", "--run-id", run_id, "--mode", "prepared",
                        "--prepared", prepared, "--model", f"{root}/weights/{model}", "--runs-dir", runs],
            "expected_s": 300,
            "timeout_s": 1200,
            "vram_mb": 0,
            "record": {
                "model_version": f"map-anything@{MA_COMMIT} + {model} ({license_}), known-K prepared",
                "weights": [f"{root}/weights/{model}/model.safetensors", f"{root}/weights/{model}/config.json"],
                "dataset": [prepared],
                "config": {"path": "ai/experiments/static_scene/run_mapanything.py",
                           "overrides": {"mode": "prepared", "model": model}},
                "results": [{"kind": "REPORT", "path": f"{out}/metrics.json"},
                            {"kind": "SCENE_OUTPUT", "path": f"{out}/cameras.json"},
                            {"kind": "SCENE_OUTPUT", "path": f"{out}/raw.npz"}],
            },
        })
    return {
        "queue": {"name": f"cal102-{sfx}", "workdir": repo, "continue_on_failure": True, "poll_s": 1,
                  "kill_grace_s": 10, "vram_wait_s": 60, "lock": {"heartbeat_s": 15, "ttl_s": 120}},
        "record": {
            "experiment_id": "VAL-01",
            "jira_key": "S15P31A101-102",
            "owner": "S15P31A101-102 Owner",
            "experiment_state": "BENCHMARK",
            "input_provenance": {"frames": {"source": "FILE", "dataset_id": dataset_id}, "is_upper_bound": False},
        },
        "jobs": jobs,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default="/srv/simai", help="Pod 저장 위치($SIMAI_ROOT)")
    ap.add_argument("--repo", required=True, help="Pod 의 저장소 사본(gpu_session1 prepare 가 올린 위치)")
    ap.add_argument("--py", default="/opt/mapanything/.venv/bin/python3")
    ap.add_argument("--prepared", required=True, help="Pod 에 올린 prepare 출력 폴더")
    ap.add_argument("--dataset-id", default="classroom-stills-20261021")
    ap.add_argument("--session", required=True)
    ap.add_argument("-o", "--out", type=Path, required=True)
    a = ap.parse_args(argv)
    if a.out.exists():
        raise SystemExit(f"{a.out} 가 이미 있다")
    doc = build(a.root, a.repo, a.py, a.prepared, a.dataset_id, a.session)
    a.out.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{a.out}: {[j['name'] for j in doc['jobs']]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
