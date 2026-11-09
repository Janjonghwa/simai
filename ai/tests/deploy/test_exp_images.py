"""실험 이미지(HMR exp02·MapAnything) 연결 (S15P31A101-207) 반례 시험. GPU·네트워크·Docker 를 쓰지 않는다.

반례: 실험 이미지 ENV(SIMAI_WEIGHTS_SET)가 Tier 0 명령의 가중치 묶음을 바꾸지 않음, 해시를 모르는 항목이 있으면 앞 항목을 받기 전에 멈춤,
묶음 이름 오타·빈 묶음은 아무것도 받지 않고 시작하는 대신 코드 2, run 뒤 명령 없음은 64, 실제 목록의 실험 항목은 HF revision 고정·해시 근거,
GPU 세대 호환(torch 휠 커널: 같은 주 버전의 낮은 부 버전 SASS·낮은 PTX 만, 모르는 값은 통과 아님), 세션 큐가 지금 이미지 항목만 넣고 실험 이미지는
LIVE 보호를 켬, 모르는 이미지 이름은 아무 항목도 돌리지 않음, MoGe 가 확인된 로컬 파일을 쓰되 없으면 원래 이름을 둠.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
PY = Path(sys.executable).as_posix()
FAKE_SMI = (REPO / "ai" / "tests" / "experiments" / "gpu_queue" / "fake_nvidia_smi.py").as_posix()


@pytest.fixture
def tmp_path(tmp_path):
    text = str(tmp_path)
    return Path(text[4:]) if text.startswith("\\\\?\\") else tmp_path


def _manifest(bs, tmp_path: Path, monkeypatch, entries: list[dict]) -> dict:
    """file:// 원본 가중치 목록과 가짜 nvidia-smi. 받은 원본 이름을 기록한다."""
    blobs = {}
    for e in entries:
        data = f"weights-{e['name']}".encode() * 100
        src = tmp_path / f"origin-{e['name']}.bin"
        src.write_bytes(data)
        e.setdefault("source", {"url": src.as_uri()})
        if "sha256" not in e:
            e["sha256"] = hashlib.sha256(data).hexdigest()
        e.setdefault("path", f"weights/{e['name']}.bin")
        blobs[e["name"]] = data
    manifest = tmp_path / "weights.json"
    manifest.write_text(json.dumps({"manifest_version": 1, "weights": entries}), encoding="utf-8")
    for k in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "SIMAI_WEIGHTS_SET", "SIMAI_TIER0_WEIGHTS", "SIMAI_TIER0_WEIGHTS_SHA256",
              "FAKE_SMI_FAIL", "FAKE_SMI_CC", "FAKE_SMI_TOTAL_MB"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SIMAI_ROOT", str(tmp_path / "simai"))
    monkeypatch.setenv("SIMAI_NVIDIA_SMI", f"{PY} {FAKE_SMI}")
    monkeypatch.setenv("SIMAI_WEIGHTS_MANIFEST", str(manifest))
    monkeypatch.setenv("SIMAI_MIN_FREE_GB", "0")
    fetched: list[str] = []
    real = bs.urlopen
    monkeypatch.setattr(bs, "urlopen", lambda req: fetched.append(req.full_url.rsplit("origin-", 1)[-1]) or real(req))
    calls: list = []
    monkeypatch.setattr(bs, "execvp", lambda argv: calls.append((list(argv), dict(os.environ))))
    return {"calls": calls, "fetched": fetched, "root": tmp_path / "simai"}


# ---------------------------------------------------------------- simai-bootstrap 가중치 묶음


def test_set_env_picks_image_set_but_tier0_command_always_uses_tier0(bs, tmp_path, monkeypatch):
    st = _manifest(bs, tmp_path, monkeypatch, [{"name": "yolo", "sets": ["tier0", "exp02"]},
                                               {"name": "sam", "sets": ["exp02"]}, {"name": "ma", "sets": ["static"]}])
    monkeypatch.setenv("SIMAI_WEIGHTS_SET", "exp02")
    assert bs.main(["run", "python", "x.py"]) == 0
    assert sorted(st["fetched"]) == ["sam.bin", "yolo.bin"]  # static 항목은 받지 않는다
    assert st["calls"][0][0] == ["python", "x.py"]
    # 실험 이미지 ENV 가 남아 있어도 tier0 명령은 tier0 묶음만 준비한다(실험 가중치 수 GB 를 Tier 0 기동에 끼우지 않는다)
    st["fetched"].clear()
    (st["root"] / "weights" / "yolo.bin").unlink()
    assert bs.main(["tier0"]) == 0
    assert st["fetched"] == ["yolo.bin"] and st["calls"][-1][0] == ["simai-tier0"]
    assert json.loads(sorted((st["root"] / "runs" / "bootstrap").glob("*.json"))[-1].read_text(encoding="utf-8"))["set"] == "tier0"


