"""배포 보류·보류 배포 자동 실행(S15P31A101-676, Tier 0 연결·녹화 업로드 -682). deploy.sh·deploy-pending.sh·freeze.sh 를 가짜 docker·flock·curl 로 돌린다.

실제 EC2·Docker 는 쓰지 않는다. 가짜 docker 는 `exec`(DB 조회) 응답을 파일로 받고 부른 명령을 기록한다.
"""

import io
import os
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(not BASH, reason="bash 필요")

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
SETUP_ID = "0b6c2f4e-1d2a-4c3b-9e8f-112233445566"

FAKE_DOCKER = """#!/bin/sh
echo "$*" >> "$FAKE/docker.log"
case "$1" in
    exec)
        if [ -f "$FAKE/db_rc" ]; then echo "db 오류" >&2; exit "$(cat "$FAKE/db_rc")"; fi
        if [ -f "$FAKE/db_out" ]; then cat "$FAKE/db_out"; fi
        ;;
    buildx)
        # 이미지를 만드는 동안 작업이 시작되는 경우를 흉내 낸다
        if [ -f "$FAKE/db_out_after_bake" ]; then mv "$FAKE/db_out_after_bake" "$FAKE/db_out"; fi
        ;;
esac
exit 0
"""
FAKE_FLOCK = '#!/bin/sh\nif [ -f "$FAKE/lock_busy" ]; then exit 1; fi\nexit 0\n'
FAKE_CURL = '#!/bin/sh\nif [ -f "$FAKE/curl_rc" ]; then exit "$(cat "$FAKE/curl_rc")"; fi\nexit 0\n'
FAKE_TIMEOUT = '#!/bin/sh\nshift\nexec "$@"\n'


def unix(path):
    if os.name == "nt":
        return subprocess.check_output(
            [BASH, "-c", 'cygpath -u "$1"', "_", str(path)], text=True
        ).strip()
    return str(path)


def tar_bytes(revision):
    # 마이그레이션은 지금 판과 같게 둔다(덤프 경로는 이 시험 범위 밖이고 Windows 에서 mkdir -m 이 안 된다)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, text in (
            ("server/src/main/resources/db/migration/V1__init.sql", "old"),
            ("SOURCE", revision),
        ):
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class Host:
    """가짜 /opt/simai 와 bin, 가짜 명령 디렉터리"""

    def __init__(self, tmp_path):
        self.root = tmp_path / "simai"
        self.bin = self.root / "bin"
        self.fake = tmp_path / "fake"
        self.pending = self.root / "pending"
        self.bin.mkdir(parents=True)
        self.fake.mkdir()
        simai = unix(self.root)
        for name in ("deploy.sh", "freeze.sh", "deploy-pending.sh"):
            source = (ROOT / "deploy/ec2" / name).read_text(encoding="utf-8")
            source = source.replace("SIMAI=/opt/simai", f"SIMAI='{simai}'")
            (self.bin / name).write_text(source, encoding="utf-8", newline="\n")
            (self.bin / name).chmod(0o755)
        for name, body in (
            ("docker", FAKE_DOCKER),
            ("flock", FAKE_FLOCK),
            ("curl", FAKE_CURL),
            ("timeout", FAKE_TIMEOUT),
        ):
            (self.fake / name).write_text(body, encoding="utf-8", newline="\n")
            (self.fake / name).chmod(0o755)
        (self.root / ".env").write_text(
            "SIMAI_TAG=old\nSIMAI_DOMAIN=example.test\n", encoding="utf-8", newline="\n"
        )
        migration = self.root / "repo/server/src/main/resources/db/migration"
        migration.mkdir(parents=True)
        (migration / "V1__init.sql").write_text("old", encoding="utf-8", newline="\n")
        self.env = dict(
            os.environ,
            FAKE=unix(self.fake),
            PATH=str(self.fake) + os.pathsep + os.environ["PATH"],
        )
        self.env.pop("SSH_ORIGINAL_COMMAND", None)

    def db(self, *lines, rc=None):
        (self.fake / "db_out").write_text(
            "".join(f"{line}\n" for line in lines), encoding="utf-8", newline="\n"
        )
        if rc is not None:
            (self.fake / "db_rc").write_text(str(rc), encoding="utf-8")

    def run(self, name, *args, commit=None, ssh=None, data=b""):
        env = dict(self.env)
        if commit:
            env["SSH_ORIGINAL_COMMAND"] = f"deploy {commit}"
        if ssh:
            env["SSH_ORIGINAL_COMMAND"] = ssh
        proc = subprocess.run(
            [BASH, unix(self.bin / name), *args],
            env=env,
            input=data,
            capture_output=True,
            check=False,
        )
        proc.stdout = proc.stdout.decode("utf-8")
        proc.stderr = proc.stderr.decode("utf-8")
        return proc

    def deploy(self, commit, data=None):
        return self.run(
            "deploy.sh",
            commit=commit,
            data=tar_bytes(commit[:12]) if data is None else data,
        )

    def docker_log(self):
        path = self.fake / "docker.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def hold(self):
        path = self.pending / "hold"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else None

    def revision(self):
        return (
            (self.root / "repo/REVISION").read_text(encoding="utf-8").strip()
            if (self.root / "repo/REVISION").exists()
            else None
        )

    def set_env(self, **extra):
        lines = ["SIMAI_TAG=old", "SIMAI_DOMAIN=example.test"]
        lines += [f"{key}={value}" for key, value in extra.items()]
        (self.root / ".env").write_text(
            "".join(f"{line}\n" for line in lines), encoding="utf-8", newline="\n"
        )

    def ignored(self):
        path = self.pending / "ignored"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def age_hold(self, hours):
        lines = self.hold()
        lines[1] = str(int(time.time()) - int(hours * 3600))
        (self.pending / "hold").write_text(
            "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
        )


