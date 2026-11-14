"""부팅 비밀값 넘김 실제 exec 연결 (S15P31A101-707). GPU·네트워크·RunPod 를 쓰지 않는다.

16차 GPU 세션: simai-bootstrap auto 가 child_env(비밀 값을 지운 환경)로 simai-gpu-boot 를 exec 해 SIMAI_GPU_BOOT_SECRET 이 없다고 64 로 끝났고
Pod 이 16초마다 재시작했다. 앞 시험은 execvp 를 바꿔 끼우거나 simai-gpu-boot 를 바로 불러 이 연결을 못 봤다.
여기서는 실제 프로세스로 simai-start(sshd 끔) → simai_bootstrap.py auto → 가짜 simai-gpu-boot(SIMAI_GPU_BOOT_CMD) 까지 돌린다.
가짜는 비밀 값의 길이만 기록한다(원문은 파일·출력에 남기지 않는다). 다른 자식 경로(그대로 넘기는 명령)는 여전히 받지 않아야 한다.
Windows(노트북)의 os.execvpe 는 진짜 exec 가 아니고 uv 런처에서 가끔 Segmentation fault 로 죽어서, Windows 에서만 실행기가 os.execvpe 를
'같은 인자·같은 환경으로 띄우고 그 종료 코드로 끝내기'로 바꾼다. 넘기는 환경은 simai_bootstrap 의 실제 코드가 만든 그대로다(Linux CI 는 진짜 exec).
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.deploy.conftest import REPO

PY = Path(sys.executable).as_posix()
START = (REPO / "deploy" / "base" / "simai-start.sh").as_posix()
BOOTSTRAP = (REPO / "deploy" / "scripts" / "simai_bootstrap.py").as_posix()
# 가짜 값(실제 비밀 아님). 길이를 서로 다르게 해 어느 이름이 어느 값을 받았는지 구분한다
SECRET = "boot-secret-" + "s" * 31
TLS_KEY = "-----BEGIN PRIVATE KEY-----\n" + "K" * 64 + "\n-----END PRIVATE KEY-----\n"
HF = "hf_" + "F4keHfT0ken" * 3
HUB = "hf_" + "HubT0ken" * 5
NAMES = ("SIMAI_GPU_BOOT_SECRET", "SIMAI_TIER0_TLS_PRIVATE_KEY", "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN")
# 부모 환경에서 물려받으면 결과가 흔들리는 이름
DROP = (*NAMES, "SIMAI_GPU_BOOT_ID", "SIMAI_EC2_BASE_URL", "SIMAI_GPU_BOOT_CMD", "SIMAI_SECRETS_SOURCE", "PUBLIC_KEY", "SSH_PUBLIC_KEY",
        "SIMAI_AUTHORIZED_KEYS_FILE", "SIMAI_TUNNEL_PREFIX", "SIMAI_SERVICES")


@pytest.fixture
def tmp_path(tmp_path):
    """--basetemp '//?/C:/...'(Windows 긴 경로 접두) 로 돌려도 Git Bash·shlex 경로가 깨지지 않게 접두를 뗀다."""
    text = str(tmp_path)
    return Path(text[4:]) if text.startswith("\\\\?\\") else tmp_path


def _runner(tmp_path: Path) -> str:
    """simai_bootstrap.py 를 __main__ 으로 돌리는 실행기. Windows 에서만 os.execvpe 를 띄우고-기다리기로 바꾼다(위 설명)."""
    runner = tmp_path / "run_bootstrap.py"
    runner.write_text(
        "import os, runpy, subprocess, sys\n"
        "if os.name == 'nt':\n"
        "    def _exec(file, args, env):\n"
        "        sys.exit(subprocess.run(args, env=env).returncode)\n"
        "    os.execvpe = _exec\n"
        f"sys.argv = [{BOOTSTRAP!r}, *sys.argv[1:]]\n"
        f"runpy.run_path({BOOTSTRAP!r}, run_name='__main__')\n", encoding="utf-8")
    return runner.as_posix()


def _probe(tmp_path: Path) -> tuple[Path, Path]:
    """비밀 이름마다 받은 값의 길이와 마지막 인자만 out 에 쓰는 가짜 simai-gpu-boot."""
    probe, out = tmp_path / "probe.py", tmp_path / "probe-out.json"
    probe.write_text(
        "import json, os, sys\n"
        f"names = {NAMES!r}\n"
        "rec = {'lens': {k: len(os.environ.get(k, '')) for k in names}, 'last_arg': sys.argv[-1]}\n"
        "tmp = sys.argv[1] + '.tmp'\n"
        "open(tmp, 'w', encoding='utf-8').write(json.dumps(rec))\n"
        "os.replace(tmp, sys.argv[1])\n", encoding="utf-8")
    return probe, out


def _env(extra: dict[str, str]) -> dict[str, str]:
    base = {k: v for k, v in os.environ.items() if k not in DROP}
    return {**base, "SIMAI_SSHD": "off", "PYTHONIOENCODING": "utf-8", **extra}


def _wait(out: Path, timeout_s: float = 30.0) -> dict | None:
    """Windows 의 os.execvpe 는 새 프로세스를 띄우고 부모가 먼저 끝날 수 있어 기록이 생길 때까지 기다린다."""
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if out.exists():
            return json.loads(out.read_text(encoding="utf-8"))
        time.sleep(0.05)
    return None


def _assert_no_secret_text(*texts: str) -> None:
    for t in texts:
        for v in (SECRET, HF, HUB, "K" * 64):
            assert v not in t


def _boot_env(probe: Path, out: Path) -> dict[str, str]:
    return {"SIMAI_GPU_BOOT_SECRET": SECRET, "SIMAI_TIER0_TLS_PRIVATE_KEY": TLS_KEY, "HF_TOKEN": HF, "HUGGING_FACE_HUB_TOKEN": HUB,
            "SIMAI_GPU_BOOT_ID": "gpu-boot-test", "SIMAI_EC2_BASE_URL": "https://simai.example",
            "SIMAI_GPU_BOOT_CMD": shlex.join([PY, probe.as_posix(), out.as_posix()])}


def test_simai_start_auto_hands_boot_secrets_to_gpu_boot_but_not_to_other_children(gs, tmp_path):
    bash, run = gs.find_bash(), _runner(tmp_path)
    probe, out = _probe(tmp_path)
    env = _env(_boot_env(probe, out))
    r = subprocess.run([bash, START, PY, run, "auto"], env=env, capture_output=True, text=True, encoding="utf-8", timeout=60, check=False)
    rec = _wait(out)
    assert rec is not None, (r.returncode, r.stderr[-2000:])
    lens = rec["lens"]
    # 16차 재현: 고치기 전에는 셋 다 0 이었다. simai-start 는 HUGGING_FACE_HUB_TOKEN 을 지우고 HF_TOKEN 만 넘긴다(-205)
    assert lens == {"SIMAI_GPU_BOOT_SECRET": len(SECRET), "SIMAI_TIER0_TLS_PRIVATE_KEY": len(TLS_KEY), "HF_TOKEN": len(HF),
                    "HUGGING_FACE_HUB_TOKEN": 0}
    assert rec["last_arg"] == "auto"
    _assert_no_secret_text(r.stdout, r.stderr, out.read_text(encoding="utf-8"))

    # 그 밖의 자식 경로(준비 없이 그대로 넘기는 명령)는 같은 사슬에서도 비밀 값을 받지 않는다
    out.unlink()
    r = subprocess.run([bash, START, PY, run, PY, probe.as_posix(), out.as_posix()], env=env, capture_output=True, text=True, check=False,
                       encoding="utf-8", timeout=60)
    rec = _wait(out)
    assert rec is not None, (r.returncode, r.stderr[-2000:])
    assert rec["lens"] == dict.fromkeys(NAMES, 0)
    _assert_no_secret_text(r.stdout, r.stderr)

    # ID 가 없으면 넘기지 않고 64(템플릿 오류)
    out.unlink()
    env.pop("SIMAI_GPU_BOOT_ID")
    r = subprocess.run([bash, START, PY, run, "auto"], env=env, capture_output=True, text=True, encoding="utf-8", timeout=60, check=False)
    assert r.returncode == 64 and not out.exists()
    _assert_no_secret_text(r.stdout, r.stderr)


def test_bootstrap_auto_process_keeps_both_hf_names_and_pythonpath_for_gpu_boot(tmp_path):
    """simai-start 없이(엘리스 VM·손 실행) simai_bootstrap.py auto 를 바로 띄워도 simai-gpu-boot 가 받는 이름 넷을 다 넘긴다."""
    probe, out = _probe(tmp_path)
    r = subprocess.run([PY, _runner(tmp_path), "auto"], env=_env(_boot_env(probe, out)), capture_output=True, text=True, encoding="utf-8", check=False,
                       timeout=60)
    rec = _wait(out)
    assert rec is not None, (r.returncode, r.stderr[-2000:])
    assert rec["lens"] == {"SIMAI_GPU_BOOT_SECRET": len(SECRET), "SIMAI_TIER0_TLS_PRIVATE_KEY": len(TLS_KEY), "HF_TOKEN": len(HF),
                           "HUGGING_FACE_HUB_TOKEN": len(HUB)}
    _assert_no_secret_text(r.stdout, r.stderr, out.read_text(encoding="utf-8"))


def test_gpu_boot_env_keeps_secrets_and_child_env_still_drops_them(bs, tmp_path, monkeypatch):
    for k, v in zip(NAMES, (SECRET, TLS_KEY, HF, HUB)):
        monkeypatch.setenv(k, v)
    ai_dir = tmp_path / "ai"
    (ai_dir / "services").mkdir(parents=True)
    monkeypatch.setenv("SIMAI_AI_DIR", str(ai_dir))
    monkeypatch.delenv("PYTHONPATH", raising=False)
    boot, child = bs.gpu_boot_env(), bs.child_env()
    assert all(boot[k] for k in NAMES) and not set(NAMES) & set(child)
    assert boot["PYTHONPATH"] == child["PYTHONPATH"] == str(ai_dir)  # 이미지 PYTHONPATH 처리는 같다
