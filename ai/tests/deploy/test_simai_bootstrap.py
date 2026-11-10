"""simai-bootstrap·simai-start (S15P31A101-205) 반례 시험. GPU·네트워크·Docker 를 쓰지 않는다(가짜 nvidia-smi, file:// 가중치, 가짜 sshd).

반례: 저장 위치(환경 변수/볼륨/컨테이너), GPU 없음·최소 사양 미만은 가중치를 받기 전에 코드 3, 가중치 없음(받아서 확인)·이미 있음(건너뜀)·
있는데 해시 불일치(지우지 않고 코드 2)·받은 파일 해시 불일치(버리고 코드 2)·목록에 해시 없음(코드 2), 남은 용량 하한, HF 토큰이 다른 호스트
리다이렉트·출력·기록·exec 환경에 남지 않음, 준비 없이 넘기는 명령, SSH 공개키 방식별 authorized_keys(RunPod·Vast·파일·중복·끔),
이미지에 들어가는 실행 스크립트가 LF 인지.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
PY = Path(sys.executable).as_posix()
FAKE_SMI = (REPO / "ai" / "tests" / "experiments" / "gpu_queue" / "fake_nvidia_smi.py").as_posix()
TOKEN = "hf_" + "T0kenValue" * 4
WEIGHTS = b"fake-yolo-weights" * 1000


@pytest.fixture
def tmp_path(tmp_path):
    """--basetemp '//?/C:/...'(Windows 긴 경로 접두) 로 돌려도 file:// 주소·Git Bash 경로가 깨지지 않게 접두를 뗀다."""
    text = str(tmp_path)
    return Path(text[4:]) if text.startswith("\\\\?\\") else tmp_path


def _setup(bs, tmp_path: Path, monkeypatch, *, sha: str | None = None, source: dict | None = None) -> dict:
    """저장 위치·가짜 nvidia-smi·가중치 목록(file:// 원본)을 꾸민다. exec 는 가로채 기록한다."""
    src = tmp_path / "origin.pt"
    src.write_bytes(WEIGHTS)
    manifest = tmp_path / "weights.json"
    entry = {"name": "yolo26x-pose", "sets": ["tier0"], "path": "weights/yolo/yolo26x-pose.pt",
             "source": source or {"url": src.as_uri()}, "license": "AGPL-3.0", "env": "SIMAI_TIER0_WEIGHTS",
             "sha256": hashlib.sha256(WEIGHTS).hexdigest() if sha is None else sha}
    manifest.write_text(json.dumps({"manifest_version": 1, "weights": [entry]}), encoding="utf-8")
    for k in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "SIMAI_TIER0_WEIGHTS", "SIMAI_TIER0_WEIGHTS_SHA256", "FAKE_SMI_FAIL",
              "FAKE_SMI_CC", "FAKE_SMI_TOTAL_MB", "SIMAI_SESSION_REPO", "SIMAI_BAKED_WEIGHTS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SIMAI_ROOT", str(tmp_path / "simai"))
    monkeypatch.setenv("SIMAI_NVIDIA_SMI", f"{PY} {FAKE_SMI}")
    monkeypatch.setenv("SIMAI_WEIGHTS_MANIFEST", str(manifest))
    monkeypatch.setenv("SIMAI_MIN_FREE_GB", "0")
    calls: list = []
    monkeypatch.setattr(bs, "execvp", lambda argv: calls.append((list(argv), dict(os.environ))))
    return {"calls": calls, "target": tmp_path / "simai" / "weights" / "yolo" / "yolo26x-pose.pt", "root": tmp_path / "simai"}


def _records(root: Path) -> list[dict]:
    d = root / "runs" / "bootstrap"
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(d.glob("*.json"))] if d.is_dir() else []


def _no_network(_req):
    raise AssertionError("받으면 안 되는 상황에서 받으려 했다")


# ---------------------------------------------------------------- 저장 위치


def test_storage_choice_env_then_workspace_then_container(bs, tmp_path, monkeypatch):
    ws, ct = tmp_path / "workspace", tmp_path / "container" / "simai"
    assert bs.choose_root({"SIMAI_ROOT": str(tmp_path / "x")}, ws, ct) == (tmp_path / "x", "env")
    assert bs.choose_root({}, ws, ct) == (ct, "container")  # /workspace 없음 → 컨테이너 디스크
    ws.mkdir()
    # S15P31A101-311: RunPod 의 볼륨 없는 /workspace 폴더처럼 마운트 지점이 아니면 고르지 않고 컨테이너 디스크를 쓴다
    assert bs.choose_root({}, ws, ct, is_mount=lambda _p: False) == (ct, "container")
    assert bs.choose_root({"SIMAI_ROOT": ""}, ws, ct, is_mount=lambda _p: False) == (ct, "container")
    # 마운트 지점(볼륨)이고 쓸 수 있을 때만 /workspace/simai, SIMAI_ROOT 가 있으면 마운트 여부와 상관없이 우선
    mounted = lambda p: Path(p) == ws  # noqa: E731
    assert bs.choose_root({}, ws, ct, is_mount=mounted) == (ws / "simai", "workspace")
    assert bs.choose_root({"SIMAI_ROOT": ""}, ws, ct, is_mount=mounted) == (ws / "simai", "workspace")  # 빈 값은 없는 것
    assert bs.choose_root({"SIMAI_ROOT": str(tmp_path / "x")}, ws, ct, is_mount=mounted) == (tmp_path / "x", "env")
    # main 경로: SIMAI_ROOT 없음 + /workspace 가 있지만 마운트 지점이 아님 → 컨테이너 디스크에 가중치를 받고 그 위치를 Tier 0 에 넘긴다
    st = _setup(bs, tmp_path, monkeypatch)
    monkeypatch.delenv("SIMAI_ROOT")
    monkeypatch.setattr(bs, "WORKSPACE", ws)
    monkeypatch.setattr(bs, "CONTAINER_ROOT", ct)
    monkeypatch.setattr(bs.os.path, "ismount", lambda _p: False)
    assert bs.main(["tier0"]) == 0
    argv, env = st["calls"][0]
    assert argv == ["simai-tier0"] and env["SIMAI_ROOT"] == str(ct)
    assert (ct / "weights" / "yolo" / "yolo26x-pose.pt").read_bytes() == WEIGHTS
    assert not (ws / "simai").exists()
    assert _records(ct)[0]["storage"]["source"] == "container"


def test_free_space_floor_refuses(bs, tmp_path, monkeypatch):
    st = _setup(bs, tmp_path, monkeypatch)
    monkeypatch.setenv("SIMAI_MIN_FREE_GB", "1e9")
    assert bs.main(["tier0"]) == bs.EXIT_STORAGE and st["calls"] == [] and not st["target"].exists()


# ---------------------------------------------------------------- GPU


@pytest.mark.parametrize("env, why", [
    ({"FAKE_SMI_FAIL": "1"}, "GPU 없음"),
    ({"FAKE_SMI_CC": "7.5"}, "Ampere"),  # T4·RTX 20 계열
    ({"FAKE_SMI_TOTAL_MB": "16384"}, "24GB"),
    ({"FAKE_SMI_CC": "[N/A]"}, "사양을 모르면"),  # 모르면 통과로 보지 않는다
])
def test_no_gpu_or_below_minimum_exits_3_before_downloading(bs, tmp_path, monkeypatch, capsys, env, why):
    st = _setup(bs, tmp_path, monkeypatch)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(bs, "urlopen", _no_network)
    assert bs.main(["tier0"]) == bs.EXIT_GPU
    assert st["calls"] == [] and not st["target"].exists()
    assert why in capsys.readouterr().err
    assert _records(st["root"])[0]["exit"] == bs.EXIT_GPU