@pytest.fixture
def host(tmp_path):
    return Host(tmp_path)


def test_work_in_progress_holds_and_saves_tar(host):
    host.db("work 48")
    result = host.deploy(COMMIT_A)
    assert result.returncode == 0, result.stderr
    assert "배포 보류(작업 48)" in result.stdout
    assert (host.pending / f"{COMMIT_A}.tar").read_bytes() == tar_bytes(COMMIT_A[:12])
    hold = host.hold()
    assert hold[0] == COMMIT_A and hold[3] == "작업 48" and hold[4] == "0"
    log = host.docker_log()
    # 읽기 전용 트랜잭션으로 묻고, 보류했으면 이미지를 만들지 않는다
    assert "default_transaction_read_only=on" in log and "simai-ec2-db-1 psql" in log
    assert "buildx" not in log
    assert host.revision() is None
    assert "보류" in (host.pending / "history.log").read_text(encoding="utf-8")
    assert not list(host.pending.glob(".incoming.*"))


def test_setup_in_progress_reason(host):
    host.db("work 48", f"setup {SETUP_ID}")
    result = host.deploy(COMMIT_A)
    assert result.returncode == 0
    assert f"배포 보류(작업 48, 세팅 {SETUP_ID})" in result.stdout


def test_nothing_in_progress_deploys(host):
    host.db()
    result = host.deploy(COMMIT_A)
    assert result.returncode == 0, result.stderr
    assert "배포 완료" in result.stdout
    assert host.revision() == COMMIT_A[:12]
    assert "SIMAI_TAG=" + COMMIT_A[:12] in (host.root / ".env").read_text(
        encoding="utf-8"
    )
    assert "buildx" in host.docker_log()
    assert host.hold() is None
    assert not list(host.pending.glob(".incoming.*"))


@pytest.mark.parametrize(
    "rc,out",
    [
        (1, None),
        (None, "ERROR: relation does not exist"),
        (None, "tier0 1700000000"),  # 표시 칸(마지막 수신)이 빠진 줄
        (None, "upload x 3"),
    ],
)
def test_db_failure_deploys_with_warning(host, rc, out):
    host.db(*([out] if out else []), rc=rc)
    result = host.deploy(COMMIT_A)
    assert result.returncode == 0, result.stderr
    assert "확인하지 못해 보류하지 않고 배포한다" in result.stderr
    assert host.revision() == COMMIT_A[:12]


