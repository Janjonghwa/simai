"""GPU 1차 세션을 그때그때 남은 GPU·지역(볼륨 없음)에서 돌리기 (S15P31A101-205) 반례 시험. GPU·RunPod 를 쓰지 않는다.

반례: 볼륨 없는 Pod 의 저장 위치·a 볼륨 측정 SKIP·결과 회수 안내, 일본 데이터센터 고정 값이 Job 파일에 남지 않음, 다른 지역 볼륨,
e·f 제외와 g·m 자리만 표시, Pod 점검의 최소 사양(Ampere·24GB급) 판정과 다른 지역 허용, d 부하의 GPU 메모리 비율(24·48·80GB,
메모리를 모르면 FAIL), 24GB 에서 의미 없는 부하 단계는 SKIP 이면서 필수 실패로 세지 않음.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
PY = Path(sys.executable).as_posix()
FAKE_SMI = (REPO / "ai" / "tests" / "experiments" / "gpu_queue" / "fake_nvidia_smi.py").as_posix()


def _clear(monkeypatch):
    for k in ("FAKE_SMI_FAIL", "FAKE_SMI_CC", "FAKE_SMI_TOTAL_MB", "RUNPOD_DC_ID", "RUNPOD_PUBLIC_IP", "RUNPOD_TCP_PORT_22",
              "PUBLIC_IPADDR", "VAST_TCP_PORT_22"):
        monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------- 세션 큐 (볼륨 없음)


def test_volume_less_pod_plan_skips_volume_measure_and_drops_fixed_region(gs, tmp_path, monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SIMAI_ROOT", str(tmp_path / "srv" / "simai"))  # simai-bootstrap 이 컨테이너 디스크를 고른 Pod
    lay = gs.Layout("pod", None, "s", "/opt/simai/ai/.venv/bin/python")
    assert lay.simai == tmp_path / "srv" / "simai" and lay.root == tmp_path / "srv" and lay.has_volume is False
    doc, held = gs.build_jobs(lay, None)
    a = next(j for j in doc["jobs"] if j["name"] == "a_pod_check")["command"]
    assert "--no-volume" in a and str(tmp_path / "srv") in a
    text = json.dumps(doc, ensure_ascii=False)
    assert "AP-JP-1" not in text and "volume" not in doc["queue"] and "provider" not in doc["record"]["gpu"]
    assert doc["jobs"][0]["env"]["SIMAI_ROOT"] == str(lay.simai)
    table = gs.plan_table(lay, doc, held)
    assert "제외: 엘리스 블록 스토리지" in table and "제외: GPU 가 필요 없어" in table
    assert table.count("자리만: 실험 이미지 티켓 후") == 2
    assert "볼륨 없음" in table and "scp" in table


def test_pod_with_volume_in_another_region_records_that_datacenter(gs, tmp_path, monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SIMAI_ROOT", str(tmp_path / "workspace" / "simai"))
    monkeypatch.setenv("RUNPOD_DC_ID", "EU-RO-1")
    monkeypatch.setattr(gs.os.path, "ismount", lambda p: Path(p) == tmp_path / "workspace")
    lay = gs.Layout("pod", None, "s", None)
    doc, _held = gs.build_jobs(lay, None)
    assert lay.has_volume and doc["queue"]["volume"] == {"root": str(tmp_path / "workspace"), "datacenter": "EU-RO-1"}
    assert "--no-volume" not in doc["jobs"][0]["command"]


def test_scp_hint_uses_vast_address(gs, monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("PUBLIC_IPADDR", "203.0.113.7")
    monkeypatch.setenv("VAST_TCP_PORT_22", "41022")
    cmds = gs.scp_command(Path("/srv/simai/runs/gpu-session1/s/b.tar.gz"))
    assert "scp -P 41022" in cmds[1] and "root@203.0.113.7:" in cmds[1]


# ---------------------------------------------------------------- Pod 점검


def _pod_check(pc, tmp_path, monkeypatch, *extra, **smi):
    _clear(monkeypatch)
    for k, v in smi.items():
        monkeypatch.setenv(k, v)
    out = tmp_path / "pod_check.json"
    monkeypatch.delenv("SIMAI_RUN_DIR", raising=False)
    code = pc.main(["--root", str(tmp_path), "--nvidia-smi", json.dumps([PY, FAKE_SMI]), "--min-free-gb", "0",
                    "--write-mb", "1", "--fsync-count", "5", "--out", str(out), *extra])
    return code, {i["name"]: i["status"] for i in json.loads(out.read_text(encoding="utf-8"))["items"]}


def test_pod_check_without_volume_skips_volume_measurements_but_keeps_rename(pc, tmp_path, monkeypatch):
    code, st = _pod_check(pc, tmp_path, monkeypatch, "--no-volume", FAKE_SMI_CC="8.9", FAKE_SMI_TOTAL_MB="24564",
                          FAKE_SMI_NAME="NVIDIA GeForce RTX 4090")
    assert st["volume_mount"] == st["volume_write"] == st["fsync"] == "SKIP"
    assert st["atomic_replace"] == "PASS" and st["gpu"] == "PASS" and code == 0
    # 음성 대조: --no-volume 이 없으면 마운트 지점이 아닌 폴더는 FAIL(예전 동작 그대로)
    code, st = _pod_check(pc, tmp_path, monkeypatch)
    assert st["volume_mount"] == "FAIL" and st["volume_write"] == "PASS" and code == 1


def test_pod_check_accepts_any_region_but_fails_below_minimum_gpu(pc, tmp_path, monkeypatch):
    code, st = _pod_check(pc, tmp_path, monkeypatch, "--no-volume", FAKE_SMI_CC="7.5", FAKE_SMI_TOTAL_MB="15360",
                          RUNPOD_DC_ID="US-KS-2")
    assert st["datacenter"] == "PASS" and st["gpu"] == "FAIL" and code == 1


# ---------------------------------------------------------------- d 부하 비율


@pytest.mark.parametrize("total, want", [
    ("81559", {"P3": ("RUN", 40_000), "P5": ("RUN", 20_000), "P6": ("RUN", 70_000)}),  # H100 80GB: -60 MR 값 그대로
    ("46068", {"P3": ("RUN", 23_000), "P5": ("RUN", 11_000), "P6": ("RUN", 40_000)}),  # L40S 48GB
    ("24564", {"P3": ("SKIP", 12_000), "P5": ("SKIP", 6_000), "P6": ("SKIP", 21_000)}),  # RTX 4090: Tier 0 전용
])
def test_d_loads_scale_with_detected_gpu_memory(gp, monkeypatch, total, want):
    _clear(monkeypatch)
    monkeypatch.setenv("FAKE_SMI_TOTAL_MB", total)
    total_mb = gp.gpu_total_mb([PY, FAKE_SMI])
    loads = gp.plan_loads(total_mb, 8192)
    assert {k: (v["status"], v["mem_mb"]) for k, v in loads.items()} == want
    cap = gp.LOAD_FRACTION * total_mb
    for k, v in loads.items():
        if v["status"] == "RUN":
            assert (v["mem_mb"] > cap) == (k == "P6")  # P6 만 상한을 넘는다(메모리 부족으로 끝나야 하는 단계)
        else:
            assert "Tier 0 전용" in v["why"]


def test_d_loads_fail_when_gpu_memory_is_unknown(gp, monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("FAKE_SMI_FAIL", "1")
    loads = gp.plan_loads(gp.gpu_total_mb([PY, FAKE_SMI]), 8192)
    assert {v["status"] for v in loads.values()} == {"FAIL"}  # 모르면 SKIP(통과)로 보지 않는다


@pytest.mark.parametrize("total, p3_status, p3_required", [("24564", "SKIP", False), ("81559", "FAIL", True)])
def test_d_skipped_load_steps_are_not_counted_as_required_failures(gp, tmp_path, monkeypatch, total, p3_status, p3_required):
    _clear(monkeypatch)
    monkeypatch.setenv("FAKE_SMI_TOTAL_MB", total)
    monkeypatch.setenv("SIMAI_NVIDIA_SMI", f"{PY} {FAKE_SMI}")
    monkeypatch.setenv("SIMAI_RUN_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("SIMAI_ROOT", str(tmp_path / "simai"))
    monkeypatch.setattr(gp, "PROBE", tmp_path / "no-probe.py")  # 측정 도구 없음: 하네스를 띄우지 않는다
    monkeypatch.setattr(gp, "_pytest", lambda py, out: ("PASS", "stub", {}))
    assert gp.main([]) == 1  # P1·P2 는 측정 도구가 없어 실패
    steps = {s["step"]: s for s in json.loads((tmp_path / "run" / "steps.json").read_text(encoding="utf-8"))["steps"]}
    assert (steps["P3_with_load"]["status"], steps["P3_with_load"]["required"]) == (p3_status, p3_required)
    assert steps["P6_over_limit"]["required"] is p3_required
