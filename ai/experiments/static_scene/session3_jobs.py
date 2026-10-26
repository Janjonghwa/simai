"""3차 GPU 세션 MapAnything 보정 입력 큐 항목 (S15P31A101-102). gpu_queue Job 파일을 만든다.

교실 사진을 노트북에서 `simai_ai.calibration.world_frame.cli prepare` 로 펴고(펴진 K·사람 마스크) Pod 의 datasets 에 올린 뒤,
MapAnything 이미지 Pod 에서 `deploy/scripts/gpu_session1.py run --exp-jobs <이 파일>` 로 돈다(LIVE 보호가 켜진 별도 큐).
항목은 둘이다: w1 기본 가중치(map-anything, CC-BY-NC-4.0), w2 apache 판(map-anything-apache, Apache-2.0). `--resolutions r518,r1036,r1554,rfull`
이면 w1 하나를 해상도만 바꿔 돌린다(S15P31A101-282, 비교는 compare_resolutions.py). 같은 입력을 known-K
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
# 입력 해상도 비교(S15P31A101-282): 키 → run_mapanything --resize-mode/--size. r518 은 공식 기본(fixed_mapping).
# 2304×1296 기준 모델 입력: r518 518×294, r1036 1036×588, r1554 1554×868, rfull 2296×1288(14 배수로 내림)
RESOLUTIONS = {
    "r518": (),
    "r1036": ("--resize-mode", "longest_side", "--size", "1036"),
    "r1554": ("--resize-mode", "longest_side", "--size", "1554"),
    "rfull": ("--resize-mode", "fixed_size", "--size", "2304x1296"),
}


def build(root: str, repo: str, py: str, prepared: str, dataset_id: str, session: str,
          resolutions: tuple[str, ...] = ()) -> dict:
    """resolutions 가 비면 기존 w1·w2(공식 기본 해상도). 주면 w1 기본 가중치 하나를 그 해상도들로(해상도 비교, -282)."""
    sfx = re.sub(r"[^A-Za-z0-9_.-]", "_", session)
    runs = f"{root}/runs/exp01"
    resolutions = tuple(dict.fromkeys(resolutions))  # 같은 키를 두 번 주면 Job·결과 폴더가 겹치므로 한 번만(순서 유지)
    unknown = [r for r in resolutions if r not in RESOLUTIONS]
    if unknown:
        raise SystemExit(f"모르는 해상도 키 {unknown} (가능: {list(RESOLUTIONS)})")
    plan = ([(key, model, license_, None) for key, model, license_ in MODELS] if not resolutions
            else [(*MODELS[0], r) for r in resolutions])
    jobs = []
    for key, model, license_, res in plan:
        tag = f"_{res}" if res else ""
        run_id = f"CAL102_{key}_{model.replace('-', '_')}_known_k{tag}_{sfx}"
        out = f"{runs}/{run_id}"
        extra = list(RESOLUTIONS[res]) if res else []
        overrides = {"mode": "prepared", "model": model, **({"resolution": res} if res else {})}
        jobs.append({
            "name": f"{key}_mapanything_known_k_{'apache' if 'apache' in model else 'default'}{tag}",
            "command": [py, "deploy/scripts/exp_job.py", "--out", out, "--",
                        py, "ai/experiments/static_scene/run_mapanything.py", "--run-id", run_id, "--mode", "prepared",
                        "--prepared", prepared, "--model", f"{root}/weights/{model}", "--runs-dir", runs, *extra],
            "expected_s": 600 if res in ("r1554", "rfull") else 300,
            "timeout_s": 2400 if res in ("r1554", "rfull") else 1200,
            "vram_mb": 0,
            "record": {
                "model_version": f"map-anything@{MA_COMMIT} + {model} ({license_}), known-K prepared{f', {res}' if res else ''}",
                "weights": [f"{root}/weights/{model}/model.safetensors", f"{root}/weights/{model}/config.json"],
                "dataset": [prepared],
                "config": {"path": "ai/experiments/static_scene/run_mapanything.py", "overrides": overrides},
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
    ap.add_argument("--resolutions", default="", help=f"쉼표 목록이면 w1 하나를 그 해상도들로(해상도 비교). 가능: {','.join(RESOLUTIONS)}")
    ap.add_argument("-o", "--out", type=Path, required=True)
    a = ap.parse_args(argv)
    if a.out.exists():
        raise SystemExit(f"{a.out} 가 이미 있다")
    res = tuple(r.strip() for r in a.resolutions.split(",") if r.strip())
    doc = build(a.root, a.repo, a.py, a.prepared, a.dataset_id, a.session, res)
    a.out.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{a.out}: {[j['name'] for j in doc['jobs']]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