def test_newer_commit_replaces_hold_and_keeps_first_time(host):
    host.db("work 48")
    assert host.deploy(COMMIT_A).returncode == 0
    host.age_hold(2)
    first = host.hold()[1]
    result = host.deploy(COMMIT_B)
    assert result.returncode == 0
    assert "이전 보류(aaaaaaaaaaaa)를 이번 커밋으로 대체했다" in result.stdout
    hold = host.hold()
    assert hold[0] == COMMIT_B and hold[1] == first
    assert not (host.pending / f"{COMMIT_A}.tar").exists()
    assert (host.pending / f"{COMMIT_B}.tar").exists()


def test_invalid_input_does_not_replace_hold(host):
    host.db("work 48")
    assert host.deploy(COMMIT_A).returncode == 0
    result = host.deploy(COMMIT_B, data=b"not a tar")
    assert result.returncode == 2
    assert host.hold()[0] == COMMIT_A


def test_direct_deploy_supersedes_hold(host):
    host.db("work 48")
    assert host.deploy(COMMIT_A).returncode == 0
    host.db()
    result = host.deploy(COMMIT_B)
    assert result.returncode == 0, result.stderr
    assert host.revision() == COMMIT_B[:12]
    assert host.hold() is None and not (host.pending / f"{COMMIT_A}.tar").exists()


def test_work_started_during_bake_holds_before_touching_services(host):
    host.db()
    (host.fake / "db_out_after_bake").write_text(
        "work 51\n", encoding="utf-8", newline="\n"
    )
    result = host.deploy(COMMIT_A)
    assert result.returncode == 0, result.stderr
    assert "배포 보류(작업 51)" in result.stdout
    assert host.hold()[0] == COMMIT_A
    assert host.revision() is None and not (host.root / "repo.new").exists()
    assert "compose" not in host.docker_log()


def test_pending_without_hold_does_nothing(host):
    result = host.run("deploy-pending.sh")
    assert result.returncode == 0 and result.stdout == ""
    assert host.docker_log() == ""


def test_pending_runs_after_work_ends(host):
    host.db("work 48")
    assert host.deploy(COMMIT_A).returncode == 0
    still = host.run("deploy-pending.sh")
    assert still.returncode == 0 and "아직 작업·세팅·GPU 세션 진행 중(작업 48)" in still.stdout
    assert host.hold()[0] == COMMIT_A and host.revision() is None
    host.db()
    done = host.run("deploy-pending.sh")
    assert done.returncode == 0, done.stderr
    assert "보류한 배포를 실행한다" in done.stdout and "배포 완료" in done.stdout
    assert host.revision() == COMMIT_A[:12]
    assert host.hold() is None and not (host.pending / f"{COMMIT_A}.tar").exists()
    history = (host.pending / "history.log").read_text(encoding="utf-8")
    assert "보류 배포 완료" in history


def test_pending_respects_manual_freeze_and_status_shows_hold(host):
    host.db("work 48")
    assert host.deploy(COMMIT_A).returncode == 0
    assert (
        host.run("freeze.sh", "on", "--hours", "2", "--reason", "GPU 세션").returncode
        == 0
    )
    host.db()
    calls = host.docker_log()
    frozen = host.run("deploy-pending.sh")
    assert frozen.returncode == 0 and "동결 중" in frozen.stdout
    assert host.hold()[0] == COMMIT_A and host.revision() is None
    assert host.docker_log() == calls  # 동결이면 DB 도 묻지 않는다
    status = host.run("freeze.sh", "status").stdout
    assert (
        "동결 중" in status
        and "자동 보류 중 (커밋: aaaaaaaaaaaa, 사유: 작업 48" in status
    )
    assert "자동 배포 시도: 0회" in status
    host.run("freeze.sh", "off")
    assert host.run("deploy-pending.sh").returncode == 0
    status = host.run("freeze.sh", "status").stdout
    assert "동결 없음" in status and "자동 보류 없음" in status


