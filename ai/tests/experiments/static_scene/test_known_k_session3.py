"""MapAnything known-K 실행 경로(-102): run_mapanything prepared 모드 입력 구성, 3차 GPU 세션 큐 항목 형식(GPU·torch 없음)."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import numpy as np
import pytest

AI = Path(__file__).resolve().parents[3]
STATIC = AI / "experiments" / "static_scene"
ENV = {**os.environ, "PYTHONUTF8": "1"}


def _load(name):
    spec = importlib.util.spec_from_file_location(f"static_scene_{name}", STATIC / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_prepared_views_pass_undistorted_k_per_view(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", types.ModuleType("torch"))  # 노트북에는 torch 가 없다. 입력 구성만 본다
    ma = _load("run_mapanything")
    K = [[1160.8, 0, 1151.5], [0, 1160.8, 647.5], [0, 0, 1]]
    (tmp_path / "prepared.json").write_text(json.dumps({
        "format": "simai.mapanything_inputs",
        "views": [{"name": "cam_A", "image": "cam_A.jpg", "K_undistorted": K, "mask": "cam_A.mask.png"},
                  {"name": "cam_B", "image": "cam_B.jpg", "K_undistorted": K, "mask": None}],
    }), encoding="utf-8")
    specs = ma.prepared_views(tmp_path)
    assert [s["name"] for s in specs] == ["cam_A", "cam_B"]
    assert specs[0]["path"] == str(tmp_path / "cam_A.jpg") and np.allclose(specs[0]["K"], K)
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "prepared.json").write_text(json.dumps({"format": "simai.mapanything_inputs", "views": [
        {"name": "cam_A", "image": "a.jpg", "K_undistorted": [[0, 0, 1], [0, 0, 1], [0, 0, 1]]}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="초점"):
        ma.prepared_views(bad)
    (bad / "prepared.json").write_text(json.dumps({"format": "other", "views": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="형식"):
        ma.prepared_views(bad)


def test_session3_jobs_cover_both_weights_and_pass_queue_check(tmp_path):
    out = tmp_path / "jobs.json"
    r = subprocess.run([sys.executable, str(STATIC / "session3_jobs.py"), "--repo", "/srv/simai/setup/session1/repo",
                        "--prepared", "/srv/simai/datasets/classroom4-prepared", "--session", "20261023-s3", "-o", str(out)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=ENV)
    assert r.returncode == 0, r.stderr
    doc = json.loads(out.read_text(encoding="utf-8"))
    models = []
    for job in doc["jobs"]:
        cmd = job["command"]
        assert cmd[1] == "deploy/scripts/exp_job.py"  # 끝난 결과를 덮어쓰지 않는 감싸개
        i = cmd.index("--mode")
        assert cmd[i + 1] == "prepared" and cmd[cmd.index("--prepared") + 1] == "/srv/simai/datasets/classroom4-prepared"
        models.append(Path(cmd[cmd.index("--model") + 1]).name)
    assert models == ["map-anything", "map-anything-apache"]
    assert doc["record"]["experiment_id"] == "VAL-01" and doc["record"]["jira_key"] == "S15P31A101-102"
    assert doc["record"]["input_provenance"]["is_upper_bound"] is False
    check = subprocess.run([sys.executable, str(AI / "experiments" / "gpu_queue" / "gpu_queue.py"), "check", str(out)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", env=ENV, cwd=AI)
    assert check.returncode == 0, check.stdout + check.stderr
    again = subprocess.run([sys.executable, str(STATIC / "session3_jobs.py"), "--repo", "/r", "--prepared", "/p",
                            "--session", "s", "-o", str(out)], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=ENV)
    assert again.returncode != 0  # 덮어쓰지 않는다


def test_use_local_dinov2_hub_bypasses_github_on_http_504_and_resolves_cache_dir(tmp_path, monkeypatch):
    """13차 재현(S15P31A101-566): GitHub 가 HTTP 504 를 내도 로컬 허브 캐시(facebookresearch_dinov2_main)가 있으면
    source='local' 로 바꿔 네트워크 없이 읽고, 캐시가 없거나 다른 저장소면 원래 호출을 유지한다."""
    import urllib.error

    monkeypatch.setitem(sys.modules, "torch", types.ModuleType("torch"))
    from simai_ai.scene import run_mapanything as rma

    hub_root = tmp_path / "torch_hub"
    cache_dir = hub_root / "facebookresearch_dinov2_main"
    cache_dir.mkdir(parents=True)
    (cache_dir / "hubconf.py").write_text("dependencies = ['torch']\n", encoding="utf-8")

    calls: list[tuple[str, str, dict]] = []

    def fake_hub_load(repo_or_dir, model, *args, **kwargs):
        calls.append((repo_or_dir, model, dict(kwargs)))
        if kwargs.get("source", "github") == "github":
            raise urllib.error.HTTPError(
                "https://github.com/facebookresearch/dinov2", 504, "Gateway Time-out", {}, None
            )
        return {"loaded_from": repo_or_dir, "model": model, "kwargs": kwargs}

    fake_hub = types.SimpleNamespace(get_dir=lambda: str(hub_root), load=fake_hub_load)
    # 1) 허브 캐시 감지 및 멱등 패치
    assert rma.resolve_local_hub_dir("facebookresearch/dinov2", hub_mod=fake_hub, env={}) == cache_dir
    assert rma.resolve_local_hub_dir("facebookresearch/dinov2:main", hub_mod=fake_hub, env={}) == cache_dir
    assert rma.resolve_local_hub_dir("other/repo", hub_mod=fake_hub, env={}) is None

    rma.use_local_dinov2_hub(fake_hub, env={})
    rma.use_local_dinov2_hub(fake_hub, env={})  # 두 번 불러도 중첩 감싸지 않는다
    out = fake_hub.load("facebookresearch/dinov2", "dinov2_vitg14", pretrained=False)
    assert out["loaded_from"] == str(cache_dir)
    assert out["model"] == "dinov2_vitg14"
    assert out["kwargs"] == {"pretrained": False, "source": "local"}
    assert len(calls) == 1

    # 2) SIMAI_DINOV2_HUB_DIR / TORCH_HOME 환경변수 경로 확인
    custom_dir = tmp_path / "custom_dinov2"
    custom_dir.mkdir()
    (custom_dir / "hubconf.py").write_text("dependencies = ['torch']\n", encoding="utf-8")
    no_dir_hub = types.SimpleNamespace(load=fake_hub_load)
    assert rma.resolve_local_hub_dir(
        "facebookresearch/dinov2", hub_mod=no_dir_hub, env={"SIMAI_DINOV2_HUB_DIR": str(custom_dir)}
    ) == custom_dir

    # 3) 캐시에 hubconf.py 가 없으면 원래 github 경로로 내려가 504 가 그대로 발생
    (cache_dir / "hubconf.py").unlink()
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        fake_hub.load("facebookresearch/dinov2", "dinov2_vitg14", pretrained=False)
    assert exc_info.value.code == 504

    # 4) 볼륨 설치 스크립트(setup_runpod.sh)도 facebookresearch_dinov2_main 허브 캐시를 준비한다
    setup_sh = (STATIC / "setup_runpod.sh").read_text(encoding="utf-8")
    assert "facebookresearch_dinov2_main" in setup_sh and "DINOV2_COMMIT" in setup_sh
