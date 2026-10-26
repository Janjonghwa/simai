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
              "FAKE_SMI_CC", "FAKE_SMI_TOTAL_MB", "SIMAI_SESSION_REPO"):
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
    assert bs.choose_root({}, ws, ct) == (ws / "simai", "workspace")
    assert bs.choose_root({"SIMAI_ROOT": ""}, ws, ct) == (ws / "simai", "workspace")  # 빈 값은 없는 것
    # main 경로: SIMAI_ROOT 없음 + /workspace 없음 → 컨테이너 디스크에 가중치를 받고 그 위치를 Tier 0 에 넘긴다
    st = _setup(bs, tmp_path, monkeypatch)
    monkeypatch.delenv("SIMAI_ROOT")
    monkeypatch.setattr(bs, "WORKSPACE", tmp_path / "no-workspace")
    monkeypatch.setattr(bs, "CONTAINER_ROOT", ct)
    assert bs.main([]) == 0
    argv, env = st["calls"][0]
    assert argv == ["simai-tier0"] and env["SIMAI_ROOT"] == str(ct)
    assert (ct / "weights" / "yolo" / "yolo26x-pose.pt").read_bytes() == WEIGHTS
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
    assert bs.main([]) == bs.EXIT_GPU
    assert st["calls"] == [] and not st["target"].exists()
    assert why in capsys.readouterr().err
    assert _records(st["root"])[0]["exit"] == bs.EXIT_GPU


def test_missing_nvidia_smi_binary_exits_3(bs, tmp_path, monkeypatch):
    st = _setup(bs, tmp_path, monkeypatch)
    monkeypatch.setenv("SIMAI_NVIDIA_SMI", (tmp_path / "no-such-nvidia-smi").as_posix())
    assert bs.main([]) == bs.EXIT_GPU and st["calls"] == []


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
    assert bs.main([]) == 0
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
    assert bs.main([]) == 0 and len(st["calls"]) == 1
    assert _records(st["root"])[0]["weights"][0]["status"] == "present"


def test_present_weights_with_wrong_hash_refuse_and_are_not_deleted(bs, tmp_path, monkeypatch, capsys):
    st = _setup(bs, tmp_path, monkeypatch)
    st["target"].parent.mkdir(parents=True)
    st["target"].write_bytes(b"tampered")
    monkeypatch.setattr(bs, "urlopen", _no_network)
    assert bs.main([]) == bs.EXIT_STORAGE and st["calls"] == []
    assert st["target"].read_bytes() == b"tampered"  # 덮어쓰거나 지우지 않는다
    assert "해시 불일치" in capsys.readouterr().err


def test_downloaded_file_with_wrong_hash_is_discarded(bs, tmp_path, monkeypatch):
    st = _setup(bs, tmp_path, monkeypatch, sha="0" * 64)
    assert bs.main([]) == bs.EXIT_STORAGE and st["calls"] == []
    assert not st["target"].exists() and not list(st["target"].parent.glob("*.part-*"))


def test_manifest_without_hash_refuses_instead_of_trusting_the_download(bs, tmp_path, monkeypatch):
    st = _setup(bs, tmp_path, monkeypatch, sha="")
    monkeypatch.setattr(bs, "urlopen", _no_network)
    assert bs.main([]) == bs.EXIT_STORAGE and st["calls"] == [] and not st["target"].exists()


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
    assert bs.main([]) == 0
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
    assert bs.main([]) == bs.EXIT_STORAGE and st["calls"] == []
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
    drop = ("PUBLIC_KEY", "SSH_PUBLIC_KEY", "SIMAI_AUTHORIZED_KEYS_FILE", "SIMAI_SSHD", "SIMAI_TUNNEL_PREFIX", "SIMAI_TUNNEL_PORT")
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
    for argv in ([], ["tier0", "--config", "x"], ["check"], ["queue"], ["sleep", "infinity"], ["--help"]):
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
