"""deploy/aws/setup_gpu_aws.sh 시험 (S15P31A101-840). 실제 AWS·Docker·EC2 를 부르지 않는다.

PATH 앞에 상태를 파일로 기억하는 가짜 aws·docker·ssh 를 둔다. 가짜 ssh 는 EC2 대신 원격 명령을 시험 폴더의 .env 에 그대로 실행한다
(.env 병합 스크립트까지 시험). 비밀은 SECRET-CANARY-… 로 넣고 어떤 출력·명령줄 인자에도 나오지 않는지 본다.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "deploy" / "aws" / "setup_gpu_aws.sh"
BASH = shutil.which("bash")
ACCOUNT = "123456789012"
SRC = "jonghwa1492/tier0@sha256:" + "ab" * 32

pytestmark = pytest.mark.skipif(not BASH, reason="bash 필요")

FAKE_AWS = r"""#!/usr/bin/env bash
S=$FAKE_STATE
printf '%s\n' "$*" >>"$S/aws.log"
while [ "${1:-}" = --profile ] || [ "${1:-}" = --region ]; do shift 2; done
svc=$1 op=$2; shift 2
args=("$@")
val() { local i; for ((i = 0; i < ${#args[@]}; i++)); do [ "${args[$i]}" = "--$1" ] && { printf '%s' "${args[$((i + 1))]}"; return; }; done; }
has() { case " ${args[*]} " in *"$1"*) return 0 ;; esac; return 1; }
fpath() { local p=${1#file://}; if command -v cygpath >/dev/null 2>&1; then cygpath -u "$p"; else printf '%s' "$p"; fi; }
missing() { echo "An error occurred (NotFound) when calling $op" >&2; exit 254; }
q=$(val query)
case "$svc $op" in
  "sts get-caller-identity") echo 123456789012 ;;
  "ec2 describe-instance-types") echo "${FAKE_VCPUS:-16}" ;;
  "service-quotas get-service-quota") echo "${FAKE_QUOTA-16.0}" ;;
  "ec2 describe-instance-type-offerings") printf '%s\n' "${FAKE_AZS-ap-northeast-2a	ap-northeast-2b}" ;;
  "ec2 describe-vpcs") echo vpc-0abc12345 ;;
  "ec2 describe-subnets") for az in ${FAKE_AZS-ap-northeast-2a ap-northeast-2b}; do printf '%s\tsubnet-0%s\tTrue\n' "$az" "${az: -1}aaa1111"; done ;;
  "ssm get-parameter") echo ami-0123456789abcdef0 ;;
  "ec2 describe-images") printf 'Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 22.04) %s\t/dev/sda1\t%s\n' "${FAKE_AMI_DATE:-20261113}" "${FAKE_AMI_DESC-Supported EC2 instances: G5, G6e, G7e. NVIDIA driver version: 595.112.07, Default CUDA 13.2}" ;;
  "iam get-role") [ -f "$S/role" ] || missing; echo "arn:aws:iam::123456789012:role/simai-gpu-instance" ;;
  "iam create-role") touch "$S/role" ;;
  "iam get-instance-profile") [ -f "$S/profile" ] || missing
    case "$q" in *Roles*) cat "$S/profile_roles" 2>/dev/null ;; *) echo "arn:aws:iam::123456789012:instance-profile/simai-gpu-instance" ;; esac ;;
  "iam create-instance-profile") touch "$S/profile" ;;
  "iam add-role-to-instance-profile") val role-name >"$S/profile_roles" ;;
  "iam get-policy") a=$(val policy-arn); [ -f "$S/policy_${a##*/}.json" ] || missing; echo v1 ;;
  "iam get-policy-version") a=$(val policy-arn); cat "$S/policy_${a##*/}.json" ;;
  "iam list-policy-versions") echo 1 ;;
  "iam create-policy") cp "$(fpath "$(val policy-document)")" "$S/policy_$(val policy-name).json" ;;
  "iam create-policy-version") a=$(val policy-arn); cp "$(fpath "$(val policy-document)")" "$S/policy_${a##*/}.json" ;;
  "iam list-attached-role-policies") cat "$S/attached_role" 2>/dev/null ;;
  "iam attach-role-policy") val policy-arn >"$S/attached_role" ;;
  "ec2 describe-security-groups")
    if has --filters; then cat "$S/sg" 2>/dev/null || echo None
    else case "$q" in *'`22`'*) paste -sd '\t' "$S/sg_22" 2>/dev/null ;; *'`17000`'*) paste -sd '\t' "$S/sg_17000" 2>/dev/null ;; esac; fi ;;
  "ec2 create-security-group") echo sg-0fake00001 >"$S/sg"; cat "$S/sg" ;;
  "ec2 authorize-security-group-ingress") p=$(val port); val cidr >>"$S/sg_${p%%-*}"; echo >>"$S/sg_${p%%-*}"; sed -i '/^$/d' "$S/sg_${p%%-*}" ;;
  "ec2 describe-key-pairs") [ -f "$S/key" ] || missing; echo key-0fake ;;
  "ec2 create-key-pair") touch "$S/key"; printf -- '-----BEGIN OPENSSH PRIVATE KEY-----\r\nSECRET-CANARY-PRIVKEY\r\n-----END OPENSSH PRIVATE KEY-----\r\n' ;;
  "ec2 import-key-pair") touch "$S/key" ;;
  "ecr describe-repositories") [ -f "$S/repo" ] || missing; echo "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/simai/tier0" ;;
  "ecr create-repository") touch "$S/repo" ;;
  "ecr describe-images")
    t=$(val image-ids); t=${t#imageTag=}
    if [ -n "$t" ]; then [ -f "$S/image_$t" ] || missing; cat "$S/image_$t"
    else ls "$S"/image_* >/dev/null 2>&1 && cat "$(ls -t "$S"/image_* | head -n 1)" || echo None; fi ;;
  "ecr get-login-password") echo SECRET-CANARY-ECRPASS ;;
  "secretsmanager describe-secret") [ -f "$S/secret.json" ] || missing; echo "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:simai/gpu-boot-AbCdEf" ;;
  "secretsmanager create-secret") cp "$(fpath "$(val secret-string)")" "$S/secret.json" ;;
  "secretsmanager get-secret-value") cat "$S/secret.json"; printf '\r\n' ;;
  "secretsmanager put-secret-value") cp "$(fpath "$(val secret-string)")" "$S/secret.json" ;;
  "ec2 describe-launch-templates") [ -f "$S/lt.json" ] || missing; echo lt-0fake00000001 ;;
  "ec2 create-launch-template") cp "$(fpath "$(val launch-template-data)")" "$S/lt.json"; echo lt-0fake00000001 ;;
  "ec2 describe-launch-template-versions") cat "$S/lt.json" ;;
  "ec2 create-launch-template-version") cp "$(fpath "$(val launch-template-data)")" "$S/lt.json"; echo 2 ;;
  "ec2 modify-launch-template") : ;;
  "iam get-user") [ -f "$S/user" ] || missing; echo "arn:aws:iam::123456789012:user/simai-gpu-launcher" ;;
  "iam create-user") touch "$S/user" ;;
  "iam list-attached-user-policies") cat "$S/attached_user" 2>/dev/null ;;
  "iam attach-user-policy") val policy-arn >"$S/attached_user" ;;
  "iam list-access-keys") paste -sd '\t' "$S/keys" 2>/dev/null ;;
  "iam create-access-key") n=$(( $(cat "$S/keys" 2>/dev/null | wc -l) + 1 )); echo "AKIAFAKEKEY00000000$n" >>"$S/keys"
    printf 'AKIAFAKEKEY00000000%s\tSECRET-CANARY-IAMKEY-a/b+c%s\r\n' "$n" "$n" ;;
  *) echo "fake aws: 모르는 호출 $svc $op" >&2; exit 255 ;;