def test_unknown_hash_stops_before_any_download(bs, tmp_path, monkeypatch):
    # 앞 항목(수 GB)을 받고 나서 뒤 항목의 해시가 없어 실패하면 안 된다: 아무것도 받지 않고 코드 2
    st = _manifest(bs, tmp_path, monkeypatch, [{"name": "big", "sets": ["exp02"]}, {"name": "mhr", "sets": ["exp02"], "sha256": ""}])
    monkeypatch.setenv("SIMAI_WEIGHTS_SET", "exp02")
    assert bs.main(["check"]) == bs.EXIT_STORAGE
    assert st["fetched"] == [] and st["calls"] == []
    rec = json.loads(next((st["root"] / "runs" / "bootstrap").glob("*.json")).read_text(encoding="utf-8"))
    assert rec["exit"] == 2 and "mhr: 목록에 sha256 이 없어" in rec["error"]


@pytest.mark.parametrize("value, why", [("exp2", "항목이 없다"), ("EXP02", "이름"), ("../x", "이름")])
def test_unknown_or_empty_set_refuses_instead_of_starting_with_nothing(bs, tmp_path, monkeypatch, value, why):
    st = _manifest(bs, tmp_path, monkeypatch, [{"name": "sam", "sets": ["exp02"]}])
    monkeypatch.setenv("SIMAI_WEIGHTS_SET", value)
    assert bs.main(["run", "true"]) == bs.EXIT_STORAGE
    assert st["fetched"] == [] and st["calls"] == []


def test_run_without_command_is_usage_error_and_prepares_nothing(bs, tmp_path, monkeypatch):
    st = _manifest(bs, tmp_path, monkeypatch, [{"name": "sam", "sets": ["exp02"]}])
    monkeypatch.setenv("SIMAI_WEIGHTS_SET", "exp02")
    assert bs.main(["run"]) == bs.EXIT_USAGE == 64
    assert st["fetched"] == [] and st["calls"] == [] and not (st["root"] / "runs").exists()


def test_run_child_gets_no_token(bs, tmp_path, monkeypatch):
    st = _manifest(bs, tmp_path, monkeypatch, [{"name": "sam", "sets": ["exp02"]}])
    monkeypatch.setenv("SIMAI_WEIGHTS_SET", "exp02")
    monkeypatch.setenv("HF_TOKEN", "hf_" + "Z" * 30)
    assert bs.main(["run", "simai-exp02", "python", "-V"]) == 0
    argv, env = st["calls"][0]
    assert argv == ["simai-exp02", "python", "-V"] and "HF_TOKEN" not in env and env["SIMAI_ROOT"] == str(st["root"])


def test_real_manifest_experiment_entries_are_pinned_and_hashes_match_records():
    doc = json.loads((REPO / "deploy" / "weights.json").read_text(encoding="utf-8"))
    assert "placeholders" not in doc
    by = {w["name"]: w for w in doc["weights"]}
    assert set(by["yolo26x-pose"]["sets"]) == {"tier0", "exp02"}
    assert set(by["yolo26x-seg"]["sets"]) == {"avatar", "exp02"}
    exp = [w for w in doc["weights"] if set(w["sets"]) & {"exp02", "static"}]
    assert {w["name"] for w in exp} >= {
        "yolo26x-pose",
        "yolo26x-seg",
        "sam-3d-body-dinov3",
        "sam-3d-body-dinov3-mhr",
        "moge-2-vits-normal",
        "map-anything-apache-model",
        "map-anything-config",
        "hy-worldmirror-2.0-model",
        "hy-worldmirror-2.0-config",
        "big-lama",
    }
    for w in exp:
        src = w["source"]
        assert set(src) in ({"hf"}, {"url"}), w["name"]
        if "hf" in src:
            hf = src["hf"]
            assert re.fullmatch(r"[0-9a-f]{40}", hf["revision"]), w["name"]  # main 이 아니라 커밋 고정
            assert w["sha256"] == "" or re.fullmatch(r"[0-9a-f]{64}", w["sha256"]), w["name"]
        else:
            url = src["url"]
            assert url.startswith("https://") and "/releases/download/v" in url, w["name"]  # 릴리스 버전 태그 고정
            assert re.fullmatch(r"[0-9a-f]{64}", w["sha256"]), w["name"]
        assert w.get("hash_from") and w.get("license"), w["name"]
        assert not Path(w["path"]).is_absolute() and ".." not in Path(w["path"]).parts
    # docs/data/weights.md 기록과 같은 값
    assert by["yolo26x-pose"]["sha256"].startswith("08ed9e01d22a")
    assert by["yolo26x-seg"]["sha256"].startswith("92b3de006576")
    # HF API LFS oid(승인 계정, 2026-10-21, docs/data/weights.md)
    assert by["sam-3d-body-dinov3-mhr"]["sha256"].startswith("352e271a6c42")
    # E-GATE-0 기록과 같은 값
    assert by["sam-3d-body-dinov3"]["sha256"].startswith("b5a2f9d305dd")
    assert by["map-anything-apache-model"]["sha256"].startswith("fa06c0fdccef")
    assert by["map-anything-model"]["sha256"].startswith("981f060c6466")
    assert by["hy-worldmirror-2.0-model"]["sha256"].startswith("9fff06539d3d")
    assert by["hy-worldmirror-2.0-config"]["sha256"].startswith("5e40150577ef")
    assert by["big-lama"]["sha256"].startswith("7ba7aa7ac37a")


