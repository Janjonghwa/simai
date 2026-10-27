"""GPU 1차 세션(2026-10-21 H100)에서 드러난 검증 도구 오류 반례 시험 (S15P31A101-213). GPU·RunPod 를 쓰지 않는다.

- c2: NVDEC 시험 자식 pytest 에 SIMAI_TEST_GPU=1 이 없으면 시험이 건너뛰어져 FAIL 이었다
- d P2·P3·P5·P6: watch 마다 따로 준 SIMAI_RUN_DIR 폴더가 없어 probe 가 metrics.json 을 못 쓰고 종료 1 → 측정 통과인데 FAIL
- 세션 Job 환경: 이미지의 SIMAI_CONTRACTS_DIR(옛 계약)가 사본 코드의 시험·검증에 쓰였다
- prepare: Git Bash(MSYS)가 --pod-simai /srv/simai 를 "C:/Program Files/Git/srv/simai" 로 바꿔 틀린 명령을 출력했다
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

JUNIT_ONE_PASS = '<testsuite name="pytest" tests="1" skipped="0" failures="0" errors="0"><testcase name="t"/></testsuite>'
JUNIT_ONE_SKIP = '<testsuite name="pytest" tests="1" skipped="1" failures="0" errors="0"><testcase name="t"><skipped/></testcase></testsuite>'


# ---------------------------------------------------------------- c2


def _fake_pytest(monkeypatch, live, junit: str) -> dict:
    seen: dict = {}

    def run(cmd, **kw):
        seen.update(kw)
        xml = next(a.split("=", 1)[1] for a in cmd if str(a).startswith("--junitxml="))
        Path(xml).write_text(junit, encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(live.subprocess, "run", run)
    return seen


@pytest.mark.parametrize("parent", [None, "0"])
def test_c2_forces_gpu_tests_on_in_child_pytest(live, tmp_path, monkeypatch, parent):
    """부모 환경에 없거나 0 이어도 자식 pytest 에는 SIMAI_TEST_GPU=1 (고치기 전: env 를 넘기지 않아 부모 값 그대로)."""
    if parent is None:
        monkeypatch.delenv("SIMAI_TEST_GPU", raising=False)
    else:
        monkeypatch.setenv("SIMAI_TEST_GPU", parent)
    monkeypatch.setenv("SIMAI_S213_KEEP", "x")
    seen = _fake_pytest(monkeypatch, live, JUNIT_ONE_PASS)
    status, detail, values = live.c2_nvdec_gpu_test(sys.executable, tmp_path)
    assert (seen.get("env") or {}).get("SIMAI_TEST_GPU") == "1", seen.get("env")
    assert seen["env"]["SIMAI_S213_KEEP"] == "x"  # 나머지 환경(PYTHONPATH·SIMAI_CONTRACTS_DIR 등)은 그대로 넘긴다
    assert status == "PASS" and values == {"passed": 1, "skipped": 0, "failed": 0}, detail


def test_c2_still_fails_when_gpu_test_is_skipped(live, tmp_path, monkeypatch):
    """CUDA 장치가 없어 건너뛰면 통과로 보지 않는다(모르면 FAIL, 기존 판정 유지)."""
    _fake_pytest(monkeypatch, live, JUNIT_ONE_SKIP)
    status, _, values = live.c2_nvdec_gpu_test(sys.executable, tmp_path)
    assert status == "FAIL" and values["skipped"] == 1


# ---------------------------------------------------------------- d watch 결과 폴더


def _marker_writer(marker: Path, stop: threading.Event) -> None:
    """Tier 0 표시 파일 흉내: 0.2 s 마다 새 창(written_at), 판정 tick 증가, 선확보 OK, 지연 100 ms."""
    i = 0
    while not stop.is_set():
        i += 1
        doc = {"gpu_priority": {"written_at": i, "load": {"lag_ms": [100.0], "unknown": 0}, "reserve": {"state": "OK"}},
               "status": {"judge": {"ticks": i * 10, "errors": 0}}}
        tmp = marker.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        try:
            os.replace(tmp, marker)
        except PermissionError:  # Windows: probe 가 읽는 중이면 다음 차례에 쓴다
            pass
        stop.wait(0.2)


def test_watch_passes_and_writes_metrics_into_its_own_run_dir(gp, tmp_path, monkeypatch):
    """측정이 통과하면 단계도 PASS 이고 probe metrics.json 은 .watch-<이름>/ 에 남는다.
    고치기 전: 폴더가 없어 probe 가 FileNotFoundError 로 종료 1 → 측정 통과인데 FAIL."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("SIMAI_RUN_DIR", str(run_dir))
    marker = tmp_path / "tier0.live"
    stop = threading.Event()
    writer = threading.Thread(target=_marker_writer, args=(marker, stop), daemon=True)
    writer.start()
    try:
        time.sleep(0.5)
        status, detail, values = gp._watch(sys.executable, marker, 3.5, run_dir / "baseline.json")
    finally:
        stop.set()
        writer.join(5)
    assert status == "PASS" and values["passed"] is True, detail
    metrics = json.loads((run_dir / ".watch-baseline" / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["passed"] == 1
    assert not (run_dir / "metrics.json").exists()  # 단계(Steps)의 metrics.json 자리를 probe 가 덮지 않는다


def test_watch_that_measures_failure_stays_fail(gp, tmp_path):
    """표시 파일이 없으면(모름) 폴더가 있어도 FAIL 이다(폴더를 만든다고 통과로 바뀌지 않는다)."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    status, _, values = gp._watch(sys.executable, tmp_path / "no-marker.live", 1.5, run_dir / "with_load.json")
    assert status == "FAIL" and values["passed"] is False and values["marker_missing_reads"] >= 1
    assert (run_dir / ".watch-with_load" / "metrics.json").is_file()


def test_kill_watch_env_creates_its_folder(gp, tmp_path, monkeypatch):
    """P5 는 watch 를 Popen 으로 따로 띄운다. 같은 도우미로 폴더를 만들고 부모 환경은 그대로 넘긴다."""
    monkeypatch.setenv("SIMAI_S213_KEEP", "x")
    env = gp._watch_env(tmp_path / "out" / ".watch-kill")
    assert Path(env["SIMAI_RUN_DIR"]).is_dir() and env["SIMAI_S213_KEEP"] == "x"
    gp._watch_env(tmp_path / "out" / ".watch-kill")  # 다시 돌려도(이어 돌기) 오류 없음


# ---------------------------------------------------------------- 세션 Job 환경의 계약 위치


@pytest.mark.parametrize("mode", ["pod", "dryrun"])
def test_session_jobs_use_the_copy_contracts_not_the_image_ones(gs, tmp_path, monkeypatch, mode):
    """이미지 환경 SIMAI_CONTRACTS_DIR=/opt/simai/contracts 가 있어도 Job 은 저장소 사본의 contracts/ 를 쓴다.
    gpu_queue 는 Job env 를 os.environ 위에 얹으므로 Job 값이 이긴다."""
    monkeypatch.setenv("SIMAI_CONTRACTS_DIR", "/opt/simai/contracts")
    monkeypatch.setenv("SIMAI_ROOT", str(tmp_path / "srv" / "simai"))
    lay = gs.Layout(mode, None if mode == "pod" else tmp_path, "s", None)
    doc, _ = gs.build_jobs(lay, None)
    assert doc["jobs"]
    for job in doc["jobs"]:
        assert job["env"]["SIMAI_CONTRACTS_DIR"] == str(gs.REPO / "contracts"), job["name"]
    assert (gs.REPO / "contracts" / "experiments" / "run_record.schema.json").is_file()


# ---------------------------------------------------------------- prepare 와 Git Bash 경로 변환


def _msys_root(tmp_path: Path) -> Path:
    """Git for Windows 설치 폴더 흉내(usr/bin/bash.exe). 실제 세션 값은 C:/Program Files/Git/srv/simai 였다."""
    root = tmp_path / "Program Files" / "Git"
    (root / "usr" / "bin").mkdir(parents=True)
    (root / "usr" / "bin" / "bash.exe").write_bytes(b"")
    return root


def _env(monkeypatch, **kw):
    for k in ("MSYSTEM", "MSYS_NO_PATHCONV", "MSYS2_ARG_CONV_EXCL"):
        monkeypatch.delenv(k, raising=False)
    for k, v in kw.items():
        monkeypatch.setenv(k, v)


@pytest.mark.parametrize("sub", ["srv/simai", "workspace/simai", "srv", ""])
def test_prepare_refuses_path_under_msys_root_even_without_shell_signal(gs, tmp_path, capsys, monkeypatch, sub):
    """MSYS 루트 아래, 그리고 루트 자체(/ → C:/Program Files/Git/)는 Git Bash 신호가 없어도 거절한다."""
    _env(monkeypatch)
    value = str(_msys_root(tmp_path) / sub) + ("/" if not sub else "")
    out = tmp_path / "prep"
    code = gs.main(["prepare", "--out", str(out), "--pod-simai", value])
    err = capsys.readouterr().err
    assert code == gs.EXIT_BAD_JOBS and "MSYS_NO_PATHCONV=1" in err
    assert not out.exists()  # 사본·명령을 만들지 않는다


@pytest.mark.parametrize("value", ["C:/Users/SSAFY/AppData/Local/Temp/simai", "C:/simai", "D:/srv/simai", r"c:\simai"])
def test_prepare_refuses_drive_path_in_git_bash(gs, tmp_path, capsys, monkeypatch, value):
    """Git Bash(MSYSTEM)에서 변환을 끄지 않았는데 드라이브 경로: /tmp/simai → %TEMP%/simai, /c/simai → C:/simai (리뷰 R1)."""
    _env(monkeypatch, MSYSTEM="MINGW64")
    out = tmp_path / "prep"
    code = gs.main(["prepare", "--out", str(out), "--pod-simai", value])
    assert code == gs.EXIT_BAD_JOBS and "MSYS_NO_PATHCONV=1" in capsys.readouterr().err
    assert not out.exists()


@pytest.mark.parametrize("signal", [{"MSYS_NO_PATHCONV": "1"}, {"MSYS2_ARG_CONV_EXCL": "*"}])
def test_drive_path_is_allowed_when_conversion_is_off_or_outside_git_bash(gs, tmp_path, monkeypatch, signal):
    """변환을 끈 Git Bash, 또는 Git Bash 밖(PowerShell·Linux)에서는 드라이브 경로가 변환 결과가 아니다(로컬 흉내 시험이 쓴다)."""
    local = (tmp_path / "fresh" / "simai").as_posix()
    _env(monkeypatch, MSYSTEM="MINGW64", **signal)
    assert not gs.msys_converted(local) and not gs.msys_converted("C:/simai")
    _env(monkeypatch)
    assert not gs.msys_converted(local) and not gs.msys_converted("C:/simai")


def test_msys_check_leaves_real_pod_paths_alone(gs, tmp_path, monkeypatch):
    """Pod 경로(POSIX·//srv 이스케이프)·상대 경로·MSYS 루트의 형제 폴더는 Git Bash 안에서도 그대로 둔다."""
    root = _msys_root(tmp_path)
    for kw in ({}, {"MSYSTEM": "MINGW64"}):
        _env(monkeypatch, **kw)
        for v in ("/srv/simai", "/workspace/simai", "/tmp/simai", "/", "//srv/simai", "srv/simai"):
            assert not gs.msys_converted(v), (kw, v)
    _env(monkeypatch)
    assert not gs.msys_converted(str(root.parent / "Gitx" / "srv" / "simai"))


def test_msys_check_treats_unreadable_path_as_not_msys(gs, tmp_path, monkeypatch):
    """읽을 수 없는 상위 폴더(비루트 Jenkins 의 /root/da3)는 예외 없이 '변환 아님'이다. 그 위의 MSYS 루트는 계속 잡는다(-297)."""
    root = _msys_root(tmp_path)
    locked = (root / "srv" / "locked", tmp_path / "root")
    real_is_file = Path.is_file

    def is_file(self, *a, **kw):  # 플랫폼과 실행 사용자에 관계없이 stat 권한 오류를 흉내 낸다
        if any(self.is_relative_to(d) for d in locked):
            raise PermissionError(13, "Permission denied", str(self))
        return real_is_file(self, *a, **kw)

    monkeypatch.setattr(Path, "is_file", is_file)
    _env(monkeypatch)
    assert not gs.msys_converted(str(tmp_path / "root" / "da3"))
    assert gs.msys_converted(str(root / "srv" / "locked" / "da3"))  # 읽을 수 있는 상위의 MSYS 루트는 잡는다


GIT_BASH = Path("C:/Program Files/Git/bin/bash.exe")


def _bash_prepare(gs, out: Path, pod: str, conv_off: bool) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("MSYS_NO_PATHCONV", "MSYS2_ARG_CONV_EXCL")}
    if conv_off:
        env["MSYS_NO_PATHCONV"] = "1"
    script = (gs.REPO / "deploy" / "scripts" / "gpu_session1.py").as_posix()
    cmd = f"'{Path(sys.executable).as_posix()}' '{script}' prepare --out '{out.as_posix()}' --pod-simai {pod}"
    return subprocess.run([str(GIT_BASH), "-c", cmd], env=env, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=300)


@pytest.mark.skipif(not GIT_BASH.is_file(), reason="Git for Windows 의 bash 가 있을 때(실제 경로 변환)")
@pytest.mark.parametrize("pod", ["/srv/simai", "/tmp/simai", "/", "/c/simai"])
def test_real_git_bash_conversion_is_refused(gs, tmp_path, pod):
    """실제 Git Bash 가 바꾼 인자(세션의 /srv/simai, 리뷰 R1 의 /tmp/simai·/·/c/simai)는 종료 4, 사본·명령 없음."""
    out = tmp_path / "prep"
    r = _bash_prepare(gs, out, pod, conv_off=False)
    assert r.returncode == gs.EXIT_BAD_JOBS and "MSYS_NO_PATHCONV=1" in r.stderr, r.stdout + r.stderr
    assert not out.exists() and "ssh " not in r.stdout


@pytest.mark.skipif(not GIT_BASH.is_file() or not (Path(__file__).resolve().parents[3] / ".git").exists(),
                    reason="Git for Windows bash 와 노트북 저장소가 있을 때")
def test_real_git_bash_with_conversion_off_prints_pod_path(gs, tmp_path):
    r = _bash_prepare(gs, tmp_path / "prep", "/tmp/simai", conv_off=True)
    assert r.returncode == gs.EXIT_OK, r.stdout + r.stderr
    assert "mkdir -p /tmp/simai/setup/session1'" in r.stdout and "root@<IP>:/tmp/simai/setup/session1/" in r.stdout


@pytest.mark.skipif(not (Path(__file__).resolve().parents[3] / ".git").exists(), reason="prepare 는 노트북 저장소에서만")
def test_prepare_prints_pod_paths_unchanged(gs, tmp_path, capsys):
    code = gs.main(["prepare", "--out", str(tmp_path / "prep"), "--pod-simai", "/srv/simai"])
    text = capsys.readouterr().out
    assert code == gs.EXIT_OK
    assert "mkdir -p /srv/simai/setup/session1'" in text and "Program Files" not in text
