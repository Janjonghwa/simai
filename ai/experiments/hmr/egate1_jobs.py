"""EXP-02 E-GATE-1 GPU 큐 항목 (S15P31A101-87). gpu_queue Job 파일을 만든다(독립 생성기, 4차 세션 큐 -291 에는 Lead 가 끼운다).

녹화(rec-20261021-031045z-classroom-dev-test 등)에서 노트북이 `egate1_frames.py` 로 뽑은 같은 시각 카메라별 그림을 Pod 의 datasets 에 올린 뒤,
통합 이미지(`jonghwa1492/tier0:unified-…`) Pod 에서 `simai-exp02` HMR 경로로 카메라별 HMR 결과 묶음을 만든다. 세 항목은 프롬프트만 다르다:

    e1  mask     bbox + YOLO26-seg 마스크 (E-GATE-1 기본 입력, 명세 5.6)
    e2  bbox     bbox 만 (기준선 '원본': 공식 경로와 같은 프롬프트)
    e3  mask_kp  e1 + 몸통·고관절 keypoint 프롬프트 (모델 내부 호출이라 GPU 미검증 변형. 실패해도 e1·e2 는 남는다)

    python ai/experiments/hmr/egate1_jobs.py --root /srv/simai --repo /srv/simai/setup/session1/repo --py python3 \\
        --frames-dir /srv/simai/datasets/egate1-frames-rec-20261021 --calibration /srv/simai/datasets/classroom-k/intrinsics-20261021-022940z.json \\
        --calibration-local ai/experiments/recordings/intrinsics/intrinsics-20261021-022940z.json \\
        --frames-index-local <노트북 egate1_frames 출력>/index.json --session 20261027-s4 -o exp-jobs-87.json
    uv run python ai/experiments/gpu_queue/gpu_queue.py check exp-jobs-87.json   # 노트북 형식 확인

결과 폴더 <root>/runs/exp02/<run_id> 는 exp_job.py 가 끝난 결과를 덮어쓰지 않는다. LIVE 관제 중에는 돌지 않는다(on_live stop, 이 Job 은
상시 아바타 스트림이 아니라 배치 실험이다, AGENTS.md). 사람이 찍힌 입력이라 결과 묶음은 노트북으로만 가져오고 커밋하지 않는다.
노트북에서 Tier 0 world_tracks(녹화 재생, 외부 파라미터가 있는 보정 필요)와 함께 `egate1_eval.py` 로 평가한다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

JIRA = "S15P31A101-87"
# docs/data/weights.md 'yolo26x-seg' (deploy/weights.json 묶음에 없어 Pod 에 따로 올린다)
SEG_SHA256 = "sha256:92b3de0065766a17180d6219858717dc9d03cdce8a3ca9576c97fd75aabb64f3"
VARIANTS = (
    ("e1", "mask", "bbox + YOLO26-seg 마스크 (E-GATE-1 기본)"),
    ("e2", "bbox", "bbox 만 (기준선 원본)"),
    ("e3", "mask_kp", "마스크 + 몸통·고관절 keypoint 프롬프트 (GPU 미검증 변형)"),
)


def calibration_provenance(path: Path) -> dict:
    """보정 기록 파일 → input_provenance.calibration (gpu_session1.calibration_provenance 와 같은 규칙). 못 읽으면 SystemExit(지어내지 않음)."""
    data = path.read_bytes()
    doc = json.loads(data)
    if not isinstance(doc, dict) or doc.get("format") != "simai.intrinsics_estimate":
        raise SystemExit(f"{path}: simai.intrinsics_estimate 기록이 아니다")
    return {"source": "G1_AUTO", "calibration_version": f"{path.name}@sha256:{hashlib.sha256(data).hexdigest()}"}


def frames_meta(path: Path) -> tuple[str, int, int]:
    """egate1_frames index.json → (dataset_id, 시각 수, 그림 수)."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("format") != "simai.egate1_frames":
        raise SystemExit(f"{path}: simai.egate1_frames index 가 아니다")
    return doc["dataset_id"], len(doc["instants"]), sum(len(i["frames"]) for i in doc["instants"])