# ---------------------------------------------------------------- GPU 세대 호환


@pytest.mark.parametrize("cap, arch, want", [
    ((8, 9), ["sm_80", "sm_86"], "sm_86"),          # L4·4090: 8.6 SASS 가 8.9 에서 돈다
    ((9, 0), ["sm_80", "sm_86", "sm_89"], None),     # H100 에 9.x 커널·PTX 없음 → 실패
    ((10, 3), ["sm_90", "sm_100"], "sm_100"),        # B300: 10.0 SASS
    ((12, 1), ["sm_120"], "sm_120"),                 # GB10
    ((12, 0), ["sm_100"], None),                     # 주 버전이 다르면 SASS 호환 없음
    ((10, 0), ["sm_90", "compute_90"], "compute_90"),  # 낮은 PTX 는 JIT
    ((8, 6), ["sm_89", "compute_90"], None),         # 높은 부 버전 SASS·높은 PTX 는 안 됨
    ((8, 0), [], None), ((8, 0), ["garbage"], None),  # 모르면 통과 아님
])
def test_arch_compatibility(xc, cap, arch, want):
    assert xc.arch_ok(cap, arch) == want


@pytest.mark.parametrize("n_kp, has_cuda, ok", [(70, False, True), (70, True, False), (69, False, False)])
def test_exp02_detectron2_check_is_lazyconfig_not_cuda_extension(xc, tmp_path, monkeypatch, n_kp, has_cuda, ok):
    # S15P31A101-238: 우리 경로가 쓰는 Detectron2 는 LazyConfig 뿐. CUDA 확장이 든 옛 이미지는 g0 에서 실패해야 한다(리뷰 M2)
    torch = types.ModuleType("torch")
    torch.__version__ = "2.7.1+cu128"
    torch.version = types.SimpleNamespace(cuda="12.8")
    torch.float16, torch.bfloat16 = "f16", "bf16"
    torch.cuda = types.SimpleNamespace(is_available=lambda: True, get_device_capability=lambda i: (9, 0),
                                       get_device_name=lambda i: "NVIDIA H100 80GB HBM3", get_arch_list=lambda: ["sm_90"])
    loaded = []
    d2 = types.ModuleType("detectron2")
    d2._C = types.SimpleNamespace(has_cuda=lambda: has_cuda)
    cfg = types.ModuleType("detectron2.config")
    cfg.LazyConfig = types.SimpleNamespace(load=lambda p: loaded.append(p) or types.SimpleNamespace(
        pose_info={"keypoint_info": dict.fromkeys(range(n_kp))}))
    for name, mod in (("torch", torch), ("detectron2", d2), ("detectron2.config", cfg)):
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.setenv("SIMAI_FS3DB_REPO", str(tmp_path / "fs"))
    out = xc.run_checks("exp02")
    names = [ch["name"] for ch in out["checks"]]
    assert "detectron2_arch" not in names and "detectron2_kernel" not in names
    ch = next(ch for ch in out["checks"] if ch["name"] == "detectron2_lazyconfig")
    assert ch["ok"] is ok and Path(loaded[0]) == tmp_path / "fs" / "sam_3d_body" / "metadata" / "mhr70.py"
    if ok:
        assert ch["value"] == {"keypoints": 70, "extension_has_cuda": False}
    elif has_cuda:
        assert "CUDA 확장" in ch["error"]
    # 점검 하나라도 실패하면 종료 1 (여기서는 가짜 torch 라 matmul·렌더도 실패하므로 detectron2 만으로 가르지 않는다)
    assert xc.main(["exp02"]) == 1


def test_check_without_cuda_fails_not_passes(xc, monkeypatch):
    fake = types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=lambda: False))
    monkeypatch.setitem(sys.modules, "torch", fake)
    out = xc.run_checks("exp02")
    assert out["checks"][0]["name"] == "cuda" and out["checks"][0]["ok"] is False and len(out["checks"]) == 1


# ---------------------------------------------------------------- 세션 큐: 이미지별 항목