esac
"""

FAKE_DOCKER = r"""#!/usr/bin/env bash
S=$FAKE_STATE
printf '%s\n' "$*" >>"$S/docker.log"
case "$1" in
  login) cat >"$S/docker_login_stdin"; echo "Login Succeeded" ;;
  push) ref=$2; tag=${ref##*:}; printf 'sha256:%s\n' "$(printf 'cd%.0s' $(seq 32))" >"$S/image_$tag"
        echo "The push refers to repository [${ref%:*}]"; echo "$tag: digest: $(cat "$S/image_$tag") size: 1234" ;;
  pull|tag) : ;;
  *) echo "fake docker: $1" >&2; exit 1 ;;
esac
"""

FAKE_SSH = r"""#!/usr/bin/env bash
S=$FAKE_STATE
printf '%s\n' "$*" >>"$S/ssh.log"
while [ "${1:-}" = -o ]; do shift 2; done
shift   # 호스트
cmd="$*"
case "$cmd" in
  *curl*) echo 203.0.113.10; exit 0 ;;
esac
cmd=${cmd#sudo -n }
eval "$cmd"
"""


def unix(path: Path) -> str:
    if os.name == "nt":
        return subprocess.check_output(
            [BASH, "-c", 'cygpath -u "$1"', "_", str(path)], text=True
        ).strip()
    return str(path)


class Env:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.state = tmp / "state"
        self.bin.mkdir()
        self.state.mkdir()
        for name, body in (
            ("aws", FAKE_AWS),
            ("docker", FAKE_DOCKER),
            ("ssh", FAKE_SSH),
        ):
            (self.bin / name).write_text(body, encoding="utf-8", newline="\n")
            (self.bin / name).chmod(0o755)
        self.remote_env = tmp / "remote.env"
        self.remote_env.write_text(
            "SIMAI_GPU_AWS_ENABLED=false\nOTHER=keep\nSIMAI_GPU_AWS_IMAGE=\n",
            encoding="utf-8",
            newline="\n",
        )
        self.key_file = tmp / "home" / ".ssh" / "simai-gpu-aws.pem"
        self.files = {}
        for name, text in (
            ("boot", "SECRET-CANARY-BOOT-xyz\n"),
            ("hf", "SECRET-CANARY-HF-123\n"),
            (
                "key",
                "-----BEGIN PRIVATE KEY-----\nSECRET-CANARY-TLSKEY\n-----END PRIVATE KEY-----\n",
            ),
            (
                "crt",
                "-----BEGIN CERTIFICATE-----\nMIIBpublic\n-----END CERTIFICATE-----\n",
            ),
        ):
            p = tmp / f"{name}.txt"
            p.write_text(text, encoding="utf-8", newline="\n")
            self.files[name] = unix(p)
        self.outputs: list[str] = []
        self.env = dict(
            os.environ,
            PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
            FAKE_STATE=unix(self.state),
        )
        self.env.pop("AWS_PROFILE", None)

    def run(self, *args, extra=None, **env):
        e = dict(self.env, **env)
        common = [
            "--ec2-host",
            "simai-ec2-fake",
            "--remote-env",
            unix(self.remote_env),
            "--key-file",
            unix(self.key_file),
        ]
        r = subprocess.run(
            [
                BASH,
                unix(SCRIPT),
                *args,
                *(common if args[0] == "apply" else []),
                *(extra or []),
            ],
            env=e,
            text=True,
            encoding="utf-8",
            capture_output=True,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        self.outputs += [r.stdout, r.stderr]
        return r

    def secret_args(self):
        f = self.files
        return [
            "--source-image",
            SRC,
            "--tls-key",
            f["key"],
            "--tls-cert",
            f["crt"],
            "--boot-secret-file",
            f["boot"],
            "--hf-token-file",
            f["hf"],
        ]

    def log(self, name):
        p = self.state / name
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def reset_logs(self):
        for name in ("aws.log", "docker.log", "ssh.log"):
            (self.state / name).unlink(missing_ok=True)

    def assert_no_canary(self):
        for text in [
            *self.outputs,
            self.log("aws.log"),
            self.log("docker.log"),
            self.log("ssh.log"),
        ]:
            assert "SECRET-CANARY" not in text


WRITE_OPS = (
    "create-",
    "put-",
    "attach-",
    "authorize-",
    "import-key-pair",
    "modify-",
    "add-role-",
    "delete-",
)


def ops(log: str):
    """aws.log 한 줄 = --profile P --region R <서비스> <동작> … → '<서비스> <동작>'"""
    out = []
    for line in log.splitlines():
        w = line.split()
        while w and w[0] in ("--profile", "--region"):
            w = w[2:]
        out.append(" ".join(w[:2]))
    return out


def writes(log: str):
    return [o for o in ops(log) if o.split(" ")[1].startswith(WRITE_OPS)]


def test_check_ok_masks_account(tmp_path):
    env = Env(tmp_path)
    r = env.run("check")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "****9012" in r.stdout and ACCOUNT not in r.stdout + r.stderr
    assert (
        "ap-northeast-2a" in r.stdout and "595.112.07" in r.stdout and "G7e" in r.stdout
    )
    assert writes(env.log("aws.log")) == []


def test_check_fails_on_low_quota(tmp_path):
    env = Env(tmp_path)
    r = env.run("check", FAKE_QUOTA="8.0")
    assert r.returncode != 0
    assert "할당량 부족" in r.stdout


def test_check_fails_without_zone(tmp_path):
    env = Env(tmp_path)
    r = env.run("check", FAKE_AZS="")
    assert r.returncode != 0
    assert "가용 영역이" in r.stdout


def test_check_old_driver_fails_unknown_warns(tmp_path):
    env = Env(tmp_path)
    old = env.run(
        "check",
        FAKE_AMI_DESC="NVIDIA driver version: 570.172.08",
        FAKE_AMI_DATE="20250722",
    )
    assert old.returncode != 0 and "570.172.08" in old.stdout
    unknown = env.run("check", FAKE_AMI_DESC="", FAKE_AMI_DATE="20250722")
    assert unknown.returncode == 0 and "릴리스 노트에서 확인" in unknown.stdout


def test_apply_dry_run_writes_nothing(tmp_path):
    env = Env(tmp_path)
    r = env.run("apply", "--dry-run", extra=env.secret_args())
    assert r.returncode == 0, r.stdout + r.stderr
    assert writes(env.log("aws.log")) == []
    assert env.log("docker.log") == ""
    assert all(
        "curl" in line or "grep" in line for line in env.log("ssh.log").splitlines()
    )
    assert "[dry-run]" in r.stderr and "g7e.4xlarge" in r.stdout
    assert not env.key_file.exists()
    assert "SIMAI_GPU_AWS_ENABLED=false" in env.remote_env.read_text(encoding="utf-8")
    env.assert_no_canary()


def test_apply_creates_then_skips_and_keeps_secrets_hidden(tmp_path):
    env = Env(tmp_path)
    first = env.run("apply", extra=env.secret_args())
    assert first.returncode == 0, first.stdout + first.stderr
    done = writes(env.log("aws.log"))
    for op in (
        "iam create-policy",
        "iam create-role",
        "iam attach-role-policy",
        "iam create-instance-profile",
        "iam add-role-to-instance-profile",
        "ec2 create-security-group",
        "ec2 authorize-security-group-ingress",
        "ec2 create-key-pair",
        "ecr create-repository",
        "secretsmanager create-secret",
        "ec2 create-launch-template",
        "iam create-user",
        "iam attach-user-policy",
        "iam create-access-key",
    ):
        assert op in done, op

    # 비밀 JSON: 네 키, 원문 그대로(PEM 줄바꿈 포함)
    secret = json.loads((env.state / "secret.json").read_text(encoding="utf-8"))
    assert secret["SIMAI_GPU_BOOT_SECRET"] == "SECRET-CANARY-BOOT-xyz"
    assert secret["HF_TOKEN"] == "SECRET-CANARY-HF-123"
    assert secret["SIMAI_TIER0_TLS_PRIVATE_KEY"].startswith(
        "-----BEGIN PRIVATE KEY-----\nSECRET-CANARY-TLSKEY\n"
    )
    assert secret["SIMAI_TIER0_TLS_CERT_PEM"].endswith("-----END CERTIFICATE-----\n")

    # 정책·시작 템플릿 내용(계정·시작 템플릿 ID 채움, 설계 9번)
    launcher = (env.state / "policy_simai-gpu-launcher-access.json").read_text(
        encoding="utf-8"
    )
    assert (
        f"arn:aws:ec2:ap-northeast-2:{ACCOUNT}:launch-template/lt-0fake00000001"
        in launcher
        and "<" not in launcher
    )
    json.loads(launcher)
    instance = json.loads(
        (env.state / "policy_simai-gpu-instance-access.json").read_text(
            encoding="utf-8"
        )
    )
    assert (
        instance["Statement"][0]["Resource"]
        == f"arn:aws:secretsmanager:ap-northeast-2:{ACCOUNT}:secret:simai/gpu-boot-*"
    )
    lt = json.loads((env.state / "lt.json").read_text(encoding="utf-8"))
    assert (
        lt["MetadataOptions"]["HttpTokens"] == "required"
        and lt["MetadataOptions"]["HttpPutResponseHopLimit"] == 2
    )
    assert lt["BlockDeviceMappings"][0]["Ebs"] == {
        "VolumeSize": 200,
        "VolumeType": "gp3",
        "DeleteOnTermination": True,
    }
    assert lt["SecurityGroupIds"] == ["sg-0fake00001"] and lt["IamInstanceProfile"] == {
        "Name": "simai-gpu-instance"
    }
    assert not {"NetworkInterfaces", "InstanceType", "UserData"} & lt.keys()
    assert (env.state / "sg_22").read_text().split() == ["203.0.113.10/32"]
    assert (env.state / "sg_17000").read_text().split() == ["203.0.113.10/32"]

    # 개인 키는 파일에만(CR 없음), ECR 비밀번호는 docker stdin 으로만
    key = env.key_file.read_text(encoding="utf-8")
    assert "SECRET-CANARY-PRIVKEY" in key and "\r" not in key
    assert "SECRET-CANARY-ECRPASS" in env.log("docker_login_stdin")
    assert "push" in env.log("docker.log")

    # EC2 .env: 네 줄을 바꾸거나 더하고 나머지는 그대로, 백업
    remote = env.remote_env.read_text(encoding="utf-8").splitlines()
    image = (
        f"{ACCOUNT}.dkr.ecr.ap-northeast-2.amazonaws.com/simai/tier0@sha256:"
        + "cd" * 32
    )
    assert (
        "OTHER=keep" in remote
        and "SIMAI_GPU_AWS_ENABLED=true" in remote
        and f"SIMAI_GPU_AWS_IMAGE={image}" in remote
    )
    assert "SIMAI_GPU_AWS_ACCESS_KEY_ID=AKIAFAKEKEY000000001" in remote
    assert "SIMAI_GPU_AWS_SECRET_ACCESS_KEY=SECRET-CANARY-IAMKEY-a/b+c1" in remote
    assert sum(line.startswith("SIMAI_GPU_AWS_ENABLED=") for line in remote) == 1
    assert list(tmp_path.glob("remote.env.bak-*"))
    assert "****9012.dkr.ecr" in first.stdout and "lt-0fake00000001" in first.stdout
    assert "재시작은 하지 않았다" in first.stdout

    # 두 번째: 모두 있음 → 쓰기 없음
    env.reset_logs()
    second = env.run("apply", extra=["--source-image", SRC])
    assert second.returncode == 0, second.stdout + second.stderr
    assert writes(env.log("aws.log")) == []
    assert "push" not in env.log("docker.log")
    assert all(
        "curl" in line or "grep" in line for line in env.log("ssh.log").splitlines()
    )

    # --update-secret: 같으면 건너뜀, 바뀌면 put-secret-value 한 번
    env.reset_logs()
    same = env.run("apply", "--update-secret", extra=env.secret_args())
    assert same.returncode == 0 and "secretsmanager put-secret-value" not in ops(
        env.log("aws.log")
    )
    (tmp_path / "hf.txt").write_text(
        "SECRET-CANARY-HF-new\n", encoding="utf-8", newline="\n"
    )
    env.reset_logs()
    changed = env.run("apply", "--update-secret", extra=env.secret_args())
    assert changed.returncode == 0 and writes(env.log("aws.log")) == [
        "secretsmanager put-secret-value"
    ]
    assert (
        json.loads((env.state / "secret.json").read_text(encoding="utf-8"))["HF_TOKEN"]
        == "SECRET-CANARY-HF-new"
    )

    # --rotate-key: 새 키를 .env 로(예전 줄을 바꿈)
    env.reset_logs()
    rotated = env.run("apply", "--rotate-key", extra=["--source-image", SRC])
    assert rotated.returncode == 0 and writes(env.log("aws.log")) == [
        "iam create-access-key"
    ]
    remote = env.remote_env.read_text(encoding="utf-8").splitlines()
    assert "SIMAI_GPU_AWS_SECRET_ACCESS_KEY=SECRET-CANARY-IAMKEY-a/b+c2" in remote
    assert (
        sum(line.startswith("SIMAI_GPU_AWS_SECRET_ACCESS_KEY=") for line in remote) == 1
    )

    env.assert_no_canary()
    for text in env.outputs:
        assert ACCOUNT not in text


def test_apply_without_secret_inputs_fails_closed(tmp_path):
    env = Env(tmp_path)
    r = env.run("apply", extra=["--source-image", SRC])
    assert r.returncode != 0 and "--tls-key" in r.stderr
    assert "secretsmanager create-secret" not in ops(env.log("aws.log"))
