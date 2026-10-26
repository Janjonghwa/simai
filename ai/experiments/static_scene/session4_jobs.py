"""4차 GPU 세션 EXP 큐 (S15P31A101-291). gpu_queue Job 파일을 만든다.

통합 이미지 Pod 에서 LIVE 시험(별도 티켓)을 끝낸 뒤 `deploy/scripts/gpu_session1.py run --only a,v4 --exp-jobs <이 파일>` 로 돈다
(실행서: docs/design/runpod-environment.md "4차 GPU 세션"). LIVE 보호(on_live=stop)는 이 파일에도 적고 세션 실행기도 강제로 켠다.

항목(순서):
  r518·r1036·r1554·rfull  -282 MapAnything 입력 해상도 4 Run(session3_jobs.build 그대로, `simai_bootstrap.py scene` 으로 감싸
                           static 가중치 받기·Tier 0 우선 감시). docs/design/mapanything-input-resolution.md
  h0                       -283 Depth Anything 3 별도 가상환경(컨테이너 디스크 /root/da3, 통합 잠금 밖). docs/design/exp01-hybrid-depth.md
  h1                       -283 DA3NESTED-GIANT-LARGE-1.1 가중치: da3_weights.json(리비전·sha256 고정)을 simai-bootstrap 받기로.
                           해시가 다르면 받은 파일을 버리고 종료 2(Job 실패). 있던 파일이 다르면 지우지 않고 종료 2
  h2~h5                    -283 DA3 자세 조건 predict, 처리 해상도 504·1008·1512·2304
  h6                       -283 DA3 자세 없이 504(NESTED 미터 깊이, 축척 근거 후보)
  h7                       -283 ray 헤드 자세. h2~h5 결과를 보고 사람이 고른 해상도(--h7-res)를 줄 때만 넣는다
  (h8 MoGe-2 는 넣지 않는다: S15P31A101-287 에서 제외)
h2~h7 은 시작 때 같은 가중치 확인(검증 스탬프로 다시 해시하지 않음)을 다시 거쳐, h1 이 실패했거나 파일이 바뀌었으면 predict 를 돌리지 않는다.
결과 폴더는 `<root>/runs/exp01/<run_id>`(run id 에 세션 이름), exp_job.py 가 끝난 결과를 덮어쓰지 않는다(DA3 는 depth.json 기준).
fuse 는 노트북에서 한다(설계 그대로). 사람이 찍힌 입력이라 결과 묶음은 노트북으로만 가져오고 커밋하지 않는다.

    MSYS_NO_PATHCONV=1 python ai/experiments/static_scene/session4_jobs.py --repo /srv/simai/setup/session1/repo --session 20261027-s4 -o exp-jobs-s4.json
    uv run python ai/experiments/gpu_queue/gpu_queue.py check exp-jobs-s4.json      # 노트북 형식 확인
    # h7 만(사람이 h2~h5 를 보고 고른 해상도)
    python ai/experiments/static_scene/session4_jobs.py --repo … --session 20261027-s4 --only h7 --h7-res 1008 -o exp-jobs-s4-h7.json
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import session3_jobs  # noqa: E402 - 같은 폴더(-282 해상도 항목을 그대로 쓴다)

DA3_REPO = "https://github.com/ByteDance-Seed/Depth-Anything-3"
DA3_COMMIT = "3d835ec1a5802d64a8b8b15f817a1ab54809bfe4"  # main 2026-07-27(태그 없음), 설계 문서 조사 기준
# 설계 h0 그대로. --no-deps 로 공식 pyproject 의 numpy<2·opencv-python 고정을 피하고 import 에 필요한 것만 깐다
DA3_PKGS = ("torch==2.11.0", "torchvision==0.26.0", "numpy==2.3.5", "einops", "huggingface_hub", "safetensors", "omegaconf",
            "addict", "opencv-python-headless", "pillow", "imageio", "matplotlib", "scipy", "tqdm", "trimesh", "plyfile",
            "moviepy==1.0.3", "pycolmap", "evo")
DA3_WEIGHTS = "weights/da3nested-giant-large-1.1"
DA3_FILES = ("model.safetensors", "config.json")
MANIFEST = "ai/experiments/static_scene/da3_weights.json"  # 저장소 사본 기준
MANIFEST_SET = "da3"
POSE_RES = {"h2": 504, "h3": 1008, "h4": 1512, "h5": 2304}  # 자세 조건 처리 해상도(긴 변 상한)
RESOLUTIONS = ("r518", "r1036", "r1554", "rfull")
DEFAULT_MA_RUN = "CAL102_w1_map_anything_known_k_20261026-s3"
DEFAULT_PREPARED = "classroom4-prepared-20261026"
EXCLUDED = {"h8": "h8(MoGe-2 단안)은 넣지 않는다(S15P31A101-287 에서 제외)"}


def _sh(args) -> str:
    return shlex.join(str(a) for a in args)


def mapanything_jobs(root: str, repo: str, py: str, prepared: str, dataset_id: str, session: str,
                     resolutions: tuple[str, ...]) -> list[dict]:
    """-282 해상도 Run. 명령은 session3_jobs 와 같고, 실험 명령을 장면 준비 모드로 감싼다(static 가중치·Tier 0 우선)."""
    doc = session3_jobs.build(root, repo, py, prepared, dataset_id, session, resolutions)
    jobs = []
    for job in doc["jobs"]:
        key = job["record"]["config"]["overrides"]["resolution"]
        cmd = list(job["command"])
        i = cmd.index("--") + 1
        cmd[i:i] = [py, "deploy/scripts/simai_bootstrap.py", "scene"]
        rec = {**doc["record"], **job["record"], "jira_key": "S15P31A101-282", "owner": "S15P31A101-291 4차 GPU 세션 큐"}
        jobs.append({**job, "key": key, "command": cmd, "record": rec})
    return jobs


def da3_jobs(root: str, repo: str, py: str, venv: str, prepared: str, ma_run: str, session: str,
             h7_res: int | None) -> list[dict]:
    sfx = re.sub(r"[^A-Za-z0-9_.-]", "_", session)
    runs = f"{root}/runs/exp01"
    wdir = f"{root}/{DA3_WEIGHTS}"
    weights = [f"{wdir}/{f}" for f in DA3_FILES]
    da3_py = f"{venv}/bin/python"
    env = {"SIMAI_WEIGHTS_MANIFEST": f"{repo}/{MANIFEST}", "SIMAI_WEIGHTS_SET": MANIFEST_SET, "SIMAI_DA3_COMMIT": DA3_COMMIT}
    check = [py, "deploy/scripts/simai_bootstrap.py", "check"]
    base_rec = {"experiment_id": "EXP-01", "jira_key": "S15P31A101-283"}
    install = [
        "set -euo pipefail",
        f"rm -rf {shlex.quote(venv)}",  # 실패한 시도가 남긴 반쯤 된 환경을 잇지 않는다(컨테이너 디스크, 이 Job 전용 폴더)
        _sh(["uv", "venv", venv, "--python", "3.11"]),
        _sh(["uv", "pip", "install", "--python", da3_py, *DA3_PKGS]),
        _sh(["uv", "pip", "install", "--python", da3_py, "--no-deps", f"git+{DA3_REPO}@{DA3_COMMIT}"]),
        _sh([da3_py, "-c", "import torch; from depth_anything_3.api import DepthAnything3; "
                           "print('da3 import ok, torch', torch.__version__, 'cuda', torch.cuda.is_available())"]),
    ]
    jobs = [
        {"key": "h0", "name": "h0_da3_venv", "command": "\n".join(install), "expected_s": 600, "timeout_s": 1800, "vram_mb": 0,
         "record": {**base_rec, "model_version": f"Depth-Anything-3@{DA3_COMMIT[:7]} 가상환경 {venv} (통합 잠금 밖, --no-deps)"}},
        {"key": "h1", "name": "h1_da3_weights", "command": check, "env": env, "expected_s": 300, "timeout_s": 1800, "vram_mb": 0,
         "record": {**base_rec, "model_version": "DA3NESTED-GIANT-LARGE-1.1@b2359bd (CC-BY-NC-4.0) 받기·sha256 확인",
                    "weights": weights}},
    ]
    plan = [(k, f"pose_{r}", r, []) for k, r in POSE_RES.items()] + [("h6", "nopose_504", 504, ["--no-pose"])]
    if h7_res is not None:
        plan.append(("h7", f"raypose_{h7_res}", h7_res, ["--ray-pose"]))
    for key, tag, res, extra in plan:
        run_id = f"HYB283_da3_{tag}_{sfx}"
        out = f"{runs}/{run_id}"
        predict = [py, "deploy/scripts/exp_job.py", "--out", out, "--metrics", "depth.json", "--",
                   da3_py, "ai/experiments/static_scene/run_hybrid_depth.py", "predict", "--ma-run", ma_run, "--prepared", prepared,
                   "--adapter", "da3", "--model", wdir, "--process-res", str(res), *extra, "--out", out]
        mode = "자세 없이" if "--no-pose" in extra else ("ray 헤드 자세" if "--ray-pose" in extra else "자세 조건(MapAnything w1)")
        jobs.append({
            "key": key, "name": f"{key}_da3_{tag}",
            # 가중치를 다시 확인한 뒤에만(검증 스탬프, 해시 불일치·파일 없음이면 종료 2 → predict 를 돌리지 않음)
            "command": f"{_sh(check)} && {_sh(predict)}", "env": env,
            "expected_s": 300 if res >= 2304 else 180, "timeout_s": 1800 if res >= 2304 else 1200, "vram_mb": 0,
            "record": {**base_rec,
                       "model_version": f"Depth-Anything-3@{DA3_COMMIT[:7]} + DA3NESTED-GIANT-LARGE-1.1@b2359bd (CC-BY-NC-4.0), {mode}, process_res {res}",
                       "weights": weights, "dataset": [prepared, ma_run],
                       "config": {"path": "ai/experiments/static_scene/run_hybrid_depth.py",
                                  "overrides": {"adapter": "da3", "process_res": res, "pose": tag.split("_")[0]}},
                       "results": [{"kind": "REPORT", "path": f"{out}/depth.json"}, {"kind": "SCENE_OUTPUT", "path": out}]},
        })
    return jobs


def build(root: str, repo: str, py: str, venv: str, prepared: str, ma_run: str, dataset_id: str, session: str,
          resolutions: tuple[str, ...] = RESOLUTIONS, h7_res: int | None = None, only: tuple[str, ...] = ()) -> dict:
    if h7_res is not None and h7_res not in POSE_RES.values():
        raise SystemExit(f"--h7-res 는 h2~h5 해상도 중 하나다: {sorted(POSE_RES.values())} (받은 값 {h7_res})")
    bad = [k for k in only if k in EXCLUDED]
    if bad:
        raise SystemExit("; ".join(EXCLUDED[k] for k in bad))
    if "h7" in only and h7_res is None:
        raise SystemExit("h7 은 --h7-res(h2~h5 를 보고 고른 해상도)가 있어야 한다")
    jobs = mapanything_jobs(root, repo, py, prepared, dataset_id, session, resolutions) if resolutions else []
    jobs += da3_jobs(root, repo, py, venv, prepared, ma_run, session, h7_res)
    if only:
        unknown = sorted(set(only) - {j["key"] for j in jobs})
        if unknown:
            raise SystemExit(f"모르는 항목 {unknown} (가능: {[j['key'] for j in jobs]})")
        jobs = [j for j in jobs if j["key"] in only]
    sfx = re.sub(r"[^A-Za-z0-9_.-]", "_", session)
    return {
        "queue": {"name": f"s4exp-{sfx}", "workdir": repo, "continue_on_failure": True, "poll_s": 1, "kill_grace_s": 10,
                  "vram_wait_s": 60, "lock": {"heartbeat_s": 15, "ttl_s": 120},
                  # 세션 실행기(exp_job_doc)도 강제로 켜지만, gpu_queue 로 바로 돌려도 LIVE 중이면 멈추게 이 파일에도 적는다.
                  # 표시 파일 경로는 적지 않는다: 실행기가 그 세션의 저장 위치로, gpu_queue 단독이면 $SIMAI_ROOT 기준 기본값으로 채운다
                  # (여기 적으면 실행기가 덮지 않아 다른 저장 위치의 표시 파일을 보게 된다)
                  "live_guard": {"enabled": True, "on_live": "stop", "marker_max_age_s": 30}},
        "record": {
            "experiment_id": "EXP-01",
            "jira_key": "S15P31A101-283",
            "owner": "S15P31A101-291 4차 GPU 세션 큐",
            "experiment_state": "BENCHMARK",
            "input_provenance": {"frames": {"source": "FILE", "dataset_id": dataset_id}, "is_upper_bound": False},
        },
        "jobs": [{k: v for k, v in j.items() if k != "key"} for j in jobs],
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default="/srv/simai", help="Pod 저장 위치($SIMAI_ROOT, 볼륨 없음)")
    ap.add_argument("--repo", required=True, help="Pod 의 저장소 사본(gpu_session1 prepare 가 올린 위치)")
    ap.add_argument("--py", default="/opt/simai/ai/.venv/bin/python", help="통합 이미지 Python(MapAnything·exp_job·가중치 확인)")
    ap.add_argument("--da3-venv", default="/root/da3", help="h0 이 만드는 DA3 가상환경(컨테이너 디스크)")
    ap.add_argument("--prepared", default=None, help=f"Pod 의 입력 폴더(기본 <root>/datasets/{DEFAULT_PREPARED})")
    ap.add_argument("--ma-run", default=None, help=f"Pod 의 MapAnything Run(기본 <root>/runs/exp01/{DEFAULT_MA_RUN})")
    ap.add_argument("--dataset-id", default="classroom-stills-20261026")
    ap.add_argument("--session", required=True)
    ap.add_argument("--resolutions", default=",".join(RESOLUTIONS), help="-282 해상도(빈 값이면 넣지 않음)")
    ap.add_argument("--h7-res", type=int, default=None, help=f"h7 ray 자세 해상도({'·'.join(map(str, POSE_RES.values()))} 중, h2~h5 를 보고)")
    ap.add_argument("--only", default="", help="이 항목만(쉼표 목록, 예: h7)")
    ap.add_argument("-o", "--out", type=Path, required=True)
    a = ap.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "deploy" / "scripts"))
    from gpu_session1 import msys_converted  # prepare 와 같은 검사(Git Bash 가 Pod 경로를 Windows 경로로 바꿈)

    bad = [f"--{k.replace('_', '-')} {v!r}" for k, v in vars(a).items()
           if k in ("root", "repo", "py", "da3_venv", "prepared", "ma_run") and v and msys_converted(v)]
    if bad:  # 바뀐 값이 workdir·SIMAI_WEIGHTS_MANIFEST·결과 경로로 Job 파일에 들어가면 Pod 에서 못 찾는다
        raise SystemExit(f"Git Bash 경로 변환으로 바뀐 Pod 경로: {', '.join(bad)}. MSYS_NO_PATHCONV=1 을 붙여 다시 실행한다")
    if a.out.exists():
        raise SystemExit(f"{a.out} 가 이미 있다")
    split = lambda s: tuple(dict.fromkeys(x.strip() for x in s.split(",") if x.strip()))  # noqa: E731
    doc = build(a.root, a.repo, a.py, a.da3_venv, a.prepared or f"{a.root}/datasets/{DEFAULT_PREPARED}",
                a.ma_run or f"{a.root}/runs/exp01/{DEFAULT_MA_RUN}", a.dataset_id, a.session,
                split(a.resolutions), a.h7_res, split(a.only))
    a.out.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    total = sum(j["expected_s"] for j in doc["jobs"])
    print(f"{a.out}: {[j['name'] for j in doc['jobs']]} (예상 {total / 60:.0f}분)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
