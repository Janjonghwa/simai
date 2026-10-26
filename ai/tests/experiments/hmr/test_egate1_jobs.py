"""E-GATE-1 GPU 쪽(S15P31A101-87): 큐 항목 생성기 형식과 run_multiview_hmr 의 torch 없는 보조(GPU·torch·cv2 없음).

반례: 마스크가 있는 사람과 없는 사람을 한 HMR 호출에 섞지 않음(없는 사람에게 사각형 마스크를 지어 주지 않음), crop 밖·빈 칸 keypoint 는
빈 프롬프트(−2), 보정 기록이 아닌 파일로는 큐를 만들지 않음, 결과 파일이 있으면 덮어쓰지 않음.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from simai_ai.hmr.prompt import HmrPrompt, PersonObservation

AI = Path(__file__).resolve().parents[3]
HMR = AI / "experiments" / "hmr"
ENV = {**os.environ, "PYTHONUTF8": "1"}


@pytest.fixture
def R(monkeypatch):
    monkeypatch.syspath_prepend(str(HMR))
    sys.modules.pop("run_multiview_hmr", None)
    return importlib.import_module("run_multiview_hmr")


def _prompt(mask):
    return HmrPrompt(PersonObservation("cam_A", (0.0, 0.0, 10.0, 20.0)), (0.0, 0.0, 10.0, 20.0), mask, None, None, ())


def test_hmr_groups_never_mix_masked_and_unmasked(R):
    m = np.ones((4, 4), bool)
    prompts = [_prompt(m), _prompt(None), _prompt(m)]
    assert R.hmr_groups(prompts, "mask") == [([0, 2], True), ([1], False)]
    assert R.hmr_groups(prompts, "bbox") == [([0, 1, 2], False)]
    assert R.hmr_groups([_prompt(None)], "mask_kp") == [([0], False)]
    assert R.hmr_groups([], "mask") == []
    with pytest.raises(ValueError):
        R.hmr_groups(prompts, "box")


def test_crop_prompt_marks_outside_and_empty_slots_as_dummy(R):
    xy = np.array([[[0.0, 0.1], [0.6, 0.0], [0.2, -0.2], [np.nan, 0.0]]])
    lab = np.array([[5, 6, -2, 9]])
    out = R.crop_prompt(xy, lab)
    np.testing.assert_allclose(out[0, 0], [0.5, 0.6, 5])
    for row in out[0, 1:]:
        np.testing.assert_allclose(row, [0, 0, -2])


def test_person_arrays_are_camera_coordinates_with_rotations(R):
    parents = np.array([-1, 0, 1, 2])
    rots = np.stack([np.eye(3)] * 4)
    out = {"pred_cam_t": np.array([0.1, 0.2, 3.0]), "pred_keypoints_3d": np.zeros((70, 3)), "pred_vertices": np.ones((8, 3)),
           "pred_global_rots": rots}
    mask = np.zeros((6, 6), bool)
    mask[1:3, 2:5] = True
    a = R.person_arrays(out, _prompt(mask), parents, hands_estimated=False)
    np.testing.assert_allclose(a["keypoints_cam"][0], [0.1, 0.2, 3.0], atol=1e-6)
    assert a["vertices_cam"].dtype == np.float16 and a["joint_rotations"].shape == (4, 3)
    np.testing.assert_allclose(a["camera_from_rig"], np.diag([1.0, -1.0, -1.0]))
    assert a["mask_box"].tolist() == [2, 1, 5, 3]
    a = R.person_arrays(out, _prompt(None), None, hands_estimated=False)
    assert "camera_from_rig" not in a and "mask_bits" not in a


def test_mask_modes_require_segmenter_weights(R):
    with pytest.raises(SystemExit):
        R.main(["--run-id", "x", "--frames", ".", "--calibration", "c.json", "--prompt", "mask"])
    with pytest.raises(SystemExit):
        R.main(["--run-id", "x", "--frames", ".", "--calibration", "c.json", "--prompt", "mask_kp", "--inference-type", "full",
                "--seg-model", "s.pt", "--seg-sha256", "sha256:" + "0" * 64])


def _local_inputs(tmp_path):
    cal = tmp_path / "intrinsics-20261021-022940z.json"
    cal.write_text(json.dumps({"format": "simai.intrinsics_estimate", "format_version": "0.2.0", "estimates": []}), encoding="utf-8")
    idx = tmp_path / "index.json"
    idx.write_text(json.dumps({"format": "simai.egate1_frames", "dataset_id": "recording-dev_test/rec-20261021-031045z-classroom-dev-test",
                               "instants": [{"instant": i, "frames": {c: {} for c in ("cam_A", "cam_B", "cam_C", "cam_D")}} for i in range(60)]}),
                   encoding="utf-8")
    return cal, idx


def _gen(tmp_path, cal, idx, out, *extra):
    return subprocess.run([sys.executable, str(HMR / "egate1_jobs.py"), "--repo", "/srv/simai/setup/session1/repo",
                           "--frames-dir", "/srv/simai/datasets/egate1-frames", "--calibration", "/srv/simai/datasets/classroom-k/x.json",
                           "--calibration-local", str(cal), "--frames-index-local", str(idx), "--session", "20261027-s4", "-o", str(out), *extra],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", env=ENV)


def test_jobs_cover_three_prompt_variants_and_pass_queue_check(tmp_path):
    cal, idx = _local_inputs(tmp_path)
    out = tmp_path / "jobs.json"
    r = _gen(tmp_path, cal, idx, out)
    assert r.returncode == 0, r.stderr
    doc = json.loads(out.read_text(encoding="utf-8"))
    modes = []
    for job in doc["jobs"]:
        cmd = job["command"]
        assert cmd[1] == "deploy/scripts/exp_job.py" and cmd[cmd.index("--") + 1] == "simai-exp02"
        assert cmd[cmd.index("--out") + 1].endswith(cmd[cmd.index("--run-id") + 1])  # exp_job 결과 폴더 = run_multiview_hmr 결과 폴더
        assert cmd[cmd.index("--inference-type") + 1] == "body"
        modes.append(cmd[cmd.index("--prompt") + 1])
        assert ("--seg-sha256" in cmd) == (modes[-1] != "bbox")
    assert modes == ["mask", "bbox", "mask_kp"]
    rec = doc["record"]
    assert (rec["experiment_id"], rec["e_gate"], rec["jira_key"]) == ("EXP-02", "E-GATE-1", "S15P31A101-87")
    assert rec["input_provenance"]["frames"]["dataset_id"].startswith("recording-dev_test/")
    assert rec["input_provenance"]["calibration"]["calibration_version"].startswith("intrinsics-20261021-022940z.json@sha256:")
    assert doc["queue"]["live_guard"]["on_live"] == "stop"
    check = subprocess.run([sys.executable, str(AI / "experiments" / "gpu_queue" / "gpu_queue.py"), "check", str(out)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", env=ENV, cwd=AI)
    assert check.returncode == 0, check.stdout + check.stderr
    assert _gen(tmp_path, cal, idx, out).returncode != 0  # 덮어쓰지 않는다


def test_jobs_refuse_non_intrinsics_calibration_and_unknown_variant(tmp_path):
    cal, idx = _local_inputs(tmp_path)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"cameras": []}), encoding="utf-8")
    assert _gen(tmp_path, bad, idx, tmp_path / "a.json").returncode != 0
    assert _gen(tmp_path, cal, idx, tmp_path / "b.json", "--variants", "e9").returncode != 0
    r = _gen(tmp_path, cal, idx, tmp_path / "c.json", "--variants", "e1")
    assert r.returncode == 0 and len(json.loads((tmp_path / "c.json").read_text(encoding="utf-8"))["jobs"]) == 1


def test_keypoint_hook_skips_reinference_when_all_prompts_are_dummy(R, monkeypatch):
    """리뷰 권장 2: 묶음 안 프롬프트가 모두 빈 칸([0, 0, −2])이면 run_keypoint_prompt 를 부르지 않고 그 사실을 남긴다."""
    import types

    monkeypatch.setitem(sys.modules, "torch", types.ModuleType("torch"))  # 건너뛰는 길은 torch 연산을 쓰지 않는다
    calls = []

    class Model:
        def run_inference(self, img, batch, inference_type="full", **kw):
            return {"image_embeddings": 1, "condition_info": 2, "mhr": 3}

        def run_keypoint_prompt(self, batch, out, prompt):
            calls.append(prompt)
            return out, prompt

    m = Model()
    hook = R.KeypointPromptHook(m)
    dummy = np.tile(np.array([0.0, 0.0, -2.0], np.float32), (2, 4, 1))
    hook.prompts = dummy
    out = m.run_inference(None, {"img": None}, inference_type="body")
    assert out == {"image_embeddings": 1, "condition_info": 2, "mhr": 3}
    assert calls == [] and (hook.skipped_all_dummy, hook.applied, hook.last) == (1, 0, "SKIPPED_ALL_DUMMY")
    assert R.all_dummy(dummy) and R.all_dummy(np.zeros((0, 4, 3)))
    real = dummy.copy()
    real[1, 0] = (10.0, 20.0, 5.0)
    assert not R.all_dummy(real)