def test_pending_safety_cap_deploys_and_ignores_forgotten_work(host):
    host.db("work 48")
    assert host.deploy(COMMIT_A).returncode == 0
    host.age_hold(11.9)
    assert host.run("deploy-pending.sh").stdout.count("다음 차례") == 1
    host.age_hold(12.1)
    result = host.run("deploy-pending.sh")
    assert result.returncode == 0, result.stderr
    assert "12시간을 넘어 진행 중(작업 48)이어도 배포한다" in result.stderr
    assert host.revision() == COMMIT_A[:12] and host.hold() is None
    # 잊힌 작업 48 로는 다시 보류하지 않지만, 새로 시작한 작업으로는 보류한다
    assert host.deploy(COMMIT_B).returncode == 0 and host.revision() == COMMIT_B[:12]
    host.db("work 48", "work 49")
    held = host.deploy(COMMIT_A)
    assert "배포 보류(작업 49)" in held.stdout


def test_safety_cap_hours_configurable(host):
    (host.root / ".env").write_text(
        "SIMAI_TAG=old\nSIMAI_DOMAIN=x\nSIMAI_DEPLOY_HOLD_MAX_HOURS=2\n",
        encoding="utf-8",
        newline="\n",
    )
    host.db("work 48")
    assert host.deploy(COMMIT_A).returncode == 0
    host.age_hold(2.1)
    result = host.run("deploy-pending.sh")
    assert "2시간을 넘어" in result.stderr and host.revision() == COMMIT_A[:12]


def test_pending_lock_busy_skips(host):
    host.db("work 48")
    assert host.deploy(COMMIT_A).returncode == 0
    host.db()
    calls = host.docker_log()
    (host.fake / "lock_busy").write_text("", encoding="utf-8")
    result = host.run("deploy-pending.sh")
    assert result.returncode == 0 and "잠금을 쥐고 있어" in result.stdout
    assert host.hold()[0] == COMMIT_A and host.docker_log() == calls
    # Jenkins 입구는 기다리다 실패한다(조용히 넘기지 않는다)
    assert host.deploy(COMMIT_B).returncode == 2


def test_pending_failure_keeps_hold_and_stops_after_limit(host):
    host.db("work 48")
    assert host.deploy(COMMIT_A).returncode == 0
    host.db()
    (host.fake / "curl_rc").write_text("22", encoding="utf-8")
    for attempt in (1, 2, 3):
        result = host.run("deploy-pending.sh")
        assert result.returncode == 1 and "되돌린다" in result.stderr
        assert (
            host.hold()[4] == str(attempt)
            and (host.pending / f"{COMMIT_A}.tar").exists()
        )
        assert host.revision() is None  # 이전 소스(REVISION 없음)로 돌아왔다
    stopped = host.run("deploy-pending.sh")
    assert stopped.returncode == 0 and "자동 실행을 멈췄다" in stopped.stdout
    assert "자동 배포 시도: 3회" in host.run("freeze.sh", "status").stdout


def test_pending_work_started_during_bake_keeps_attempts(host):
    host.db("work 48")
    assert host.deploy(COMMIT_A).returncode == 0
    host.db()
    (host.fake / "db_out_after_bake").write_text(
        "work 52\n", encoding="utf-8", newline="\n"
    )
    result = host.run("deploy-pending.sh")
    assert result.returncode == 0 and "다음 차례로 미룬다" in result.stdout
    assert host.hold()[4] == "0" and host.revision() is None


def utc(epoch):
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(epoch))


# --- S15P31A101-682: Tier 0 연결·녹화 업로드 ---
# 최근/오래됨의 판정은 DB(ACTIVE_SQL)가 한다. 가짜 DB 는 "최근"이면 줄을 내고 "오래됨"이면 내지 않는다.
# SQL 조건(임계값·시계·구간 시작)은 docker 기록에 남은 SQL 문으로 확인한다.