def build(*, root: str, repo: str, py: str, frames_dir: str, calibration: str, calibration_prov: dict, dataset_id: str,
          images: int, session: str, seg_model: str, seg_sha256: str = SEG_SHA256, variants: tuple[str, ...] = ()) -> dict:
    sfx = re.sub(r"[^A-Za-z0-9_.-]", "_", session)
    runs = f"{root}/runs/exp02"
    w = f"{root}/weights"
    hmr = "ai/experiments/hmr"
    chosen = [v for v in VARIANTS if not variants or v[0] in variants]
    unknown = sorted(set(variants) - {v[0] for v in VARIANTS})
    if unknown:
        raise SystemExit(f"모르는 항목 {unknown} (가능: {[v[0] for v in VARIANTS]})")
    # 그림당 1.1 s 로 잡는다(H100 실측 HMR 1명 약 0.3 s·5명 약 0.4 s, EXP-02 6.4 에 검출·분할·펴기·쓰기를 넉넉히 더함) + 모델 적재 120 s
    expected = int(120 + 1.1 * images)
    jobs = []
    for key, mode, title in chosen:
        run_id = f"EG1_{key}_{mode}_{sfx}"
        out = f"{runs}/{run_id}"
        weights = [f"{w}/yolo/yolo26x-pose.pt", f"{w}/sam-3d-body-dinov3/model.ckpt", f"{w}/sam-3d-body-dinov3/assets/mhr_model.pt"]
        cmd = ["simai-exp02", py, f"{hmr}/run_multiview_hmr.py", "--run-id", run_id, "--frames", frames_dir,
               "--calibration", calibration, "--prompt", mode, "--inference-type", "body"]
        if mode != "bbox":
            cmd += ["--seg-model", seg_model, "--seg-sha256", seg_sha256]
            weights.append(seg_model)
        jobs.append({
            "name": f"{key}_egate1_hmr_{mode}",
            "command": [py, "deploy/scripts/exp_job.py", "--out", out, "--", *cmd],
            "expected_s": expected,
            "timeout_s": max(1800, 3 * expected),
            "vram_mb": 0,
            "env": {"SIMAI_ROOT": root},
            "record": {
                "model_version": f"Fast-SAM-3D-Body@d72aa36 dinov3 (opt, body) + YOLO26x-pose, prompt {mode}: {title}",
                "weights": weights,
                "dataset": [frames_dir, calibration],
                "config": {"path": f"{hmr}/run_multiview_hmr.py", "overrides": {"prompt": mode, "inference_type": "body"}},
                "results": [{"kind": "REPORT", "path": f"{out}/metrics.json"},
                            {"kind": "HUMAN_MESH_OUTPUT", "path": f"{out}/hmr_frames.jsonl"}],
            },
        })
    return {
        "queue": {"name": f"egate1-{sfx}", "workdir": repo, "continue_on_failure": True, "poll_s": 1, "kill_grace_s": 10,
                  "vram_wait_s": 60, "lock": {"heartbeat_s": 15, "ttl_s": 120},
                  "live_guard": {"on_live": "stop", "poll_s": 15}},
        "record": {
            "experiment_id": "EXP-02",
            "e_gate": "E-GATE-1",
            "jira_key": JIRA,
            "owner": f"{JIRA} Owner",
            "experiment_state": "BENCHMARK",
            "input_provenance": {"frames": {"source": "FILE", "dataset_id": dataset_id}, "calibration": calibration_prov,
                                 "is_upper_bound": False},
        },
        "jobs": jobs,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default="/srv/simai", help="Pod 저장 위치($SIMAI_ROOT)")
    ap.add_argument("--repo", required=True, help="Pod 의 저장소 사본")
    ap.add_argument("--py", default="python3", help="통합 이미지의 Python")
    ap.add_argument("--frames-dir", required=True, help="Pod 에 올린 egate1_frames 출력 폴더")
    ap.add_argument("--calibration", required=True, help="Pod 의 교실 내부 파라미터 기록(simai.intrinsics_estimate)")
    ap.add_argument("--calibration-local", required=True, type=Path, help="같은 기록의 노트북 사본(출처·sha256 을 읽는다)")
    ap.add_argument("--frames-index-local", required=True, type=Path, help="노트북의 egate1_frames index.json(dataset_id·그림 수)")
    ap.add_argument("--seg-model", default=None, help="Pod 의 yolo26x-seg.pt (기본 <root>/weights/yolo/yolo26x-seg.pt)")
    ap.add_argument("--seg-sha256", default=SEG_SHA256)
    ap.add_argument("--variants", default="", help="쉼표 목록(e1,e2,e3). 비면 전부")
    ap.add_argument("--session", required=True)
    ap.add_argument("-o", "--out", type=Path, required=True)
    a = ap.parse_args(argv)
    if a.out.exists():
        raise SystemExit(f"{a.out} 가 이미 있다")
    dataset_id, _instants, images = frames_meta(a.frames_index_local)
    doc = build(root=a.root, repo=a.repo, py=a.py, frames_dir=a.frames_dir, calibration=a.calibration,
                calibration_prov=calibration_provenance(a.calibration_local), dataset_id=dataset_id, images=images,
                session=a.session, seg_model=a.seg_model or f"{a.root}/weights/yolo/yolo26x-seg.pt", seg_sha256=a.seg_sha256,
                variants=tuple(v.strip() for v in a.variants.split(",") if v.strip()))
    a.out.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{a.out}: {[j['name'] for j in doc['jobs']]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