@pytest.mark.parametrize("image, want", [
    ("tier0", ["a_pod_check", "b_tier0_engine", "t_test_tools", "c_tier0_live", "d_tier0_gpu_priority"]),
    ("exp02", ["a_pod_check", "g0_exp02_image_check", "g1_exp02_h6_det", "g2_exp02_h0_smoke", "g3_exp02_h1_official",
               "g4_exp02_h7_calib_k"]),
    ("mapanything", ["a_pod_check", "m0_mapanything_image_check", "m1_mapanything_c1", "m2_mapanything_c2"]),
    # 통합 이미지는 Tier 0 층이 같아 tier0 항목도 돈다(S15P31A101-246). v4 는 3차의 사람 항목 v1~v3 를 대신한 통합 시험(-291), e25 는 빠른 경로 종단 확인(-25)
    ("unified", ["a_pod_check", "b_tier0_engine", "t_test_tools", "c_tier0_live", "d_tier0_gpu_priority", "u1_unified_image_check",
                 "u2_unified_hmr_calib_k", "v4_vlm_int", "e25_fast_path", "u3_unified_mapanything_c1", "u4_unified_mapanything_c2"]),
])
def test_queue_takes_only_this_images_items_and_exp_images_keep_live_guard(gs, monkeypatch, image, want):
    monkeypatch.setenv("SIMAI_IMAGE_DIGEST", "sha256:" + "b" * 64)
    lay = gs.Layout("pod", None, "20261021", "/opt/x/.venv/bin/python", image)
    doc, held = gs.build_jobs(lay, {"commit": "0" * 40, "branch": "dev", "dirty_worktree": False})
    assert [j["name"] for j in doc["jobs"]] == want
    guard = doc["queue"]["live_guard"]
    if image in ("tier0", "unified"):  # Tier 0 를 스스로 띄우고 죽이는 검증 큐
        assert guard == {"enabled": False}
    else:
        assert guard["enabled"] is True and guard["on_live"] == "stop"
        assert guard["marker_files"] == [str(lay.simai / "state" / "tier0.live")]
        assert lay.queue_name.endswith(f"-{image}")  # Tier 0 세션 상태와 섞이지 않는다
    assert doc["record"]["env"]["lock_files"] == [gs.LOCK_FILES[image]]
    assert all(j["timeout_s"] >= j["expected_s"] for j in doc["jobs"])
    # 다른 이미지 항목은 '다른 이미지' 로 표에 남는다(성공으로 꾸미지 않는다)
    other = [h for h in held if h["command"] is not None and not h.get("excluded")]
    assert other and all(gs._held_why(h).startswith("다른 이미지") for h in other)
    gq = gs.load_gpu_queue()
    path = Path(gs.tempfile.mkdtemp()) / "jobs.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    gq.load_job_file(path)
    queue = gq.Queue(doc, path, path.parent / "state", None, gs.REPO / "contracts", persist=False)
    assert queue.dry_run_errors() == []


def test_exp_items_do_not_overwrite_egate0_runs(gs):
    lay = gs.Layout("pod", None, "s2", "/py", "exp02")
    rids = [it["command"][i + 1] for it in gs.items(lay) for i, c in enumerate(it["command"] or []) if c == "--run-id"]
    egate0 = {"H0_smoke", "H1_official_opt", "H6_det_yolo26x", "C1_room4_bf16", "C2_room4_apache_bf16"}
    assert rids and not (set(rids) & egate0) and len(rids) == len(set(rids))


def test_unknown_image_kind_runs_nothing(gs, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SIMAI_IMAGE_KIND", "exp03")
    ran = []
    monkeypatch.setattr(gs, "cmd_run", lambda *a: ran.append(a) or 0)
    assert gs.main(["run", "--mode", "dryrun", "--root", str(tmp_path)]) == gs.EXIT_BAD_JOBS
    assert ran == [] and "exp03" in capsys.readouterr().err


# ---------------------------------------------------------------- HMR 스크립트: 확인된 MoGe 파일


def test_moge_uses_verified_local_file_and_keeps_repo_id_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMAI_ROOT", str(tmp_path))
    sys.path.insert(0, str(REPO / "ai" / "experiments" / "hmr"))
    try:
        sys.modules.pop("fs3db_common", None)
        import fs3db_common as c
    finally:
        sys.path.pop(0)
    fe = types.ModuleType("tools.build_fov_estimator")
    fe.MOGE_MODELS = {"s": "Ruicheng/moge-2-vits-normal", "l": "Ruicheng/moge-2-vitl-normal"}
    monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))
    monkeypatch.setitem(sys.modules, "tools.build_fov_estimator", fe)
    local = tmp_path / "weights" / "moge" / "moge-2-vits-normal" / "model.pt"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"x")
    c.use_local_moge()
    assert fe.MOGE_MODELS["s"] == str(local)
    assert fe.MOGE_MODELS["l"] == "Ruicheng/moge-2-vitl-normal"  # 없으면 원래 이름(볼륨 venv 환경)


