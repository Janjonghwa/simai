"""세션 큐 g4: HMR 보정 K 입력 경로 (S15P31A101-243) 반례 시험. GPU·Docker·실제 교실 사진을 쓰지 않는다(가짜 파일).

반례: 보정 기록이 없거나 형식이 다르면 Run 기록 calibration 을 지어내지 않고 판정이 실패, Job 기록의 보정 출처가 run_image 의
k_input.input_provenance(fs3db_common.k_summary)와 같은 값, 초점 차이 경계(0.5 px 는 실패), 모르는 값(None·필드 없음)은 통과 아님,
사람 없는 그림은 통과, MoGe 적재·그림 수·출처 섞임은 실패, 판정 실패는 Job 실패(종료 1), 사진은 저장소 안 폴더에 묶지 않음.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import sys
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
PY = Path(sys.executable).as_posix()
HMR = REPO / "ai" / "experiments" / "hmr"
SIZE = [2304, 1296]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def ck():
    return _load("check_calib_k_243", HMR / "check_calib_k.py")


@pytest.fixture
def ej():
    return _load("exp_job_243", REPO / "deploy" / "scripts" / "exp_job.py")


def _calib_doc() -> dict:
    cams = [{"camera_id": f"cam_{c}", "status": "OK",
             "camera": {"camera_id": f"cam_{c}", "image_size": SIZE, "K": [[1259.0, 0, 1152], [0, 1259.0, 648], [0, 0, 1]],
                        "distortion": {"model": "OPENCV_PINHOLE", "coeffs": [-0.3, 0.06, 0.0, 0.0]}}} for c in "ABCD"]
    return {"format": "simai.intrinsics_estimate", "format_version": 1, "estimates": cams}


def _layout(gs, tmp_path: Path, calib: dict | str | None):
    lay = gs.Layout("pod", tmp_path / "vol", "s3", PY, "exp02")
    stills = lay.simai / "datasets" / gs.G4_STILLS
    stills.mkdir(parents=True)
    (stills / "cam_A_01.jpg").write_bytes(b"jpg")
    path = lay.simai / "datasets" / "classroom-k" / gs.G4_CALIB
    if calib is not None:
        path.parent.mkdir(parents=True)
        path.write_text(calib if isinstance(calib, str) else json.dumps(calib), encoding="utf-8")
    return lay, path


def _g4(gs, lay) -> dict:
    doc, _held = gs.build_jobs(lay, {"commit": "0" * 40, "branch": "dev", "dirty_worktree": False})
    return doc, next(j for j in doc["jobs"] if j["name"] == "g4_exp02_h7_calib_k")


def _metrics(provenance: dict, n: int = 20) -> dict:
    rows = [{"image": f"cam_{'ABCD'[i // 5]}_{i % 5 + 1:02d}.jpg", "result": "SUCCESS", "finite": True, "n_people": 3,
             "k_input": {"source": "CALIBRATION", "undistorted": True, "image_space": "UNDISTORTED_ALPHA1"},
             "k_focal_diff_px": 0.0, "reproj_err_px": [0.0, 0.0, 0.0]} for i in range(n)]
    rows[-1].update(result="NO_PERSON", n_people=0, k_focal_diff_px=None, reproj_err_px=[])  # 사람 없는 그림(cam_D)
    return {"result": "PARTIAL", "images": rows,
            "load": {"fov": None, "load": [{"component": "hmr_dinov3"}, {"component": "detector_yolo_pose"}]},
            "k_input": {"source": "CALIBRATION", "count": {"CALIBRATION": n}, "undistorted": n,
                        "input_provenance": {k: provenance[k] for k in ("calibration", "is_upper_bound")}}}


# ---------------------------------------------------------------- 큐 구성


def test_g4_job_reads_calibration_and_records_same_provenance_as_run_image(gs, tmp_path, monkeypatch):
    lay, calib = _layout(gs, tmp_path, _calib_doc())
    doc, job = _g4(gs, lay)
    cmd = job["command"]
    tail = cmd[cmd.index("--") + 1:]
    assert tail[:3] == ["simai-exp02", PY, "ai/experiments/hmr/run_image.py"]
    assert tail[tail.index("--calibration") + 1] == str(calib) and tail[tail.index("--mode") + 1] == "official"
    assert tail[tail.index("--images") + 1] == str(lay.simai / "datasets" / gs.G4_STILLS)
    rec = job["record"]
    assert rec["jira_key"] == "S15P31A101-238" and rec["experiment_id"] == "EXP-02"
    assert not any("/moge/" in Path(w).as_posix() for w in rec["weights"]) and len(rec["weights"]) == 4  # MoGe 를 띄우지 않는다
    assert rec["dataset"] == [str(lay.simai / "datasets" / gs.G4_STILLS), str(calib)]  # 보정 기록도 입력(해시가 남는다)
    sha = "sha256:" + hashlib.sha256(calib.read_bytes()).hexdigest()
    prov = rec["input_provenance"]
    assert prov == {"frames": {"source": "FILE", "dataset_id": gs.G4_STILLS}, "is_upper_bound": False,
                    "calibration": {"source": "G1_AUTO", "calibration_version": f"{gs.G4_CALIB}@{sha}"}}
    # run_image 가 metrics 의 k_input.input_provenance 로 내는 값(fs3db_common)과 같다
    monkeypatch.setenv("SIMAI_ROOT", str(lay.simai))
    monkeypatch.syspath_prepend(str(HMR))
    sys.modules.pop("fs3db_common", None)
    import fs3db_common as c
    kin, why = c.known_k(c.K_CALIBRATION, calib, SIZE, stem="cam_B_03")
    assert why is None
    assert c.k_summary([kin, kin])["input_provenance"] == {"calibration": prov["calibration"], "is_upper_bound": False}
    # 판정 인자에 같은 기대값이 들어간다
    arg = next(a for a in cmd if a.startswith("--check-arg=--provenance="))
    assert json.loads(arg.split("=", 2)[2]) == prov
    # Run 기록 계약 통과, 입력 해시가 남는다
    gq = gs.load_gpu_queue()
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    q = gq.Queue(doc, path, tmp_path / "state", None, gs.REPO / "contracts", persist=False)
    full = gq.build_record(run_id="r1", queue_name=q.name, job=job, rec_cfg=q.record_cfg(job), attempt=1, started_at=1,
                           finished_at=2, status="SUCCEEDED", failure_reason=None, exit_code=0, run_dir=tmp_path / "run",
                           log_path=None, workdir=q.workdir, repo_dir=None, job_file=path, git=gq.git_info(None, doc.get("git")),
                           gpu={"model": "", "driver": "", "cuda": ""}, volume=None, cache=gq.HashCache(None))
    assert q.validate(full) == []
    assert full["input_provenance"]["calibration"] == prov["calibration"]
    assert full["input_provenance"]["frames"]["dataset_hash"].startswith("sha256:")


@pytest.mark.parametrize("calib", [None, "{not json", {"format": "other", "cameras": []}], ids=["missing", "broken", "other-format"])
def test_unknown_calibration_is_left_empty_and_check_fails(gs, ck, tmp_path, calib):
    lay, _path = _layout(gs, tmp_path, calib)
    _doc, job = _g4(gs, lay)
    prov = job["record"]["input_provenance"]
    assert "calibration" not in prov  # 지어내지 않는다(모델 추정 Run 과 같은 모양)
    good = _metrics({"calibration": {"source": "G1_AUTO", "calibration_version": "x@sha256:" + "0" * 64}, "is_upper_bound": False})
    assert any("calibration 이 없다" in b for b in ck.check(good, 20, prov))


# ---------------------------------------------------------------- 판정


PROV = {"frames": {"source": "FILE", "dataset_id": "classroom-stills-20261021"}, "is_upper_bound": False,
        "calibration": {"source": "G1_AUTO", "calibration_version": "intrinsics-20261021-022940z.json@sha256:" + "a" * 64}}


def _set(path: str, value):
    def f(m):
        *keys, last = path.split(".")
        d = m
        for k in keys:
            d = d[int(k)] if isinstance(d, list) else d[k]
        if value is KeyError:
            del d[last]
        else:
            d[last] = value
    return f


def test_check_passes_good_run_with_empty_image(ck):
    assert ck.check(_metrics(PROV), 20, PROV) == []
    m = _metrics(PROV)
    m["images"][0]["k_focal_diff_px"] = 0.4999  # 경계 바로 안
    assert ck.check(m, 20, PROV) == []


@pytest.mark.parametrize("mutate, why", [
    (_set("images.3.k_input.source", "MOGE_ESTIMATE"), "k_input.source"),
    (_set("images.3.k_input.undistorted", False), "펴짐"),
    (_set("images.3.k_input.image_space", KeyError), "image_space"),
    (_set("images.0.k_focal_diff_px", 0.5), "k_focal_diff_px"),  # 경계: 0.5 는 실패
    (_set("images.0.k_focal_diff_px", None), "k_focal_diff_px"),  # 사람이 있는데 초점 비교가 없음 = 모름
    (_set("images.0.k_focal_diff_px", float("nan")), "k_focal_diff_px"),
    (_set("images.0.finite", False), "finite"),
    (_set("images.0.finite", KeyError), "finite"),
    (_set("images.0.result", "ERROR"), "result"),
    (_set("images.0.reproj_err_px", [0.0, 0.5]), "reproj_err_px"),
    (_set("images.0.reproj_err_px", []), "reproj_err_px"),
    (_set("load.fov", "moge2"), "MoGe"),
    (lambda m: m["load"]["load"].append({"component": "fov_moge2"}), "MoGe"),
    (_set("load", KeyError), "적재 기록 없음"),
    (_set("k_input.count", {"CALIBRATION": 19, "MOGE_ESTIMATE": 1}), "k_input.count"),
    (_set("k_input.input_provenance.calibration.calibration_version", "intrinsics-20261021-022940z.json@sha256:" + "b" * 64),
     "Job 기록"),
    (_set("k_input.input_provenance", KeyError), "Job 기록"),
    (lambda m: m["images"].pop(), "19장"),
])
def test_check_counterexamples_fail(ck, mutate, why):
    m = copy.deepcopy(_metrics(PROV))
    mutate(m)
    bad = ck.check(m, 20, PROV)
    assert bad and any(why in b for b in bad), bad


# ---------------------------------------------------------------- exp_job 판정 연결 (실제 g4 명령의 판정 인자)


def _run_g4(gs, ej, tmp_path, monkeypatch, metrics: dict | None, child_exit: int = 0):
    lay, _calib = _layout(gs, tmp_path, _calib_doc())
    _doc, job = _g4(gs, lay)
    cmd = job["command"]
    out = Path(cmd[cmd.index("--out") + 1])
    write = "" if metrics is None else (f"p = pathlib.Path(r'{out}'); p.mkdir(parents=True, exist_ok=True); "
                                        f"(p / 'metrics.json').write_text({json.dumps(metrics)!r}); ")
    child = f"import pathlib, sys; {write}sys.exit({child_exit})"
    run_dir = tmp_path / "run"
    monkeypatch.setenv("SIMAI_RUN_DIR", str(run_dir))
    monkeypatch.chdir(REPO)  # 큐 workdir 는 저장소 사본(판정 스크립트 경로가 상대 경로)
    code = ej.main(cmd[2:cmd.index("--")] + ["--", PY, "-c", child])
    rec = run_dir / "metrics.json"
    return code, (json.loads(rec.read_text(encoding="utf-8")) if rec.is_file() else None), job["record"]["input_provenance"]


def test_exp_job_check_pass_and_fail_set_exit_and_metric(gs, ej, tmp_path, monkeypatch):
    lay, _ = _layout(gs, tmp_path / "p", _calib_doc())
    prov = _g4(gs, lay)[1]["record"]["input_provenance"]
    code, rec, _ = _run_g4(gs, ej, tmp_path / "ok", monkeypatch, _metrics(prov))
    assert code == 0 and rec["check.passed"] is True
    bad = _metrics(prov)
    bad["images"][2]["k_input"]["source"] = "MOGE_ESTIMATE"
    code, rec, _ = _run_g4(gs, ej, tmp_path / "bad", monkeypatch, bad)
    assert code == 1 and rec["check.passed"] is False  # 판정 실패는 Job 실패(SUCCEEDED 로 두지 않는다)


def test_exp_job_check_without_metrics_fails_and_failed_command_keeps_its_code(gs, ej, tmp_path, monkeypatch):
    code, rec, _ = _run_g4(gs, ej, tmp_path / "none", monkeypatch, None)
    assert code == 1 and rec is None  # 지표가 없으면 판정할 수 없다 → 실패
    code, rec, _ = _run_g4(gs, ej, tmp_path / "crash", monkeypatch, None, child_exit=7)
    assert code == 7


# ---------------------------------------------------------------- prepare: g4 입력 묶음


def _stills(tmp_path: Path, n: int) -> tuple[Path, Path]:
    stills = tmp_path / "rec" / "classroom-stills-20261021"
    stills.mkdir(parents=True)
    for i in range(n):
        (stills / f"cam_{'ABCD'[i // 5]}_{i % 5 + 1:02d}.jpg").write_bytes(b"jpg")
    calib = tmp_path / "rec" / "intrinsics-20261021-022940z.json"
    calib.write_text(json.dumps(_calib_doc()), encoding="utf-8")
    return stills, calib


def test_prepare_bundles_g4_inputs_outside_repo_only(gs, tmp_path):
    stills, calib = _stills(tmp_path, 20)
    out = tmp_path / "prep"
    out.mkdir()
    tar, note = gs.g4_inputs(out, stills, calib)
    assert tar == (out / "g4-inputs.tar").resolve() and "내부 확인용" in note
    assert [p.name for p in out.iterdir()] == ["g4-inputs.tar"]  # 임시 파일이 남지 않는다
    with tarfile.open(tar) as t:
        names = sorted(t.getnames())
    assert len(names) == 21 and f"classroom-k/{gs.G4_CALIB}" in names
    assert all(n.startswith(f"{gs.G4_STILLS}/cam_") for n in names if not n.startswith("classroom-k/"))
    # 저장소 안 폴더에는 사람이 찍힌 사진을 묶지 않는다
    inside = gs.REPO / "ai" / "experiments" / "runs"
    tar2, note2 = gs.g4_inputs(inside, stills, calib)
    assert tar2 is None and "저장소 안" in note2 and not (inside / "g4-inputs.tar").exists()


def test_prepare_reports_missing_or_short_g4_inputs(gs, tmp_path):
    stills, calib = _stills(tmp_path, 19)
    out = tmp_path / "prep"
    out.mkdir()
    tar, note = gs.g4_inputs(out, stills, calib)
    assert tar is None and "19/20" in note and not (out / "g4-inputs.tar").exists()
    calib.unlink()
    (stills / "cam_D_05.jpg").write_bytes(b"jpg")
    tar, note = gs.g4_inputs(out, stills, calib)
    assert tar is None and "보정 기록 없음" in note


# ---------------------------------------------------------------- prepare: 링크로 저장소 안에 쓰는 우회 (리뷰 R1)


def _fake_repo(gs, tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    monkeypatch.setattr(gs, "REPO", repo)
    out = tmp_path / "outside"
    out.mkdir()
    return repo, out


def _link_or_skip(make):
    try:
        make()
    except (OSError, NotImplementedError) as e:  # Windows 는 개발자 모드·권한 없이 symlink 를 만들 수 없다
        pytest.skip(f"링크를 만들 수 없는 환경: {e}")


def test_prepare_refuses_existing_hardlink_into_repo(gs, tmp_path, monkeypatch):
    repo, out = _fake_repo(gs, tmp_path, monkeypatch)
    stills, calib = _stills(tmp_path, 20)
    target = repo / "privacy.tar"
    target.write_bytes(b"SENTINEL")
    os.link(target, out / "g4-inputs.tar")
    tar, note = gs.g4_inputs(out, stills, calib)
    assert tar is None and "이미 있다" in note
    assert target.read_bytes() == b"SENTINEL"  # 저장소 안 파일을 덮어쓰지 않았다
    assert sorted(p.name for p in out.iterdir()) == ["g4-inputs.tar"]  # 임시 파일도 남기지 않는다


def test_prepare_refuses_existing_plain_file_without_overwriting(gs, tmp_path, monkeypatch):
    _repo, out = _fake_repo(gs, tmp_path, monkeypatch)
    stills, calib = _stills(tmp_path, 20)
    (out / "g4-inputs.tar").write_bytes(b"OLD")
    tar, note = gs.g4_inputs(out, stills, calib)
    assert tar is None and (out / "g4-inputs.tar").read_bytes() == b"OLD"


def test_prepare_refuses_file_symlink_into_repo(gs, tmp_path, monkeypatch):
    repo, out = _fake_repo(gs, tmp_path, monkeypatch)
    stills, calib = _stills(tmp_path, 20)
    target = repo / "privacy.tar"
    target.write_bytes(b"SENTINEL")
    _link_or_skip(lambda: (out / "g4-inputs.tar").symlink_to(target))
    tar, _note = gs.g4_inputs(out, stills, calib)
    assert tar is None and target.read_bytes() == b"SENTINEL"
    dangling = repo / "not-yet.tar"  # 대상이 아직 없는 링크도 따라가 만들지 않는다
    (out / "g4-inputs.tar").unlink()
    (out / "g4-inputs.tar").symlink_to(dangling)
    assert gs.g4_inputs(out, stills, calib)[0] is None and not dangling.exists()


def test_prepare_refuses_out_dir_symlink_into_repo(gs, tmp_path, monkeypatch):
    repo, _out = _fake_repo(gs, tmp_path, monkeypatch)
    stills, calib = _stills(tmp_path, 20)
    link = tmp_path / "looks-outside"
    _link_or_skip(lambda: link.symlink_to(repo / "sub", target_is_directory=True))
    tar, note = gs.g4_inputs(link, stills, calib)
    assert tar is None and "저장소 안" in note and list((repo / "sub").iterdir()) == []


def test_prepare_relative_out_resolves_against_cwd(gs, tmp_path, monkeypatch):
    repo, out = _fake_repo(gs, tmp_path, monkeypatch)
    stills, calib = _stills(tmp_path, 20)
    monkeypatch.chdir(repo / "sub")
    tar, note = gs.g4_inputs(Path(".."), stills, calib)  # 상대 경로로 저장소 최상위
    assert tar is None and "저장소 안" in note and not (repo / "g4-inputs.tar").exists()
    tar, _ = gs.g4_inputs(Path("../../outside"), stills, calib)  # 상대 경로라도 실제로 밖이면 만든다
    assert tar == (out / "g4-inputs.tar").resolve() and tar.is_file()