def test_missing_nvidia_smi_binary_exits_3(bs, tmp_path, monkeypatch):
    st = _setup(bs, tmp_path, monkeypatch)
    monkeypatch.setenv("SIMAI_NVIDIA_SMI", (tmp_path / "no-such-nvidia-smi").as_posix())
    assert bs.main(["tier0"]) == bs.EXIT_GPU and st["calls"] == []


def test_gpu_arch_is_recorded_and_exported(bs, tmp_path, monkeypatch):
    st = _setup(bs, tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_SMI_CC", "8.9")
    monkeypatch.setenv("FAKE_SMI_TOTAL_MB", "24564")  # RTX 4090: Tier 0 전용 최소 사양
    assert bs.main(["tier0", "--config", "x.json"]) == 0
    argv, env = st["calls"][0]
    assert argv == ["simai-tier0", "--config", "x.json"] and env["SIMAI_GPU_ARCH"] == "sm_89"
    assert _records(st["root"])[0]["gpu"]["arch"] == "sm_89"


# ---------------------------------------------------------------- 가중치


def test_missing_weights_are_downloaded_verified_and_exported(bs, tmp_path, monkeypatch):
    st = _setup(bs, tmp_path, monkeypatch)
    assert bs.main(["tier0"]) == 0
    _argv, env = st["calls"][0]
    want = hashlib.sha256(WEIGHTS).hexdigest()
    assert st["target"].read_bytes() == WEIGHTS
    assert env["SIMAI_TIER0_WEIGHTS"] == str(st["target"]) and env["SIMAI_TIER0_WEIGHTS_SHA256"] == want
    assert not list(st["target"].parent.glob("*.part-*"))
    assert _records(st["root"])[0]["weights"][0]["status"] == "downloaded"


def test_present_weights_with_matching_hash_are_reused_without_download(bs, tmp_path, monkeypatch):
    st = _setup(bs, tmp_path, monkeypatch)
    st["target"].parent.mkdir(parents=True)
    st["target"].write_bytes(WEIGHTS)
    monkeypatch.setattr(bs, "urlopen", _no_network)  # 엘리스 블록 스토리지 재사용: 받지 않는다
    assert bs.main(["tier0"]) == 0 and len(st["calls"]) == 1
    assert _records(st["root"])[0]["weights"][0]["status"] == "present"


def test_present_weights_with_wrong_hash_refuse_and_are_not_deleted(bs, tmp_path, monkeypatch, capsys):
    st = _setup(bs, tmp_path, monkeypatch)
    st["target"].parent.mkdir(parents=True)
    st["target"].write_bytes(b"tampered")
    monkeypatch.setattr(bs, "urlopen", _no_network)
    assert bs.main(["tier0"]) == bs.EXIT_STORAGE and st["calls"] == []
    assert st["target"].read_bytes() == b"tampered"  # 덮어쓰거나 지우지 않는다
    assert "해시 불일치" in capsys.readouterr().err


def test_downloaded_file_with_wrong_hash_is_discarded(bs, tmp_path, monkeypatch):
    st = _setup(bs, tmp_path, monkeypatch, sha="0" * 64)
    assert bs.main(["tier0"]) == bs.EXIT_STORAGE and st["calls"] == []
    assert not st["target"].exists() and not list(st["target"].parent.glob("*.part-*"))


def test_manifest_without_hash_refuses_instead_of_trusting_the_download(bs, tmp_path, monkeypatch):
    st = _setup(bs, tmp_path, monkeypatch, sha="")
    monkeypatch.setattr(bs, "urlopen", _no_network)
    assert bs.main(["tier0"]) == bs.EXIT_STORAGE and st["calls"] == [] and not st["target"].exists()


def test_repo_manifest_is_valid_and_pins_tier0_weights(bs):
    entries = bs.load_manifest(bs.DEFAULT_MANIFEST, "tier0")
    assert [e["name"] for e in entries] == ["yolo26x-pose"]
    assert len(entries[0]["sha256"]) == 64 and entries[0]["license"] and entries[0]["source"]["url"].startswith("https://")


# ---------------------------------------------------------------- HF 토큰


def test_hf_token_goes_only_to_first_request_and_never_to_logs_records_or_children(bs, tmp_path, monkeypatch, capsys):
    st = _setup(bs, tmp_path, monkeypatch, source={"hf": {"repo": "org/model", "file": "w.pt"}})
    monkeypatch.setenv("HF_TOKEN", TOKEN)
    seen = []

    def fake_open(req):
        seen.append(req)
        return io.BytesIO(WEIGHTS)

    monkeypatch.setattr(bs, "urlopen", fake_open)
    assert bs.main(["tier0"]) == 0
    req = seen[0]
    assert req.full_url == "https://huggingface.co/org/model/resolve/main/w.pt"
    assert req.unredirected_hdrs.get("Authorization") == f"Bearer {TOKEN}"
    assert "Authorization" not in req.headers  # 리다이렉트(다른 호스트 CDN)에는 실리지 않는다
    _argv, env = st["calls"][0]
    assert "HF_TOKEN" not in env  # Tier 0·큐에 넘기지 않는다
    out = capsys.readouterr()
    rec_text = "".join(p.read_text(encoding="utf-8") for p in (st["root"] / "runs" / "bootstrap").glob("*.json"))
    assert TOKEN not in out.out + out.err + rec_text
    assert json.loads(rec_text)["hf_token"] == "present"


def test_hf_token_is_redacted_from_download_errors(bs, tmp_path, monkeypatch, capsys):
    st = _setup(bs, tmp_path, monkeypatch, source={"hf": {"repo": "org/model", "file": "w.pt"}})
    monkeypatch.setenv("HF_TOKEN", TOKEN)

    def leaky(req):
        raise RuntimeError(f"401 for header Bearer {TOKEN}")

    monkeypatch.setattr(bs, "urlopen", leaky)
    assert bs.main(["tier0"]) == bs.EXIT_STORAGE and st["calls"] == []
    out = capsys.readouterr()
    rec = _records(st["root"])[0]
    assert TOKEN not in out.out + out.err + json.dumps(rec)
    assert "받기 실패" in rec["error"]


# ---------------------------------------------------------------- 실행 모드


def test_other_commands_pass_through_without_preparation(bs, tmp_path, monkeypatch):
    st = _setup(bs, tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_SMI_FAIL", "1")  # GPU 가 없어도 sleep infinity·셸은 그대로 뜬다
    assert bs.main(["sleep", "infinity"]) == 0
    assert st["calls"][0][0] == ["sleep", "infinity"] and not st["root"].exists()
    assert bs.main(["--help"]) == 0 and st["calls"][1][0] == ["simai-tier0", "--help"]


def test_no_args_shows_usage_and_starts_nothing(bs, tmp_path, monkeypatch, capsys):
    """S15P31A101-414: 10차 세션에서 SSH 셸의 맨 simai-bootstrap 이 예전 교실 기본값으로 Tier 0 를 띄웠다. 인자 없으면 사용법만(64)."""
    st = _setup(bs, tmp_path, monkeypatch)
    monkeypatch.setattr(bs, "urlopen", _no_network)
    assert bs.main([]) == bs.EXIT_USAGE
    err = capsys.readouterr().err
    assert st["calls"] == [] and not st["root"].exists()
    assert "simai-bootstrap tier0" in err and "simai-bootstrap lobby" in err
    # 컨테이너 시작 경로(이미지 CMD tier0, RunPod 시작 명령 sleep infinity)는 그대로다
    assert bs.main(["sleep", "infinity"]) == 0 and st["calls"][0][0] == ["sleep", "infinity"]


def test_queue_mode_needs_repo_copy_and_runs_session_queue(bs, tmp_path, monkeypatch):
    st = _setup(bs, tmp_path, monkeypatch)
    assert bs.main(["queue"]) == bs.EXIT_STORAGE and st["calls"] == []  # 사본이 없으면 시작하지 않는다
    script = st["root"] / "setup" / "session1" / "repo" / "deploy" / "scripts" / "gpu_session1.py"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    assert bs.main(["queue", "--session", "s1"]) == 0
    assert st["calls"][0][0] == [sys.executable, str(script), "run", "--session", "s1"]


# ---------------------------------------------------------------- SSH (simai-start)


def _start(gs, tmp_path: Path, env: dict) -> tuple[subprocess.CompletedProcess, Path]:
    fake = tmp_path / "fakebin"
    fake.mkdir(parents=True, exist_ok=True)
    # 가짜 ssh-keygen·sshd 는 자기 환경에 HF 토큰이 있는지 FAKE_SSHD_MARK.env 에 남긴다(리뷰 R1)
    # 터널 비밀값(-286)도 같은 방식으로 본다
    seen = ('echo "$0 ${HF_TOKEN:+TOKEN}${HUGGING_FACE_HUB_TOKEN:+TOKEN}'
            '${SIMAI_TUNNEL_PREFIX:+TUNNEL}${WSTUNNEL_RESTRICT_HTTP_UPGRADE_PATH_PREFIX:+TUNNEL}" >> "$FAKE_SSHD_MARK.env"')
    # 가짜 wstunnel 은 받은 인자와 환경(경로 비밀값·HF 토큰)을 FAKE_SSHD_MARK.wst 에 다 쓴 뒤 옮긴다(백그라운드라 시험이 기다린다)
    wst = ('{ printf "%s\\n" "$*"; echo "prefix=${WSTUNNEL_RESTRICT_HTTP_UPGRADE_PATH_PREFIX-}"; '
           'echo "other=${HF_TOKEN:+TOKEN}${SIMAI_TUNNEL_PREFIX:+TUNNEL}"; } > "$FAKE_SSHD_MARK.wst.tmp"\n'
           'mv "$FAKE_SSHD_MARK.wst.tmp" "$FAKE_SSHD_MARK.wst"')
    for name, body in (("ssh-keygen", seen), ("sshd", seen + '\necho started >> "$FAKE_SSHD_MARK"'), ("wstunnel", wst)):
        (fake / name).write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8", newline="\n")
        (fake / name).chmod(0o755)
    ssh = tmp_path / "ssh"
    drop = ("PUBLIC_KEY", "SSH_PUBLIC_KEY", "SIMAI_AUTHORIZED_KEYS_FILE", "SIMAI_SSHD", "SIMAI_TUNNEL_PREFIX", "SIMAI_TUNNEL_PORT",
            "SIMAI_TUNNEL_ALLOW")
    base = {k: v for k, v in os.environ.items() if k not in drop}
    full = {**base, "PATH": str(fake) + os.pathsep + base.get("PATH", ""), "SIMAI_SSH_DIR": ssh.as_posix(),
            "SIMAI_SSHD_CONFIG_DIR": (tmp_path / "sshd_config.d").as_posix(), "SIMAI_SSHD_BIN": (fake / "sshd").as_posix(), "SIMAI_SSH_KEYGEN_BIN": (fake / "ssh-keygen").as_posix(),
            "SIMAI_SSH_ENV_FILE": (tmp_path / "profile.d" / "simai-env.sh").as_posix(), "SIMAI_WSTUNNEL_BIN": (fake / "wstunnel").as_posix(),
            "FAKE_SSHD_MARK": (tmp_path / "sshd.mark").as_posix(), **env}
    bash = gs.find_bash()
    # 넘겨받은 명령(simai-bootstrap)은 HF 토큰을 받아야 하고, 터널 비밀값은 받으면 안 된다
    cmd = ('echo ran-cmd; echo "next-token=${HF_TOKEN:+yes}"; '
           'echo "next-tunnel=${SIMAI_TUNNEL_PREFIX:+yes}${WSTUNNEL_RESTRICT_HTTP_UPGRADE_PATH_PREFIX:+yes}"')
    r = subprocess.run([bash, (REPO / "deploy" / "base" / "simai-start.sh").as_posix(), "bash", "-c", cmd],
                       env=full, capture_output=True, text=True, encoding="utf-8", timeout=60)
    return r, ssh / "authorized_keys"


KEY_A = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyA runpod"
KEY_B = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyB vast"


@pytest.mark.parametrize("env, want", [
    ({"PUBLIC_KEY": KEY_A}, [KEY_A]),  # RunPod
    ({"SSH_PUBLIC_KEY": KEY_B}, [KEY_B]),  # Vast.ai (Entrypoint 실행 방식)
])
def test_ssh_key_sources_start_key_only_sshd(gs, tmp_path, env, want):
    r, keys = _start(gs, tmp_path, env)
    assert r.returncode == 0 and "ran-cmd" in r.stdout, r.stderr
    assert keys.read_text(encoding="utf-8").split("\n")[:-1] == want
    conf = (tmp_path / "sshd_config.d" / "00-simai.conf").read_text(encoding="utf-8")
    assert "PasswordAuthentication no" in conf and "KbdInteractiveAuthentication no" in conf
    assert (tmp_path / "sshd.mark").read_text(encoding="utf-8").strip() == "started"


def test_ssh_keys_file_merges_with_env_without_duplicates_or_cr(gs, tmp_path):
    f = tmp_path / "keys.txt"
    f.write_bytes(f"{KEY_A}\r\n\r\n{KEY_B}\r\n".encode())  # Windows 에서 만든 파일
    r, keys = _start(gs, tmp_path, {"PUBLIC_KEY": KEY_A, "SIMAI_AUTHORIZED_KEYS_FILE": f.as_posix()})
    assert r.returncode == 0, r.stderr
    assert keys.read_bytes().decode().split("\n")[:-1] == [KEY_A, KEY_B]


def test_sshd_off_or_no_keys_starts_no_sshd_but_runs_command(gs, tmp_path):
    r, keys = _start(gs, tmp_path, {"PUBLIC_KEY": KEY_A, "SIMAI_SSHD": "off"})  # 엘리스 VM: 호스트 sshd
    assert r.returncode == 0 and "ran-cmd" in r.stdout and not keys.exists() and not (tmp_path / "sshd.mark").exists()
    r, keys = _start(gs, tmp_path, {"SIMAI_AUTHORIZED_KEYS_FILE": (tmp_path / "missing").as_posix()})
    assert r.returncode == 0 and "읽을 수 없다" in r.stderr and not keys.exists() and not (tmp_path / "sshd.mark").exists()


# ---------------------------------------------------------------- 줄바꿈


def test_scripts_copied_into_images_are_lf():
    files = [REPO / "deploy" / "base" / "simai-start.sh", REPO / "deploy" / "tier0" / "entrypoint.sh",
             *(REPO / "deploy" / "scripts").glob("*.py"), *(REPO / "deploy" / "scripts").glob("*.sh")]
    assert REPO / "deploy" / "scripts" / "wait_tier0_ready.py" in files
    crlf = [p.name for p in files if b"\r" in p.read_bytes()]
    assert crlf == []
    r = subprocess.run(["git", "-C", str(REPO), "check-attr", "eol", "--", "deploy/scripts/wait_tier0_ready.py"],
                       capture_output=True, text=True)
    if r.returncode == 0:  # 저장소 사본(.git 없음)에서는 건너뛴다
        assert r.stdout.strip().endswith("eol: lf")


# ---------------------------------------------------------------- 리뷰 수정 (R1 토큰 상속, SSH 입력 검증, R2 첫 업로드)


def test_ssh_helpers_get_no_hf_token_but_bootstrap_does(gs, tmp_path):
    r, keys = _start(gs, tmp_path, {"PUBLIC_KEY": KEY_A, "HF_TOKEN": TOKEN, "HUGGING_FACE_HUB_TOKEN": TOKEN})
    assert r.returncode == 0, r.stderr
    seen = (tmp_path / "sshd.mark.env").read_text(encoding="utf-8").split("\n")[:-1]
    assert len(seen) == 2 and all(not line.endswith("TOKEN") for line in seen), seen  # ssh-keygen·sshd
    assert "next-token=yes" in r.stdout  # 가중치를 받을 simai-bootstrap 에는 넘긴다


def test_simai_start_drops_hf_token_before_any_helper_command():
    """2차 리뷰 재현: cat·tr·awk·mkdir·chmod·wc·ssh-keygen·sshd 어느 것도 토큰을 받지 않는다. 지우기는 맨 앞, 다시 내보내기는 exec 바로 앞뿐이다."""
    lines = (REPO / "deploy" / "base" / "simai-start.sh").read_text(encoding="utf-8").splitlines()
    code = [(i, ln) for i, ln in enumerate(lines) if ln.strip() and not ln.lstrip().startswith("#")]
    unset_at = next(i for i, ln in code if ln.strip() == "unset HF_TOKEN HUGGING_FACE_HUB_TOKEN")
    unset_tunnel_at = next(i for i, ln in code if ln.strip() == "unset SIMAI_TUNNEL_PREFIX")  # -286 터널 비밀값도 같다
    helpers = ("cat ", "tr ", "awk", "mkdir", "chmod", "wc ", '"$KEYGEN"', '"$SSHD"', '"$WSTUNNEL"')
    first_helper = min(i for i, ln in code if any(h in ln for h in helpers))
    assert unset_at < first_helper and unset_tunnel_at < first_helper
    exports = [i for i, ln in code if "export HF_TOKEN" in ln]
    exec_at = max(i for i, ln in code if ln.strip() == 'exec "$@"')
    assert len(exports) == 1 and [i for i, _ in code if exports[0] < i] == [exec_at]


def test_ssh_key_lines_with_options_or_control_chars_are_dropped(gs, tmp_path):
    bad = [f'command="echo injected" {KEY_B}', f'from="1.2.3.4" {KEY_B}', KEY_B + "\x1b[2J", "not-a-key AAAA", KEY_B + "\tx"]
    r, keys = _start(gs, tmp_path, {"SSH_PUBLIC_KEY": "\n".join([KEY_A, *bad])})
    assert r.returncode == 0, r.stderr
    assert keys.read_text(encoding="utf-8").split("\n")[:-1] == [KEY_A]
    assert "5개를 버렸다" in r.stderr
    # 옵션 줄만 있으면 키가 없으니 sshd 를 띄우지 않는다
    r, keys2 = _start(gs, tmp_path / "only-bad", {"SSH_PUBLIC_KEY": bad[0]})
    assert r.returncode == 0 and not keys2.exists() and not (tmp_path / "only-bad" / "sshd.mark").exists()


def test_no_child_process_inherits_hf_token(bs, tmp_path, monkeypatch):
    """리뷰 재현: nvidia-smi 조회·도움말·그대로 넘기는 명령·tier0·queue·check 어느 자식에도 토큰이 없어야 한다."""
    real_execvp = bs.execvp  # _setup 이 가로채는 exec 를 되돌려 실제 os.execvpe 호출 인자를 본다
    st = _setup(bs, tmp_path, monkeypatch)
    monkeypatch.setattr(bs, "execvp", real_execvp)
    log = tmp_path / "smi-env.log"
    smi = tmp_path / "smi_env.py"
    smi.write_text(
        "import os, sys\n"
        f"open(r'{log}', 'a').write(('TOKEN' if {{'HF_TOKEN', 'HUGGING_FACE_HUB_TOKEN'}} & set(os.environ) else 'CLEAN') + '\\n')\n"
        "print('NVIDIA H100 80GB HBM3, 9.0, 81559, 580.65.06')\n", encoding="utf-8")
    monkeypatch.setenv("SIMAI_NVIDIA_SMI", f"{PY} {smi.as_posix()}")
    script = st["root"] / "setup" / "session1" / "repo" / "deploy" / "scripts" / "gpu_session1.py"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    execs = []
    monkeypatch.setattr(bs.os, "execvpe", lambda file, argv, env: execs.append((list(argv), dict(env))))
    # take_token 전에 detect_gpu 를 바로 불러도 새지 않는다(리뷰 재현 그대로)
    monkeypatch.setenv("HF_TOKEN", TOKEN)
    assert bs.detect_gpu([PY, smi.as_posix()])["arch"] == "sm_90"
    for argv in (["tier0"], ["tier0", "--config", "x"], ["check"], ["queue"], ["sleep", "infinity"], ["--help"]):
        monkeypatch.setenv("HF_TOKEN", TOKEN)
        monkeypatch.setenv("HUGGING_FACE_HUB_TOKEN", TOKEN)
        bs.main(argv)
        assert "HF_TOKEN" not in os.environ and "HUGGING_FACE_HUB_TOKEN" not in os.environ
    lines = log.read_text(encoding="utf-8").split()
    assert lines and set(lines) == {"CLEAN"}, lines  # 조회 5번(직접 1 + tier0·tier0·check·queue)
    assert len(execs) == 5  # tier0·tier0·queue·sleep·help (check 는 exec 하지 않음)
    for argv, env in execs:
        assert not {"HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"} & set(env), argv
        assert TOKEN not in json.dumps(env)


def test_prepare_upload_works_on_a_fresh_disk(gs, tmp_path, capsys, monkeypatch):
    """리뷰 R2: 새 디스크(볼륨 없음)에서 prepare 안내를 순서대로 따르면 업로드·풀기가 된다(ssh·scp 는 로컬 대응으로)."""
    import shutil
    import tarfile

    monkeypatch.setenv("MSYS_NO_PATHCONV", "1")  # 로컬 폴더로 Pod 를 흉내 낸다: Git Bash 경로 변환이 아님을 밝힌다(-213)
    pod_simai = (tmp_path / "fresh" / "simai").as_posix()
    assert gs.main(["prepare", "--out", str(tmp_path / "prep"), "--pod-simai", pod_simai]) == 0
    lines = [ln.strip() for ln in capsys.readouterr().out.splitlines() if ln.strip().startswith(("ssh ", "scp "))]
    up = f"{pod_simai}/setup/session1"
    mk = next(i for i, ln in enumerate(lines) if f"mkdir -p {up}'" in ln)
    cp = next(i for i, ln in enumerate(lines) if ln.startswith("scp "))
    assert mk < cp and lines[cp].endswith(f":{up}/")  # scp 보다 먼저 업로드 폴더를 만든다
    Path(up).mkdir(parents=True)  # 안내 1: ssh mkdir -p
    for f in ("simai-repo.tar", "git-stamp.json"):  # 안내 2: scp
        shutil.copy(tmp_path / "prep" / f, Path(up) / f)
    dest = Path(up) / "repo"  # 안내 3: 풀기
    dest.mkdir()
    with tarfile.open(Path(up) / "simai-repo.tar") as t:
        # deploy/ 만 푼다(긴 --basetemp 에서 Windows 260자 한도에 걸리는 깊은 시험 자료 경로를 피한다)
        t.extractall(dest, members=[m for m in t.getmembers() if m.name.startswith("deploy/")], filter="data")
    # 안내 4: simai-bootstrap queue 의 기본 사본 위치($SIMAI_ROOT/setup/session1/repo)에 큐 스크립트가 있다
    assert (Path(pod_simai) / "setup" / "session1" / "repo" / "deploy" / "scripts" / "gpu_session1.py").is_file()


# ---------------------------------------------------------------- 443 예비 터널·SSH 세션 환경 (S15P31A101-286)

PREFIX = "Tun3lPrefix_" + "x9-Q" * 8  # 44자, 허용 문자만
WST_ARGS = "server --log-lvl OFF --restrict-to 127.0.0.1:22 --restrict-to localhost:22 ws://0.0.0.0:{port}"


def _wst(tmp_path: Path, wait_s: float = 10.0) -> list[str] | None:
    """백그라운드 가짜 wstunnel 이 남긴 [인자, prefix=, other=] 줄. wait_s 안에 없으면 None."""
    import time

    mark = tmp_path / "sshd.mark.wst"
    end = time.monotonic() + wait_s
    while not mark.exists() and time.monotonic() < end:
        time.sleep(0.05)
    return mark.read_text(encoding="utf-8").split("\n")[:-1] if mark.exists() else None


@pytest.mark.parametrize("port_env, port", [({}, "8080"), ({"SIMAI_TUNNEL_PORT": "18080"}, "18080")])
def test_tunnel_starts_after_sshd_and_prefix_reaches_only_wstunnel_env(gs, tmp_path, port_env, port):
    r, _ = _start(gs, tmp_path, {"PUBLIC_KEY": KEY_A, "SIMAI_TUNNEL_PREFIX": PREFIX, "HF_TOKEN": TOKEN, **port_env})
    assert r.returncode == 0 and "ran-cmd" in r.stdout, r.stderr
    assert _wst(tmp_path) == [WST_ARGS.format(port=port), f"prefix={PREFIX}", "other="]  # 명령줄(ps)에는 비밀값이 없다
    assert "wstunnel 시작" in r.stderr
    # 출력·넘겨받은 명령·sshd·ssh-keygen·SSH 세션 환경 파일 어디에도 비밀값이 없다
    assert PREFIX not in r.stdout + r.stderr and "next-tunnel=\n" in r.stdout and "next-token=yes" in r.stdout
    seen = (tmp_path / "sshd.mark.env").read_text(encoding="utf-8").split("\n")[:-1]
    assert len(seen) == 2 and all(not line.endswith(("TOKEN", "TUNNEL")) for line in seen), seen
    env_file = (tmp_path / "profile.d" / "simai-env.sh").read_text(encoding="utf-8")
    assert PREFIX not in env_file and TOKEN not in env_file


@pytest.mark.parametrize("env, why", [
    ({}, "SIMAI_TUNNEL_PREFIX 없음"),
    ({"SIMAI_TUNNEL_PREFIX": ""}, "SIMAI_TUNNEL_PREFIX 없음"),
    ({"SIMAI_TUNNEL_PREFIX": "short-secret"}, "약하거나"),
    ({"SIMAI_TUNNEL_PREFIX": "a" * 31}, "약하거나"),  # 32자 미만
    ({"SIMAI_TUNNEL_PREFIX": "a" * 129}, "약하거나"),
    ({"SIMAI_TUNNEL_PREFIX": "a" * 40 + "/../x"}, "약하거나"),  # 경로 문자
    ({"SIMAI_TUNNEL_PREFIX": "a" * 40 + " b"}, "약하거나"),
    ({"SIMAI_TUNNEL_PREFIX": PREFIX, "SIMAI_TUNNEL_PORT": "0"}, "포트 번호가 아니라"),
    ({"SIMAI_TUNNEL_PREFIX": PREFIX, "SIMAI_TUNNEL_PORT": "70000"}, "포트 번호가 아니라"),
    ({"SIMAI_TUNNEL_PREFIX": PREFIX, "SIMAI_TUNNEL_PORT": "80a"}, "포트 번호가 아니라"),
])
def test_missing_weak_prefix_or_bad_port_starts_no_tunnel_but_sshd_and_command_run(gs, tmp_path, env, why):
    r, keys = _start(gs, tmp_path, {"PUBLIC_KEY": KEY_A, **env})
    assert r.returncode == 0 and "ran-cmd" in r.stdout and keys.exists(), r.stderr
    assert why in r.stderr and "wstunnel 시작" not in r.stderr
    assert _wst(tmp_path, wait_s=0.3) is None
    prefix = env.get("SIMAI_TUNNEL_PREFIX")
    if prefix:  # 약한 비밀값도 출력하지 않는다
        assert prefix not in r.stdout + r.stderr


@pytest.mark.parametrize("env", [
    {"PUBLIC_KEY": KEY_A, "SIMAI_SSHD": "off"},  # 엘리스 VM: 호스트 sshd
    {},  # 공개키 없음 → sshd 없음
])
def test_no_sshd_means_no_tunnel_even_with_prefix(gs, tmp_path, env):
    r, keys = _start(gs, tmp_path, {**env, "SIMAI_TUNNEL_PREFIX": PREFIX})
    assert r.returncode == 0 and "ran-cmd" in r.stdout and not keys.exists(), r.stderr
    assert "sshd 를 띄우지 않아 443 예비 터널" in r.stderr and _wst(tmp_path, wait_s=0.3) is None
    assert PREFIX not in r.stdout + r.stderr and "next-tunnel=\n" in r.stdout
    assert not (tmp_path / "profile.d" / "simai-env.sh").exists()


def test_ssh_session_env_file_keeps_non_secret_names_only(gs, tmp_path):
    quoted = "it's $HOME `x` \"q\""  # 작은따옴표·확장 문자가 그대로 돌아와야 한다
    env = {"PUBLIC_KEY": KEY_A, "SIMAI_TUNNEL_PREFIX": PREFIX, "HF_TOKEN": TOKEN, "HUGGING_FACE_HUB_TOKEN": TOKEN,
           "SIMAI_IMAGE_DIGEST": "sha256:abc", "SIMAI_ODD": quoted, "CUDA_VERSION": "13.0.3", "RUNPOD_POD_ID": "pod123",
           "RUNPOD_API_KEY": "rpa_secret_value", "SIMAI_WORKER_TOKEN": "worker-secret", "SIMAI_DB_PASSWORD": "pw-secret",
           "UNRELATED_NAME": "not-copied"}
    r, _ = _start(gs, tmp_path, env)
    assert r.returncode == 0, r.stderr
    f = tmp_path / "profile.d" / "simai-env.sh"
    text = f.read_text(encoding="utf-8")
    for secret in (PREFIX, TOKEN, "rpa_secret_value", "worker-secret", "pw-secret", KEY_A):
        assert secret not in text
    assert "UNRELATED_NAME" not in text and "PUBLIC_KEY" not in text
    probe = 'set -e; . "$1"; printf "%s|%s|%s|%s" "$SIMAI_IMAGE_DIGEST" "$CUDA_VERSION" "$RUNPOD_POD_ID" "$SIMAI_ODD"'
    out = subprocess.run([gs.find_bash(), "-c", probe, "probe", f.as_posix()], capture_output=True, text=True, encoding="utf-8",
                         env={"PATH": os.environ.get("PATH", "")}, timeout=30)
    assert out.returncode == 0 and out.stdout == f"sha256:abc|13.0.3|pod123|{quoted}", out.stderr


# ---------------------------------------------------------------- 터널 허용 대상 (S15P31A101-288)


@pytest.mark.parametrize("allow, extra", [
    ("17000,17001", ["17000", "17001"]),
    (" 17000 , 22,17000,,17001 ", ["17000", "17001"]),  # 공백·빈 항목·22·같은 포트는 한 번
    ("22", []),
    ("", []),
])
def test_tunnel_allow_adds_loopback_ports_only(gs, tmp_path, allow, extra):
    r, _ = _start(gs, tmp_path, {"PUBLIC_KEY": KEY_A, "SIMAI_TUNNEL_PREFIX": PREFIX, "SIMAI_TUNNEL_ALLOW": allow})
    assert r.returncode == 0 and "ran-cmd" in r.stdout, r.stderr
    want = WST_ARGS.format(port="8080").replace(
        " ws://", "".join(f" --restrict-to 127.0.0.1:{p}" for p in extra) + " ws://")
    assert _wst(tmp_path) == [want, f"prefix={PREFIX}", "other="]
    assert "→ " + ",".join(["127.0.0.1:22", *(f"127.0.0.1:{p}" for p in extra)]) + " 만" in r.stderr
    assert "포트 목록이 아니다" not in r.stderr and PREFIX not in r.stdout + r.stderr


@pytest.mark.parametrize("allow, port_env", [
    ("17000,abc", {}),
    ("17000,0", {}),
    ("70000", {}),
    ("017000", {}),  # 앞자리 0
    ("17000;17001", {}),
    ("0.0.0.0:17000", {}),  # 주소는 받지 않는다(127.0.0.1 로만 잇는다)
    ("8080", {}),  # 터널 포트 자신
    ("18080", {"SIMAI_TUNNEL_PORT": "18080"}),
    (",".join(str(17000 + i) for i in range(17)), {}),  # 16개 초과
])
def test_bad_tunnel_allow_falls_back_to_ssh_only(gs, tmp_path, allow, port_env):
    r, _ = _start(gs, tmp_path, {"PUBLIC_KEY": KEY_A, "SIMAI_TUNNEL_PREFIX": PREFIX, "SIMAI_TUNNEL_ALLOW": allow, **port_env})
    assert r.returncode == 0 and "ran-cmd" in r.stdout, r.stderr
    assert "포트 목록이 아니다" in r.stderr and "→ 127.0.0.1:22 만" in r.stderr
    port = port_env.get("SIMAI_TUNNEL_PORT", "8080")
    assert _wst(tmp_path) == [WST_ARGS.format(port=port), f"prefix={PREFIX}", "other="]


def test_tunnel_allow_without_prefix_or_sshd_opens_nothing(gs, tmp_path):
    r, _ = _start(gs, tmp_path, {"PUBLIC_KEY": KEY_A, "SIMAI_TUNNEL_ALLOW": "17000"})
    assert r.returncode == 0 and "SIMAI_TUNNEL_PREFIX 없음" in r.stderr and _wst(tmp_path, wait_s=0.3) is None
    r, _ = _start(gs, tmp_path / "nosshd", {"SIMAI_TUNNEL_PREFIX": PREFIX, "SIMAI_TUNNEL_ALLOW": "17000"})
    assert r.returncode == 0 and _wst(tmp_path / "nosshd", wait_s=0.3) is None


# ---------------------------------------------------------------- 이미지에 구운 시연 가중치 (S15P31A101-577)


def _bake(tmp_path: Path, monkeypatch, rel: str = "weights/yolo/yolo26x-pose.pt", data: bytes = WEIGHTS,
          tag: str | None = None) -> Path:
    """구운 사본(<baked>/<path> + .sha256)을 만들고 SIMAI_BAKED_WEIGHTS 로 가리킨다."""
    baked = tmp_path / "baked" / rel
    baked.parent.mkdir(parents=True, exist_ok=True)
    baked.write_bytes(data)
    baked.with_name(baked.name + ".sha256").write_text((tag or hashlib.sha256(data).hexdigest()) + "\n", encoding="utf-8")
    monkeypatch.setenv("SIMAI_BAKED_WEIGHTS", str(tmp_path / "baked"))
    return baked


def _hashed(bs, monkeypatch) -> list[Path]:
    """sha256_file 이 해시한 파일 목록(실제 해시는 그대로)."""
    seen: list[Path] = []
    real = bs.sha256_file
    monkeypatch.setattr(bs, "sha256_file", lambda p: (seen.append(Path(p)), real(p))[1])
    return seen


def test_baked_copy_is_verified_and_linked_instead_of_downloading(bs, tmp_path, monkeypatch, capsys):
    st = _setup(bs, tmp_path, monkeypatch)
    baked = _bake(tmp_path, monkeypatch)
    hashed = _hashed(bs, monkeypatch)
    monkeypatch.setattr(bs, "urlopen", _no_network)  # 새 Pod 가 HF·GitHub 에 가지 않는다
    assert bs.main(["tier0"]) == 0
    _argv, env = st["calls"][0]
    assert st["target"].read_bytes() == WEIGHTS and hashed == [baked]  # 사본을 한 번 해시해 확인했다
    assert env["SIMAI_TIER0_WEIGHTS"] == str(st["target"])  # Tier 0 는 늘 $SIMAI_ROOT 경로를 본다
    if st["target"].is_symlink():  # 링크를 못 거는 Windows 시험 환경은 복사
        assert st["target"].resolve() == baked.resolve()
    assert _records(st["root"])[0]["weights"][0]["status"] == "baked"
    assert "yolo26x-pose baked" in capsys.readouterr().err
    # 다음 시작: 이미 있는 파일 규칙(Tier 0 가중치는 매번 해시)
    assert bs.main(["tier0"]) == 0 and _records(st["root"])[1]["weights"][0]["status"] == "present"


def test_baked_copy_of_another_version_is_skipped_without_hashing_and_downloaded(bs, tmp_path, monkeypatch):
    st = _setup(bs, tmp_path, monkeypatch)
    baked = _bake(tmp_path, monkeypatch, data=b"older-weights", tag="1" * 64)  # 목록이 바뀐 뒤의 옛 이미지
    hashed = _hashed(bs, monkeypatch)
    assert bs.main(["tier0"]) == 0
    assert baked not in hashed  # 옆 .sha256 이 목록과 다르면 수 GB 를 해시하지 않는다
    assert st["target"].read_bytes() == WEIGHTS and not st["target"].is_symlink()
    assert _records(st["root"])[0]["weights"][0]["status"] == "downloaded"


def test_corrupt_baked_copy_falls_back_to_download_and_is_left_alone(bs, tmp_path, monkeypatch, capsys):
    st = _setup(bs, tmp_path, monkeypatch)
    baked = _bake(tmp_path, monkeypatch, data=b"bit-rot", tag=hashlib.sha256(WEIGHTS).hexdigest())  # 표는 맞는데 내용이 다르다
    assert bs.main(["tier0"]) == 0
    assert st["target"].read_bytes() == WEIGHTS and baked.read_bytes() == b"bit-rot"
    assert _records(st["root"])[0]["weights"][0]["status"] == "downloaded"
    assert "구운 사본 해시 불일치" in capsys.readouterr().err


def test_without_baked_copy_downloads_as_before(bs, tmp_path, monkeypatch):
    st = _setup(bs, tmp_path, monkeypatch)
    (tmp_path / "baked").mkdir()
    monkeypatch.setenv("SIMAI_BAKED_WEIGHTS", str(tmp_path / "baked"))
    assert bs.main(["tier0"]) == 0 and _records(st["root"])[0]["weights"][0]["status"] == "downloaded"


def test_existing_target_keeps_its_rules_even_with_a_baked_copy(bs, tmp_path, monkeypatch):
    st = _setup(bs, tmp_path, monkeypatch)
    baked = _bake(tmp_path, monkeypatch)
    hashed = _hashed(bs, monkeypatch)
    monkeypatch.setattr(bs, "urlopen", _no_network)
    st["target"].parent.mkdir(parents=True)
    st["target"].write_bytes(b"tampered")  # 볼륨에 남은 깨진 파일: 사본으로 덮지 않고 시작하지 않는다(지우지 않음)
    assert bs.main(["tier0"]) == bs.EXIT_STORAGE and st["target"].read_bytes() == b"tampered" and baked not in hashed
    st["target"].write_bytes(WEIGHTS)
    assert bs.main(["tier0"]) == 0 and baked not in hashed
    assert _records(st["root"])[-1]["weights"][0]["status"] == "present"


def test_baked_worker_weight_is_stamped_then_not_rehashed(bs, tmp_path, monkeypatch):
    """tier0 밖 항목(아바타·정적 맵): 사본 확인 뒤 검증 스탬프를 남겨 다음 시작에 수 GB 를 다시 해시하지 않는다."""
    root = tmp_path / "simai"
    data = b"fake-sam3d" * 300
    entry = {"name": "sam-3d-body-dinov3", "sets": ["avatar"], "path": "weights/sam-3d-body-dinov3/model.ckpt",
             "source": {"url": "https://example.invalid/never"}, "sha256": hashlib.sha256(data).hexdigest()}
    _bake(tmp_path, monkeypatch, entry["path"], data)
    monkeypatch.setattr(bs, "urlopen", _no_network)
    assert [r["status"] for r in bs.ensure_weights(root, [entry])] == ["baked"]
    monkeypatch.setattr(bs, "sha256_file", lambda p: (_ for _ in ()).throw(AssertionError(f"다시 해시했다: {p}")))
    assert [r["status"] for r in bs.ensure_weights(root, [entry])] == ["present-stamp"]
    assert (root / entry["path"]).read_bytes() == data


def test_baked_copy_falls_back_to_copy_when_symlinks_are_refused(bs, tmp_path, monkeypatch):
    root = tmp_path / "simai"
    entry = {"name": "y", "sets": ["tier0"], "path": "weights/yolo/yolo26x-pose.pt", "source": {"url": "https://example.invalid/x"},
             "sha256": hashlib.sha256(WEIGHTS).hexdigest()}
    baked = _bake(tmp_path, monkeypatch)
    monkeypatch.setattr(bs, "urlopen", _no_network)
    monkeypatch.setattr(bs.os, "symlink", lambda *a, **k: (_ for _ in ()).throw(OSError("no symlinks here")))
    assert [r["status"] for r in bs.ensure_weights(root, [entry])] == ["baked"]
    target = root / entry["path"]
    assert target.read_bytes() == WEIGHTS and not target.is_symlink() and baked.read_bytes() == WEIGHTS
    assert not list(target.parent.glob("*.baked-*"))  # 임시 이름이 남지 않는다


def test_dangling_link_from_an_old_image_is_replaced_by_the_new_copy(bs, tmp_path, monkeypatch):
    root = tmp_path / "simai"
    target = root / "weights" / "yolo" / "yolo26x-pose.pt"
    target.parent.mkdir(parents=True)
    try:
        os.symlink(tmp_path / "gone" / "yolo26x-pose.pt", target)  # 볼륨에 남은, 이전 이미지 사본을 가리키던 링크
    except OSError:
        pytest.skip("심볼릭 링크를 만들 수 없는 환경")
    entry = {"name": "y", "sets": ["tier0"], "path": "weights/yolo/yolo26x-pose.pt", "source": {"url": "https://example.invalid/x"},
             "sha256": hashlib.sha256(WEIGHTS).hexdigest()}
    baked = _bake(tmp_path, monkeypatch)
    monkeypatch.setattr(bs, "urlopen", _no_network)
    assert [r["status"] for r in bs.ensure_weights(root, [entry])] == ["baked"]
    assert target.resolve() == baked.resolve()


# ---------------------------------------------------------------- MapAnything 한 판만 (S15P31A101-577)


def _ma_entries() -> list[dict]:
    def e(name, path, group=True):
        return {"name": name, "sets": ["static"], "path": path, "source": {"url": "x"}, "sha256": "0" * 64,
                **({"group": "mapanything"} if group else {})}
    return [e("ma-apache-model", "weights/map-anything-apache/model.safetensors"),
            e("ma-apache-config", "weights/map-anything-apache/config.json"),
            e("ma-model", "weights/map-anything/model.safetensors"), e("ma-config", "weights/map-anything/config.json"),
            e("wm2", "weights/hy-world-2.0/HY-WorldMirror-2.0/model.safetensors", group=False),
            e("lama", "weights/lama/big-lama.pt", group=False)]


def test_pick_mapanything_keeps_the_setup_folder_variant_and_other_static_entries(bs, tmp_path, monkeypatch):
    root = tmp_path / "simai"
    monkeypatch.delenv("SIMAI_MAPANYTHING_MODEL", raising=False)

    def names(ws):
        return [w["name"] for w in ws]

    default = ["ma-model", "ma-config", "wm2", "lama"]
    apache_names = ["ma-apache-model", "ma-apache-config", "wm2", "lama"]
    assert names(bs.pick_mapanything(root, _ma_entries())) == default  # 현장 세팅 워커 기본 폴더
    apache = root / "weights" / "map-anything-apache"
    assert names(bs.pick_mapanything(root, _ma_entries(), hint=apache)) == apache_names
    assert names(bs.pick_mapanything(root, _ma_entries(), hint=root / "weights" / "moge")) == default  # 판 폴더가 아닌 --model 은 무시
    monkeypatch.setenv("SIMAI_MAPANYTHING_MODEL", str(apache))
    assert names(bs.pick_mapanything(root, _ma_entries())) == apache_names
    monkeypatch.setenv("SIMAI_MAPANYTHING_MODEL", str(tmp_path / "elsewhere"))
    with pytest.raises(bs.BootstrapError, match="MapAnything 폴더"):
        bs.pick_mapanything(root, _ma_entries())
    plain = [w for w in _ma_entries() if "group" not in w]  # 판 표시가 없는 목록은 그대로
    assert bs.pick_mapanything(root, plain) == plain
    assert bs._model_arg(["python", "run.py", "--model", str(apache)]) == apache
    assert bs._model_arg(["python", "run.py", f"--model={apache}"]) == apache and bs._model_arg(["python", "-m", "x"]) is None


def test_check_sets_and_preflight_fetch_one_mapanything_but_static_set_env_keeps_both(bs, tmp_path, monkeypatch, capsys):
    manifest = tmp_path / "weights.json"
    manifest.write_text(json.dumps({"weights": _ma_entries()}), encoding="utf-8")
    monkeypatch.setenv("SIMAI_ROOT", str(tmp_path / "simai"))
    monkeypatch.setenv("SIMAI_WEIGHTS_MANIFEST", str(manifest))
    monkeypatch.setenv("SIMAI_MIN_FREE_GB", "0")
    for k in ("SIMAI_MAPANYTHING_MODEL", "SIMAI_WEIGHTS_SET", "SIMAI_WM2_REPO"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(bs, "detect_gpu", lambda smi: {"name": "NVIDIA H100", "arch": "sm_90", "memory_total_mb": 81559.0})
    got: list[list[str]] = []

    def fake_ensure(_root, entries):
        got.append([e["name"] for e in entries])
        return [{"name": e["name"], "status": "present"} for e in entries]

    monkeypatch.setattr(bs, "ensure_weights", fake_ensure)
    assert bs.main(["check", "--sets", "static"]) == 0
    assert got == [["ma-model", "ma-config", "wm2", "lama"]]  # -539 큐(check --sets static)·preflight: apache 판 4.9GB 를 받지 않는다
    got.clear()
    monkeypatch.setenv("SIMAI_WEIGHTS_SET", "static")  # 실험 경로(MapAnything 이미지 m1·m2 비교): 목록 그대로
    assert bs.main(["check"]) == 0
    assert got == [[w["name"] for w in _ma_entries()]]
    capsys.readouterr()


# ---------------------------------------------------------------- 저장소 목록·Dockerfile·bake 대조 (S15P31A101-577)


def _baked_in_dockerfile() -> dict[str, dict]:
    """deploy/tier0/Dockerfile 의 weights-* 단계: 단계 이름 → {from, run, items: [(path, sha256, source)]}."""
    text = (REPO / "deploy" / "tier0" / "Dockerfile").read_text(encoding="utf-8").replace("\\\n", " ")
    stages: dict[str, dict] = {}
    for m in re.finditer(r"^FROM (\S+) AS (weights-[a-z0-9-]+)\n(?:#.*\n)*RUN (.*)$", text, re.MULTILINE):
        toks = m.group(3).split()
        args = toks[toks.index("/tmp/bake_weight.py") + 2:]
        assert len(args) % 3 == 0, m.group(2)
        stages[m.group(2)] = {"from": m.group(1), "run": m.group(3),
                              "items": [tuple(args[i:i + 3]) for i in range(0, len(args), 3)]}
    return stages


def test_dockerfile_bakes_exactly_the_demo_weights_with_manifest_hashes_and_sources(bs):
    doc = json.loads(bs.DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    by = {w["path"]: w for w in doc["weights"]}
    stages = _baked_in_dockerfile()
    assert stages, "weights-* 단계가 없다"
    baked: set[str] = set()
    for name, st in stages.items():
        assert st["from"] == "${WEIGHTS_IMAGE}", name  # simai-base 가 바뀌어도 다시 받지 않는다
        for path, sha, src in st["items"]:
            w = by[path]
            assert sha == w["sha256"], path
            s = w["source"]
            want = s["url"] if "url" in s else f"hf:{s['hf']['repo']}@{s['hf']['revision']}:{s['hf']['file']}"
            assert src == want, path
            assert (re.fullmatch(r"[0-9a-f]{40}", s["hf"]["revision"]) if "hf" in s else src.startswith("https://")), path
            baked.add(path)
        has_hf = any(src.startswith("hf:") for _p, _s, src in st["items"])
        assert ("--mount=type=secret,id=hf_token" in st["run"]) is has_hf, name  # HF 단계만 비밀을 연다
    # 시연 경로가 읽는 것 = tier0·avatar·ppe 전부 + static 중 MapAnything 은 현장 세팅 기본 판만. VLM·apache 판·실험 전용은 굽지 않는다
    demo = {w["path"] for w in doc["weights"] if set(w["sets"]) & {"tier0", "avatar", "ppe", "static"}
            and (w.get("group") != bs.MA_GROUP or w["path"].startswith("weights/map-anything/"))}
    assert baked == demo
    assert not any("vlm" in by[p]["sets"] for p in baked)
    assert {w["name"] for w in doc["weights"] if w.get("group") == bs.MA_GROUP} == {
        "map-anything-model", "map-anything-config", "map-anything-apache-model", "map-anything-apache-config"}


def test_baked_layers_sit_below_code_and_token_is_only_a_buildkit_secret():
    text = (REPO / "deploy" / "tier0" / "Dockerfile").read_text(encoding="utf-8")
    unified = text[text.index("FROM unified-deps AS unified\n"):]
    code_at = unified.index("COPY ai/src ./src")
    for name in _baked_in_dockerfile():
        line = f"COPY --link --from={name} /out/ /opt/simai/baked/"
        assert line in unified and unified.index(line) < code_at, name  # 코드·apt 층보다 앞, 앞 층과 무관한 --link
    assert "SIMAI_BAKED_WEIGHTS=/opt/simai/baked" in unified
    tier0 = text[text.index("AS tier0\n"):text.index("AS unified-deps")]
    assert "baked" not in tier0  # tier0 타깃은 그대로(가중치 없음)
    assert not re.search(r"^\s*(ARG|ENV)\b.*HF_TOKEN", text, re.MULTILINE) and "HUGGING_FACE" not in text
    bake = (REPO / "deploy" / "docker-bake.hcl").read_text(encoding="utf-8")
    unified_target = bake[bake.index('target "unified"'):]
    unified_target = unified_target[:unified_target.index("\n}\n") + 2]
    assert 'secret     = ["type=env,id=hf_token,env=HF_TOKEN"]' in unified_target
    assert 'WEIGHTS_IMAGE = "${PYTHON_RISK_ENGINE}"' in unified_target
    ignore = (REPO / "deploy" / "tier0" / "Dockerfile.dockerignore").read_text(encoding="utf-8").splitlines()
    assert "!deploy/scripts/bake_weight.py" in ignore


# ---------------------------------------------------------------- 굽기 단계 받기 도구 (deploy/scripts/bake_weight.py)


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_a):
        self.close()


def test_bake_weight_writes_file_tag_and_epoch_mtime(bw, tmp_path, monkeypatch):
    data = b"lama" * 1000
    sha = hashlib.sha256(data).hexdigest()
    monkeypatch.setattr(bw.urllib.request, "urlopen", lambda req, timeout: _Resp(data))
    assert bw.main([str(tmp_path / "out"), "weights/lama/big-lama.pt", sha, "https://example.invalid/big-lama.pt"]) == 0
    f = tmp_path / "out" / "weights" / "lama" / "big-lama.pt"
    assert f.read_bytes() == data and f.with_name("big-lama.pt.sha256").read_text(encoding="utf-8").strip() == sha
    assert {p.stat().st_mtime for p in (tmp_path / "out").rglob("*")} == {0}  # 다시 받아도 같은 층
    assert not list(f.parent.glob("*.part"))


def test_bake_weight_sends_secret_token_only_unredirected_and_never_prints_it(bw, tmp_path, monkeypatch, capsys):
    secret = tmp_path / "hf_token"
    secret.write_text(TOKEN + "\n", encoding="utf-8")
    monkeypatch.setattr(bw, "SECRET", secret)
    data = b"ckpt" * 100
    seen = []

    def fake_open(req, timeout):
        seen.append(req)
        return _Resp(data)

    monkeypatch.setattr(bw.urllib.request, "urlopen", fake_open)
    assert bw.main([str(tmp_path / "out"), "weights/sam/assets/m.pt", hashlib.sha256(data).hexdigest(),
                    "hf:facebook/sam-3d-body-dinov3@" + "a" * 40 + ":assets/m.pt"]) == 0
    req = seen[0]
    assert req.full_url == "https://huggingface.co/facebook/sam-3d-body-dinov3/resolve/" + "a" * 40 + "/assets/m.pt"
    assert req.unredirected_hdrs.get("Authorization") == f"Bearer {TOKEN}" and "Authorization" not in req.headers
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err
    files = [p for p in (tmp_path / "out").rglob("*") if p.is_file()]
    assert TOKEN not in "".join(p.read_text(encoding="utf-8", errors="replace") for p in files)


def test_bake_weight_refuses_hash_mismatch_and_bad_arguments(bw, tmp_path, monkeypatch):
    monkeypatch.setattr(bw.urllib.request, "urlopen", lambda req, timeout: _Resp(b"other"))
    with pytest.raises(SystemExit, match="해시 불일치"):
        bw.main([str(tmp_path / "out"), "weights/x.pt", "0" * 64, "https://example.invalid/x"])
    assert not (tmp_path / "out" / "weights" / "x.pt").exists() and not list((tmp_path / "out" / "weights").glob("*.part"))
    assert bw.main([str(tmp_path / "out"), "weights/x.pt", "0" * 64]) == 64
    with pytest.raises(SystemExit, match="경로"):
        bw.main([str(tmp_path / "out"), "../x.pt", "0" * 64, "https://example.invalid/x"])
    with pytest.raises(SystemExit, match="원본 형식"):
        bw.main([str(tmp_path / "out"), "weights/x.pt", "0" * 64, "http://example.invalid/x"])