# ---------------------------------------------------------------- 실험 Job 의 Run 기록 연결 (리뷰 M1·M2·R1·R2)


def _full_record(gs, doc: dict, job: dict, run_dir: Path, tmp_path: Path):
    """gpu_queue 기록기로 실제 Run 기록을 만든다(dry_run_errors 와 달리 weights·dataset·results 를 빼지 않는다)."""
    gq = gs.load_gpu_queue()
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    q = gq.Queue(doc, path, tmp_path / "state", None, gs.REPO / "contracts", persist=False)
    rec = gq.build_record(run_id="r1", queue_name=q.name, job=job, rec_cfg=q.record_cfg(job), attempt=1, started_at=1,
                          finished_at=2, status="SUCCEEDED", failure_reason=None, exit_code=0, run_dir=run_dir, log_path=None,
                          workdir=q.workdir, repo_dir=None, job_file=path, git=gq.git_info(None, doc.get("git")),
                          gpu={"model": "", "driver": "", "cuda": ""}, volume=None, cache=gq.HashCache(None))
    return rec, q.validate(rec), q.workdir


def _expected_hash(paths: list[str]) -> str:
    """기록기와 따로 계산한 기대값: 파일 하나면 그 파일, 여럿이면 (경로, 파일 해시) 목록의 해시."""
    files = [(p, "sha256:" + hashlib.sha256(Path(p).read_bytes()).hexdigest()) for p in paths]
    if len(files) == 1:
        return files[0][1]
    blob = json.dumps(files, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(blob).hexdigest()


SYNTH = {
    "hmr": {"run_id": "x", "result": "SUCCESS", "load": {"variant": "dinov3"}, "images": [
        {"image": "a.jpg", "n_people": 1, "finite": True, "result": "SUCCESS", "process_one_image_ms": 300.0, "meshes": ["m.ply"]},
        {"image": "b.jpg", "n_people": 5, "finite": True, "result": "SUCCESS", "process_one_image_ms": 500.0}]},
    "det": {"run_id": "x", "rows": [{"imgsz": 640, "batch": 1, "ms_per_frame_p50": 15.4, "people_detected": 5, "ms": {"p50": 15.4}}]},
    "ma": {"run_id": "x", "result": "SUCCESS", "infer_ms_warm": 783.2, "metric_scaling_factor": 2.94,
           "per_view": [{"name": "a", "valid_ratio": 0.87}]},
}


@pytest.mark.parametrize("image", ["exp02", "mapanything"])
def test_exp_jobs_record_real_weights_dataset_metrics_and_results(gs, ej, tmp_path, monkeypatch, image):
    vol = tmp_path / "vol"  # 데이터 루트(저장 위치)는 저장소 사본(큐 workdir)과 다른 곳
    lay = gs.Layout("pod", vol, "s2", PY, image)
    doc, _held = gs.build_jobs(lay, {"commit": "0" * 40, "branch": "dev", "dirty_worktree": False})
    jobs = [j for j in doc["jobs"] if j["record"].get("experiment_id") in ("EXP-01", "EXP-02")]
    assert len(jobs) == (4 if image == "exp02" else 2)
    for n, job in enumerate(jobs):
        rec_cfg = job["record"]
        for i, p in enumerate(rec_cfg["weights"] + rec_cfg["dataset"]):
            assert Path(p).is_absolute() and Path(p).is_relative_to(lay.simai), p
            target = Path(p) / "f.jpg" if not Path(p).suffix else Path(p)  # 폴더 입력(hmr_poc14·room4)은 안에 파일 하나
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(f"{job['name']}-{i}".encode() * 10)
        cmd = job["command"]
        out = Path(cmd[cmd.index("--out") + 1])
        assert out.is_relative_to(lay.simai / "runs") and out.name.endswith("_img_s2")  # 세션마다 다른 결과 폴더
        assert cmd[cmd.index("--") + 1] in ("simai-exp02", PY)
        kind = "ma" if image == "mapanything" else ("det" if "det_bench" in " ".join(cmd) else "hmr")
        run_dir = tmp_path / "run" / str(n)
        monkeypatch.setenv("SIMAI_RUN_DIR", str(run_dir))
        child = (f"import json, pathlib; p = pathlib.Path(r'{out}'); p.mkdir(parents=True, exist_ok=True); "
                 f"(p / 'metrics.json').write_text(json.dumps({SYNTH[kind]!r}))")
        assert ej.main(["--out", str(out), "--", PY, "-c", child]) == 0
        rec, errors, workdir = _full_record(gs, doc, job, run_dir, tmp_path)
        assert errors == [] and not lay.simai.is_relative_to(workdir)
        assert rec["weights_hash"] == _expected_hash(rec_cfg["weights"])
        assert rec["input_provenance"]["frames"]["dataset_hash"].startswith("sha256:")
        assert rec["metrics"] and all(isinstance(v, (int, float, str, bool, type(None))) for v in rec["metrics"].values())
        report = next(r for r in rec["results"] if r["kind"] == "REPORT")
        assert report["sha256"] == "sha256:" + hashlib.sha256((out / "metrics.json").read_bytes()).hexdigest()
        if kind == "hmr":
            assert rec["metrics"]["images.count"] == 2 and rec["metrics"]["images.result.success"] == 2
            assert rec["metrics"]["images.n_people.max"] == 5 and rec["metrics"]["images.finite.true"] == 2


def test_unified_mapanything_code_pins_match_the_torch271_image():
    """통합 이미지의 map-anything·DINOv2·WorldMirror 2.0 커밋이 deploy/mapanything(torch 2.7.1) 과 같다(S15P31A101-250·-539).
    map-anything 휠은 코드 층의 정확 sync 뒤에 --no-deps 로 깐다(앞에 깔면 정확 sync 가 지운다)."""
    pins = lambda text: dict(re.findall(r"^ARG (MA_COMMIT|DINOV2_COMMIT|WM2_COMMIT)=([0-9a-f]{40})$", text, re.M))  # noqa: E731
    unified = (REPO / "deploy" / "tier0" / "Dockerfile").read_text(encoding="utf-8")
    ma = (REPO / "deploy" / "mapanything" / "Dockerfile").read_text(encoding="utf-8")
    assert pins(unified) == pins(ma) and len(pins(ma)) == 3
    stage = unified[unified.index("AS unified\n"):]
    sync = stage.index("uv sync --locked --no-dev")
    wheel = stage.index("--no-deps --no-index /tmp/ma-dist/mapanything-")
    assert sync < wheel and "SIMAI_MAPANYTHING_REPO=" in stage and "SIMAI_MAPANYTHING_COMMIT=${MA_COMMIT}" in stage
    assert "SIMAI_WM2_REPO=" in stage and "SIMAI_WM2_COMMIT=${WM2_COMMIT}" in stage
    tier0 = unified[unified.index("AS tier0\n"):unified.index("AS unified-deps")]
    assert "mapanything" not in tier0.lower()  # tier0 타깃은 그대로


def test_unified_mapanything_items_mirror_m1_m2_through_scene_mode(gs, tmp_path):
    """u3·u4(S15P31A101-250): m1·m2 와 같은 입력·가중치·인자를 장면 준비 모드로 돌리고 run id 만 다르다(compare_envs 가 비교할 수 있게)."""
    by = {it["key"]: it for it in gs.items(gs.Layout("pod", tmp_path, "s3", PY, "unified"))}
    for u, m in (("u3", "m1"), ("u4", "m2")):
        cu, cm = by[u]["command"], by[m]["command"]
        assert by[u]["image"] == "unified" and by[m]["image"] == "mapanything"
        inner_u, inner_m = cu[cu.index("--") + 1:], cm[cm.index("--") + 1:]
        assert inner_u[:3] == [PY, "deploy/scripts/simai_bootstrap.py", "scene"]  # LIVE·GPU 사용 중 거부, static 가중치
        args_u, args_m = inner_u[3:], inner_m
        rid_u, rid_m = args_u[args_u.index("--run-id") + 1], args_m[args_m.index("--run-id") + 1]
        assert rid_u.endswith("_uni_s3") and rid_m.endswith("_img_s3") and rid_u.replace("_uni_", "_img_") == rid_m
        assert [a for a in args_u if a != rid_u] == [a for a in args_m if a != rid_m]  # 같은 스크립트·입력·모델·결과 폴더 위치
        assert by[u]["weights"] == by[m]["weights"] and by[u]["dataset"] == by[m]["dataset"]
        assert cu[cu.index("--out") + 1].endswith(rid_u) and by[u]["timeout_s"] >= by[u]["expected_s"]
        assert {r["path"].rsplit("/", 1)[-1].rsplit("\\", 1)[-1] for r in by[u]["results"]} >= {"metrics.json", "cameras.json", "raw.npz"}


def test_hmr_jobs_hash_every_weight_the_official_path_reads(gs, tmp_path):
    lay = gs.Layout("pod", tmp_path, "s", PY, "exp02")
    by = {it["key"]: it for it in gs.items(lay)}
    names = {Path(p).relative_to(lay.simai / "weights").as_posix() for p in by["g3"]["weights"]}
    assert names == {"yolo/yolo26x-pose.pt", "sam-3d-body-dinov3/model.ckpt", "sam-3d-body-dinov3/assets/mhr_model.pt",
                     "sam-3d-body-dinov3-fastcfg/model_config.yaml", "moge/moge-2-vits-normal/model.pt"}
    assert by["g1"]["weights"] == [str(lay.simai / "weights" / "yolo" / "yolo26x-pose.pt")]


def test_exp_job_refuses_finished_result_and_keeps_it(ej, tmp_path, monkeypatch):
    out = tmp_path / "H1_official_opt_img_s1"
    out.mkdir()
    (out / "metrics.json").write_text('{"result": "SUCCESS"}', encoding="utf-8")
    marker = tmp_path / "ran"
    monkeypatch.setenv("SIMAI_RUN_DIR", str(tmp_path / "run"))
    assert ej.main(["--out", str(out), "--", PY, "-c", f"open(r'{marker}', 'w')"]) == ej.EXIT_EXISTS
    assert not marker.exists() and (out / "metrics.json").read_text(encoding="utf-8") == '{"result": "SUCCESS"}'
    # 같은 세션의 실패한 시도가 남긴 반쯤 된 폴더(metrics.json 없음)는 이어 쓴다. 명령의 종료 코드를 그대로 낸다
    (out / "metrics.json").unlink()
    assert ej.main(["--out", str(out), "--", PY, "-c", "raise SystemExit(7)"]) == 7
    assert not (tmp_path / "run" / "metrics.json").exists()  # 지표 없음 → 비움(지어내지 않는다)


def test_session_refuses_without_run_record_validator(gs, tmp_path, monkeypatch):
    # 실험 이미지 venv 에 jsonschema 가 없으면 gpu_queue 가 기록 검증을 건너뛰고 성공으로 친다 → 그 상태로는 시작하지 않는다
    monkeypatch.setattr(gs, "validator_ready", lambda contracts=None: False)
    ran = []
    monkeypatch.setattr(gs, "run_queue", lambda *a: ran.append(a) or 0)
    code = gs.main(["run", "--mode", "dryrun", "--root", str(tmp_path), "--session", "v", "--min-free-mb", "1"])
    assert code == gs.EXIT_PREFLIGHT and ran == []
    # Pod 모드: 잠금 판으로 컨테이너 디스크에 설치를 시도하고, 그래도 없으면 이유를 돌려준다
    calls = []
    monkeypatch.setattr(gs.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or types.SimpleNamespace(stdout="", stderr="net"))
    why = gs.ensure_validator(gs.Layout("pod", tmp_path, "v", PY, "exp02"))
    assert why and "검증기" in why
    assert calls and calls[0][:3] == ["uv", "pip", "install"] and any(c.startswith("jsonschema==") for c in calls[0])


def test_validator_is_really_available_in_dev_env(gs):
    assert gs.validator_ready() is True  # 위 시험의 짝: 정상 환경에서는 통과


# ---------------------------------------------------------------- 층 목록 추출 (리뷰 R2)


def _blocks(text: str) -> dict[str, list[str]]:
    """requirements 형식에서 패키지별 블록(주석 줄 제외)."""
    out: dict[str, list[str]] = {}
    name = None
    for line in text.splitlines():
        if line and not line[0].isspace() and not line.startswith("#"):
            name = re.split(r"[ =;@]", line, maxsplit=1)[0]
            out.setdefault(name, [])
        if name and line.strip() and not line.lstrip().startswith("#"):
            out[name].append(line.rstrip())
    return out


def _layer_script(dockerfile: str) -> str:
    text = (REPO / dockerfile).read_text(encoding="utf-8")
    m = re.search(r"^RUN (set -eu;.*?rm one\.txt)$", text, re.S | re.M)
    assert m, "층 목록 RUN 을 찾지 못함"
    return m.group(1).replace("\\\n", "")  # Dockerfile 줄 잇기는 셸처럼 지운다


@pytest.mark.parametrize("dockerfile, groups", [
    ("deploy/tier0/Dockerfile", ("cuda", "torch", "trt", "wheels")),
    ("deploy/exp02/Dockerfile", ("cuda", "torch")),
    ("deploy/mapanything/Dockerfile", ("cuda", "torch")),
])
def test_layer_lists_keep_version_marker_hashes_without_duplicates(gs, tmp_path, dockerfile, groups):
    work = tmp_path / "src"
    work.mkdir()
    if "tier0" in dockerfile:
        for f in ("pyproject.toml", "uv.lock"):
            (work / f).write_bytes((REPO / "ai" / f).read_bytes())
    else:
        (work / "all.txt").write_bytes((REPO / dockerfile).with_name("requirements.lock.txt").read_bytes())
    r = subprocess.run([gs.find_bash(), "-c", _layer_script(dockerfile)], cwd=work, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr[-500:]
    full = _blocks((work / "all.txt").read_text(encoding="utf-8"))
    seen: set[str] = set()
    for g in groups:
        part = _blocks((work / f"{g}.txt").read_text(encoding="utf-8"))
        assert part, g
        for name, lines in part.items():
            assert name not in seen, f"{name} 가 두 묶음에"
            seen.add(name)
            assert lines == full[name], name  # 판·marker·해시 줄이 잠금 내보내기와 한 글자도 다르지 않다
            assert any("--hash=sha256:" in ln for ln in lines), name
    assert seen < set(full)  # 나머지는 뒤의 전체 설치가 맡는다


def test_unified_worker_layer_list_is_from_worker_export_and_disjoint_from_tier0_layers(gs, tmp_path):
    """통합 이미지 워커 층(S15P31A101-246): tier0+worker 내보내기와 한 글자도 다르지 않고, Tier 0 층 묶음과 겹치지 않는다."""
    for f in ("pyproject.toml", "uv.lock"):
        (tmp_path / f).write_bytes((REPO / "ai" / f).read_bytes())
    r = subprocess.run([gs.find_bash(), "-c", _layer_script("deploy/tier0/Dockerfile")], cwd=tmp_path, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr[-500:]
    full = _blocks((tmp_path / "all-worker.txt").read_text(encoding="utf-8"))
    tier0 = _blocks((tmp_path / "all.txt").read_text(encoding="utf-8"))
    worker = _blocks((tmp_path / "worker.txt").read_text(encoding="utf-8"))
    assert "vllm" in worker and "flashinfer-cubin" in worker
    for name, lines in worker.items():
        assert lines == full[name] and name not in tier0, name
    t0_layers = set().union(*(_blocks((tmp_path / f"{g}.txt").read_text(encoding="utf-8")) for g in ("cuda", "torch", "trt", "wheels")))
    assert not t0_layers & set(worker)
    # 잠금 밖 휠은 통합 이미지에 넣지 않는다(시연 HMR 경로가 쓰지 않는다)
    text = (REPO / "deploy" / "tier0" / "Dockerfile").read_text(encoding="utf-8")
    unified = text[text.index("AS unified-deps"):]
    assert "pip wheel" not in unified and "detectron2.git" not in unified and "--inexact" not in unified
    assert "NVIDIA_DRIVER_CAPABILITIES=compute,utility,video,graphics" in unified


def test_layer_list_fails_when_a_group_name_is_missing(gs, tmp_path):
    (tmp_path / "all.txt").write_text("numpy==2.4.6 \\\n    --hash=sha256:" + "0" * 64 + "\n", encoding="utf-8")
    r = subprocess.run([gs.find_bash(), "-c", _layer_script("deploy/exp02/Dockerfile")], cwd=tmp_path, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    assert r.returncode != 0 and "잠금에 없다" in r.stderr


# ---------------------------------------------------------------- 통합 이미지의 HMR 경로: 잠금 밖 휠 없이 (S15P31A101-246)


def _run_image(tmp_path, *extra):
    cv2 = pytest.importorskip("cv2")
    pytest.importorskip("torch")
    import numpy as np

    img = tmp_path / "cam_A_01.png"
    cv2.imwrite(str(img), np.zeros((32, 48, 3), np.uint8))
    env = {**os.environ, "SIMAI_ROOT": str(tmp_path / "root"), "PYTHONUTF8": "1"}
    return subprocess.run([sys.executable, str(REPO / "ai" / "experiments" / "hmr" / "run_image.py"), "--run-id", "t246",
                           "--images", str(img), "--mode", "official", *extra], env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300)


def test_unified_hmr_refuses_render_without_detectron2(tmp_path):
    import importlib.util

    if importlib.util.find_spec("detectron2") is not None:
        pytest.skip("Detectron2 가 있는 환경(exp02 이미지)")
    r = _run_image(tmp_path)
    assert r.returncode == 2 and "--no-render" in r.stderr and not (tmp_path / "root" / "runs").exists()


def test_unified_hmr_refuses_image_without_k_instead_of_guessing(tmp_path):
    import importlib.util

    if importlib.util.find_spec("moge") is not None:
        pytest.skip("MoGe 가 있는 환경(exp02 이미지)")
    r = _run_image(tmp_path, "--no-render")  # K 를 주지 않았다: MoGe 예비 경로도 없으니 모델을 올리기 전에 멈춘다
    assert r.returncode == 2 and "K 가 없는 그림 cam_A_01" in r.stderr, r.stderr[-800:]


def test_unified_hmr_check_fails_if_out_of_lock_wheels_sneak_in(xc, monkeypatch):
    """통합 이미지 점검(u1): Detectron2·MoGe·chumpy 가 들어 있으면 통과로 두지 않는다(시연 경로가 그것 없이 돈다는 것을 보는 항목)."""
    import importlib.util

    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda n, *a: object() if n == "moge" else real(n, *a))
    with pytest.raises(AssertionError, match="잠금 밖 휠"):
        xc.hmr_chain()