def test_tier0_recent_holds_with_last_report_reason(host):
    last = int(time.time()) - 20
    host.db(f"tier0 {last - 3600} {last}")
    result = host.deploy(COMMIT_A)
    assert result.returncode == 0, result.stderr
    reason = f"Tier 0 연결(마지막 보고 {utc(last)})"
    assert f"배포 보류({reason})" in result.stdout
    assert host.hold()[3] == reason and host.revision() is None
    sql = host.docker_log()
    # 서버 수신 시각(EC2 시계)으로 최근 3분, 구간을 찾는 창은 안전장치의 두 배(24시간), 미래 시각은 보지 않는다
    assert "FROM system_health" in sql
    assert "HAVING max(t) > now() - make_interval(mins => 3)" in sql
    assert "received_at > now() - make_interval(hours => 24)" in sql
    assert "received_at <= now() + interval '1 minute'" in sql
    status = host.run("freeze.sh", "status").stdout
    assert f"자동 보류 중 (커밋: aaaaaaaaaaaa, 사유: {reason}" in status


def test_tier0_stale_deploys_and_pending_runs_after_pod_stops(host):
    last = int(time.time()) - 20
    host.db(f"tier0 {last - 600} {last}")
    assert host.deploy(COMMIT_A).returncode == 0
    still = host.run("deploy-pending.sh")
    assert "아직 작업·세팅·GPU 세션 진행 중(Tier 0 연결" in still.stdout
    assert host.revision() is None
    host.db()  # Pod 를 껐다: 마지막 health 가 3분보다 오래돼 DB 가 줄을 내지 않는다
    done = host.run("deploy-pending.sh")
    assert done.returncode == 0, done.stderr
    assert "배포 완료" in done.stdout and host.revision() == COMMIT_A[:12]
    assert host.hold() is None


def test_recording_upload_recent_holds_and_stale_deploys(host):
    host.db(f"upload {int(time.time()) - 900} 3")
    result = host.deploy(COMMIT_A)
    assert result.returncode == 0, result.stderr
    assert "배포 보류(녹화 업로드 3건)" in result.stdout
    sql = host.docker_log()
    assert "purpose = 'RECORDING_SEGMENT'" in sql
    assert "HAVING max(t) > now() - make_interval(mins => 10)" in sql
    host.db()  # 마지막 발급이 10분보다 오래됐다
    done = host.run("deploy-pending.sh")
    assert done.returncode == 0 and host.revision() == COMMIT_A[:12]


def test_combined_reason_order(host):
    now = int(time.time())
    host.db("work 48", f"tier0 {now - 600} {now - 5}", f"upload {now - 300} 2")
    result = host.deploy(COMMIT_A)
    expected = f"작업 48, Tier 0 연결(마지막 보고 {utc(now - 5)}), 녹화 업로드 2건"
    assert f"배포 보류({expected})" in result.stdout and host.hold()[3] == expected


def test_minutes_configurable_and_invalid_falls_back(host):
    host.set_env(
        SIMAI_DEPLOY_HOLD_TIER0_MINUTES=5,
        SIMAI_DEPLOY_HOLD_UPLOAD_MINUTES=20,
        SIMAI_DEPLOY_HOLD_MAX_HOURS=6,
    )
    host.db()
    assert host.deploy(COMMIT_A).returncode == 0
    sql = host.docker_log()
    assert "make_interval(mins => 5)" in sql and "make_interval(mins => 20)" in sql
    assert "make_interval(hours => 12)" in sql  # 창 = 안전장치 6시간의 두 배
    host.set_env(SIMAI_DEPLOY_HOLD_TIER0_MINUTES="03", SIMAI_DEPLOY_HOLD_UPLOAD_MINUTES="x")
    (host.fake / "docker.log").unlink()
    result = host.deploy(COMMIT_B)
    assert "SIMAI_DEPLOY_HOLD_TIER0_MINUTES 이 1 이상 정수가 아니라 3분" in result.stderr
    assert "SIMAI_DEPLOY_HOLD_UPLOAD_MINUTES 이 1 이상 정수가 아니라 10분" in result.stderr
    sql = host.docker_log()
    assert "make_interval(mins => 3)" in sql and "make_interval(mins => 10)" in sql


