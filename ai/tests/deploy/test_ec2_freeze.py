from pathlib import Path
import os
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[3]
BASH = shutil.which("bash")


@pytest.mark.skipif(not BASH, reason="bash 필요")
def test_freeze_on_off_expiry_and_deploy(tmp_path):
    # Windows Git Bash에는 flock이 없어 시험용 단일 프로세스 대역을 둔다.
    def unix(path):
        if os.name == "nt":
            return subprocess.check_output([BASH, "-c", 'cygpath -u "$1"', "_", str(path)], text=True).strip()
        return str(path)

    root = unix(tmp_path)
    for name in ("deploy.sh", "freeze.sh"):
        source = (ROOT / "deploy/ec2" / name).read_text(encoding="utf-8")
        source = source.replace("SIMAI=/opt/simai", f"SIMAI='{root}'")
        if name == "deploy.sh":
            source = source.replace('env_get() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1; }', 'env_get() { :; }')
        (tmp_path / name).write_text(source, encoding="utf-8", newline="\n")
    (tmp_path / "flock").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
    (tmp_path / "flock").chmod(0o755)
    env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ["PATH"])

    def run(name, *args, **kwargs):
        return subprocess.run([BASH, unix(tmp_path / name), *args], env=env, text=True, encoding="utf-8", capture_output=True, **kwargs)

    assert run("freeze.sh", "on", "--hours", "2", "--reason", "GPU session").returncode == 0
    assert "GPU session" in run("freeze.sh", "status").stdout
    env["SSH_ORIGINAL_COMMAND"] = "deploy " + "a" * 40
    frozen = run("deploy.sh", input="archive bytes")
    assert frozen.returncode == 0 and "동결 중" in frozen.stdout
    assert run("freeze.sh", "off").returncode == 0
    assert "동결 없음" in run("freeze.sh", "status").stdout
    (tmp_path / "deploy.freeze").write_text("1\nexpired session\ntester\n", encoding="utf-8")
    assert "동결 만료" in run("freeze.sh", "status").stdout
    expired = run("deploy.sh", input="")
    assert "동결이 만료되어 배포한다" in expired.stderr
    assert expired.returncode != 0  # 빈 입력은 유효한 tar 가 아니다
    assert run("freeze.sh", "on", "--hours", "0", "--reason", "x").returncode != 0