def test_tier0_safety_cap_ignores_same_connection_until_pod_restarts(host):
    now = int(time.time())
    start = now - 13 * 3600
    host.db(f"tier0 {start} {now - 60}")
    assert host.deploy(COMMIT_A).returncode == 0
    host.age_hold(12.1)
    host.db(f"tier0 {start} {now - 2}")  # 마지막 수신은 바뀌어도 이어진 구간의 시작(키)은 같다
    result = host.run("deploy-pending.sh")
    assert result.returncode == 0, result.stderr
    assert "12시간을 넘어 진행 중(Tier 0 연결" in result.stderr
    assert host.revision() == COMMIT_A[:12] and host.hold() is None
    assert host.ignored() == [f"tier0 {start}"]
    # 같은 연결이 이어지는 동안은 다음 커밋을 보류하지 않는다
    assert host.deploy(COMMIT_B).returncode == 0 and host.revision() == COMMIT_B[:12]
    # Pod 를 껐다 켜면(새 구간 시작) 다시 보류한다
    host.db(f"tier0 {now - 30} {now - 1}")
    held = host.deploy(COMMIT_A)
    assert "배포 보류(Tier 0 연결" in held.stdout and host.revision() == COMMIT_B[:12]


def test_safety_cap_keeps_previously_ignored_items(host):
    # 예전에 넘긴 작업 48(작업 종료를 잊음)이 다음 안전장치 뒤에도 넘긴 채로 남는다(-676 판은 덮어써 잃었다)
    (host.pending).mkdir(parents=True, exist_ok=True)
    (host.pending / "ignored").write_text("work 48\n", encoding="utf-8", newline="\n")
    now = int(time.time())
    host.db("work 48", f"tier0 {now - 600} {now - 3}")
    held = host.deploy(COMMIT_A)
    assert "배포 보류(Tier 0 연결" in held.stdout and "작업 48" not in held.stdout
    host.age_hold(12.1)
    assert host.run("deploy-pending.sh").returncode == 0
    assert host.revision() == COMMIT_A[:12]
    assert sorted(host.ignored()) == sorted(["work 48", f"tier0 {now - 600}"])
    assert host.deploy(COMMIT_B).returncode == 0 and host.revision() == COMMIT_B[:12]


def test_upload_safety_cap_uses_run_start_as_key(host):
    now = int(time.time())
    host.db(f"upload {now - 3600} 4")
    assert host.deploy(COMMIT_A).returncode == 0
    host.age_hold(12.1)
    host.db(f"upload {now - 3600} 1")
    assert host.run("deploy-pending.sh").returncode == 0
    assert host.ignored() == [f"upload {now - 3600}"]
    host.db(f"upload {now - 60} 1")  # 업로드를 쉬었다가(10분 넘게) 새로 시작했다
    held = host.deploy(COMMIT_B)
    assert "배포 보류(녹화 업로드 1건)" in held.stdout


def test_usage_without_command_fails(host):
    assert host.run("deploy.sh").returncode == 2
    assert host.run("deploy.sh", "deploy", COMMIT_A).returncode == 2
    # Jenkins 입구에서는 pending 같은 다른 명령을 받지 않는다
    assert host.run("deploy.sh", ssh="pending").returncode == 2
    assert host.run("deploy.sh", "pending", ssh="deploy x").returncode == 2


def test_shell_syntax():
    for name in ("deploy.sh", "freeze.sh", "deploy-pending.sh", "install-host.sh"):
        subprocess.run([BASH, "-n", unix(ROOT / "deploy/ec2" / name)], check=True)
