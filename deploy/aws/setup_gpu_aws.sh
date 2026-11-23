#!/usr/bin/env bash
# AWS 서울 GPU(g7e 등) 준비 스크립트 (S15P31A101-840, 설계 docs/design/S15P31A101-703-aws-gpu-provider.md 체크리스트 3·5~11).
#
#   check  읽기만: 계정, 할당량(L-DB2E81BA), 인스턴스 종류가 있는 가용 영역, 기본 VPC 서브넷의 공인 IPv4, DLAMI(드라이버 판), 이미 있는 자원 표
#   apply  멱등: 있으면 건너뛰고 바뀐 것만 새 판. IAM 정책 2개·인스턴스 역할·보안 그룹·키 페어·ECR 사본·Secrets Manager·시작 템플릿·
#          IAM 사용자 키 → EC2 /opt/simai/.env. 서버 재시작·compose 는 하지 않는다(Jenkins 배포와 겹치지 않게).
#
# 비밀 규칙: 부팅 비밀값·HF 토큰은 read -rs 또는 --boot-secret-file·--hf-token-file 로만, TLS 키·인증서는 파일 경로로만 받는다.
# 비밀은 화면·로그·명령줄 인자에 나오지 않는다(aws 에는 umask 077 임시 파일 file://, docker·ssh 에는 stdin).
# aws cli v1(1.46)·v2 둘 다 되는 옵션만 쓴다. Git Bash·Linux 둘 다. 자세한 순서는 deploy/aws/README.md.
set -euo pipefail
set +x
unset BASH_XTRACEFD 2>/dev/null || true

# ---------- 고정 이름(설계 -703) ----------
ROLE=simai-gpu-instance
INSTANCE_POLICY=simai-gpu-instance-access
LAUNCHER_USER=simai-gpu-launcher
LAUNCHER_POLICY=simai-gpu-launcher-access
SG_NAME=simai-gpu
LT_NAME=simai-gpu
ECR_REPO=simai/tier0
SECRET_ID=simai/gpu-boot
QUOTA_CODE=L-DB2E81BA   # Running On-Demand G and VT instances (vCPU)
DLAMI_PARAM=/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id
DLAMI_NAME_PREFIX='Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 22.04)'
DLAMI_NOTES=https://docs.aws.amazon.com/dlami/latest/devguide/aws-deep-learning-x86-base-gpu-ami-ubuntu-22-04.html
# 릴리스 노트로 확인한 판: 20261113 = NVIDIA 595.112.07, supported_ec2_instances 에 G7e (2026-11-23 확인)
DLAMI_VERIFIED_DATE=20261113
MIN_DRIVER_MAJOR=580    # CUDA 13 (명세 3.12, -720)

# ---------- 인자 ----------
CMD=""
PROFILE=simai
REGION=ap-northeast-2
INSTANCE_TYPE=g7e.4xlarge
DRY_RUN=0
EC2_IP=""
EC2_HOST=simai-ec2
REMOTE_ENV=/opt/simai/.env
SSH_CIDRS=()
I0_CIDRS=()
SOURCE_IMAGE=""
TLS_KEY=""
TLS_CERT=""
BOOT_SECRET_FILE=""
HF_TOKEN_FILE=""
UPDATE_SECRET=0
ROTATE_KEY=0
KEY_NAME=simai-gpu-aws
KEY_FILE="${HOME}/.ssh/simai-gpu-aws.pem"
AMI_OVERRIDE=""

usage() {
  cat <<'EOF'
사용: setup_gpu_aws.sh check|apply [옵션]
  --profile P            aws 프로필(기본 simai)
  --region R             리전(기본 ap-northeast-2)
  --instance-type T      인스턴스 종류(기본 g7e.4xlarge)
  --dry-run              apply 의 쓰기 호출을 출력만 한다
  --ec2-ip IP            EC2 서버 공인 IP(보안 그룹 출처). 없으면 ssh <ec2-host> 로 읽는다
  --ec2-host H           EC2 ssh 별칭(기본 simai-ec2)
  --remote-env PATH      EC2 .env 경로(기본 /opt/simai/.env)
  --ssh-cidr CIDR        22 번을 더 열 출처(여러 번)
  --i0-cidr CIDR         17000-17001 을 더 열 출처(여러 번, 집 노트북 공인 IP/32 등)
  --source-image REF     ECR 로 복사할 원본(예 jonghwa1492/tier0@sha256:<64자>)
  --tls-key FILE         Tier 0 TLS 비밀 키 PEM(비밀 만들기·바꾸기 때 필수)
  --tls-cert FILE        Tier 0 TLS 인증서 PEM(비밀 만들기·바꾸기 때 필수)
  --boot-secret-file F   부팅 비밀값 파일(없으면 화면에 보이지 않게 묻는다)
  --hf-token-file F      HF 토큰 파일(없으면 화면에 보이지 않게 묻는다)
  --update-secret        이미 있는 simai/gpu-boot 도 입력값과 비교해 다르면 새 판
  --rotate-key           simai-gpu-launcher 키가 이미 있어도 새로 만들어 EC2 .env 를 바꾼다
  --key-name N           키 페어 이름(기본 simai-gpu-aws)
  --key-file PATH        개인 키 파일(기본 ~/.ssh/simai-gpu-aws.pem)
  --ami-id AMI           DLAMI 대신 이 AMI(드라이버 판단은 똑같이 한다)
EOF
}

die() { printf '오류: %s\n' "$(mask "$*")" >&2; exit 1; }

[ $# -ge 1 ] || { usage >&2; exit 2; }
case "$1" in
  check|apply) CMD=$1; shift ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac
while [ $# -gt 0 ]; do
  case "$1" in
    --profile) PROFILE=$2; shift 2 ;;
    --region) REGION=$2; shift 2 ;;
    --instance-type) INSTANCE_TYPE=$2; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --ec2-ip) EC2_IP=$2; shift 2 ;;
    --ec2-host) EC2_HOST=$2; shift 2 ;;
    --remote-env) REMOTE_ENV=$2; shift 2 ;;
    --ssh-cidr) SSH_CIDRS+=("$2"); shift 2 ;;
    --i0-cidr) I0_CIDRS+=("$2"); shift 2 ;;
    --source-image) SOURCE_IMAGE=$2; shift 2 ;;
    --tls-key) TLS_KEY=$2; shift 2 ;;
    --tls-cert) TLS_CERT=$2; shift 2 ;;
    --boot-secret-file) BOOT_SECRET_FILE=$2; shift 2 ;;
    --hf-token-file) HF_TOKEN_FILE=$2; shift 2 ;;
    --update-secret) UPDATE_SECRET=1; shift ;;
    --rotate-key) ROTATE_KEY=1; shift ;;
    --key-name) KEY_NAME=$2; shift 2 ;;
    --key-file) KEY_FILE=$2; shift 2 ;;
    --ami-id) AMI_OVERRIDE=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) printf '모르는 인자: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ $REGION =~ ^[a-z]{2}(-[a-z]+)+-[0-9]$ ]] || die "리전 형식이 틀리다: $REGION"
[[ $INSTANCE_TYPE =~ ^[a-z][a-z0-9-]*\.[a-z0-9-]+$ ]] || die "인스턴스 종류 형식이 틀리다: $INSTANCE_TYPE"
if [ -n "$SOURCE_IMAGE" ] && ! [[ $SOURCE_IMAGE =~ ^[a-z0-9._/-]+@sha256:[0-9a-f]{64}$ ]]; then
  die "--source-image 는 다이제스트 참조여야 한다(예 jonghwa1492/tier0@sha256:<64자>)"
fi

# ---------- 공통 도구 ----------
ACCOUNT=""
WORK=$(umask 077 && mktemp -d "${TMPDIR:-/tmp}/simai-gpu-aws.XXXXXX")
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT
trap 'exit 130' INT TERM

# 출력에서 계정 ID 를 뒤 4자리만 남긴다
mask() {
  local s="$*"
  if [ -n "$ACCOUNT" ]; then s=${s//$ACCOUNT/****${ACCOUNT: -4}}; fi
  printf '%s' "$s"
}
say() { printf '%s\n' "$(mask "$*")"; }
mask_stream() {
  if [ -n "$ACCOUNT" ]; then sed "s/$ACCOUNT/****${ACCOUNT: -4}/g"; else cat; fi
}

# aws(Windows python 판)는 Git Bash 경로를 모른다 → file:// 은 C:/… 꼴로
file_url() {
  if command -v cygpath >/dev/null 2>&1; then printf 'file://%s' "$(cygpath -m "$1")"; else printf 'file://%s' "$1"; fi
}

AWS_BASE=(--profile "$PROFILE" --region "$REGION")

# 읽기: 표준 출력의 CR 을 지운다(Windows aws 의 text 출력). 오류는 표준 오류에(가린 뒤)
aws_r() {
  local err="$WORK/aws.err" rc=0 out
  out=$(aws "${AWS_BASE[@]}" "$@" 2>"$err" | tr -d '\r') || rc=$?
  if [ $rc -ne 0 ]; then
    [ "${QUIET_ERR:-0}" = 1 ] || mask_stream <"$err" >&2
    return $rc
  fi
  printf '%s' "$out"
}
# 있는지 보기(없음 오류는 숨김). 값이 비거나 None 이면 실패
aws_exists() {
  local out
  out=$(QUIET_ERR=1 aws_r "$@") || return 1
  [ -n "$out" ] && [ "$out" != None ] || return 1
  printf '%s' "$out"
}
# 쓰기: --dry-run 이면 출력만. 인자에 비밀을 넣지 않는다(비밀은 file:// 임시 파일)
aws_w() {
  if [ $DRY_RUN = 1 ]; then
    say "[dry-run] aws $*" >&2
    return 0
  fi
  aws_r "$@"
}
dry() { [ $DRY_RUN = 1 ]; }

FAILS=0
WARNS=0
ok()   { say "  [통과] $*"; }
warn() { say "  [주의] $*"; WARNS=$((WARNS + 1)); }
fail() { say "  [실패] $*"; FAILS=$((FAILS + 1)); }

# JSON 문자열 이스케이프(stdin → stdout). 줄은 \n 으로 잇고, $1=tail 이면 끝에 \n 하나를 붙인다(PEM)
json_esc() {
  awk -v tail="${1:-}" '
    function esc(s,   out, i, c) {
      out = ""
      for (i = 1; i <= length(s); i++) {
        c = substr(s, i, 1)
        if (c == "\\") out = out "\\\\"
        else if (c == "\"") out = out "\\\""
        else if (c == "\t") out = out "\\t"
        else if (c == "\r") continue
        else out = out c
      }
      return out
    }
    { printf "%s%s", (NR > 1 ? "\\n" : ""), esc($0) }
    END { if (tail == "tail" && NR > 0) printf "\\n" }'
}
strip_ws() { tr -d ' \t\r\n'; }

# ---------- 찾기(check·apply 공통) ----------
VCPUS=""
QUOTA=""
AZS=()
VPC_ID=""
PREFERRED_AZ=""
PREFERRED_SUBNET=""
AMI_ID=""
AMI_NAME=""
AMI_ROOT=""
EC2_IP_RESOLVED=""

discover_account() {
  ACCOUNT=$(aws_r sts get-caller-identity --query Account --output text) || die "aws 자격 증명을 못 읽는다(프로필 $PROFILE, aws configure --profile $PROFILE)"
  [[ $ACCOUNT =~ ^[0-9]{12}$ ]] || die "계정 ID 형식이 틀리다"
  ok "계정 $(mask "$ACCOUNT"), 프로필 $PROFILE, 리전 $REGION"
}

discover_quota() {
  VCPUS=$(aws_r ec2 describe-instance-types --instance-types "$INSTANCE_TYPE" \
    --query 'InstanceTypes[0].VCpuInfo.DefaultVCpus' --output text) || { fail "$INSTANCE_TYPE 를 이 리전이 모른다"; return; }
  QUOTA=$(aws_r service-quotas get-service-quota --service-code ec2 --quota-code "$QUOTA_CODE" \
    --query Quota.Value --output text) || { fail "할당량 $QUOTA_CODE 를 못 읽었다(servicequotas:GetServiceQuota 권한)"; return; }
  local q=${QUOTA%%.*}
  if [[ $q =~ ^[0-9]+$ ]] && [[ $VCPUS =~ ^[0-9]+$ ]] && [ "$q" -ge "$VCPUS" ]; then
    ok "할당량 Running On-Demand G and VT = $QUOTA vCPU ≥ $INSTANCE_TYPE $VCPUS vCPU"
  else
    fail "할당량 부족: Running On-Demand G and VT = ${QUOTA:-?} vCPU < $INSTANCE_TYPE ${VCPUS:-?} vCPU (Service Quotas 에서 신청)"
  fi
}

discover_azs() {
  local raw
  raw=$(aws_r ec2 describe-instance-type-offerings --location-type availability-zone \
    --filters "Name=instance-type,Values=$INSTANCE_TYPE" --query 'InstanceTypeOfferings[].Location' --output text) || raw=""
  read -r -a AZS <<<"$(printf '%s' "$raw" | tr '\t\n' '  ')"
  if [ ${#AZS[@]} -eq 0 ]; then
    fail "$INSTANCE_TYPE 를 파는 가용 영역이 $REGION 에 없다"
  else
    ok "$INSTANCE_TYPE 가용 영역: ${AZS[*]}"
  fi
}

discover_subnets() {
  VPC_ID=$(aws_exists ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text) || {
    fail "기본 VPC 가 없다(서브넷을 화면 「GPU 설정」에 따로 넣어야 한다)"; return; }
  local lines az subnet pub
  lines=$(aws_r ec2 describe-subnets --filters "Name=vpc-id,Values=$VPC_ID" Name=default-for-az,Values=true \
    --query 'Subnets[].[AvailabilityZone,SubnetId,MapPublicIpOnLaunch]' --output text) || lines=""
  for az in "${AZS[@]}"; do
    subnet=$(printf '%s\n' "$lines" | awk -v az="$az" '$1 == az { print $2; exit }')
    pub=$(printf '%s\n' "$lines" | awk -v az="$az" '$1 == az { print $3; exit }')
    if [ -z "$subnet" ]; then
      warn "$az: 기본 서브넷 없음"
    elif [ "$pub" != True ] && [ "$pub" != true ]; then
      warn "$az: 기본 서브넷 $subnet 의 공인 IPv4 자동 할당이 꺼져 있다"
    else
      ok "$az: 기본 서브넷 $subnet (공인 IPv4 자동 할당 켜짐)"
      if [ -z "$PREFERRED_AZ" ]; then PREFERRED_AZ=$az; PREFERRED_SUBNET=$subnet; fi
    fi
  done
  [ -n "$PREFERRED_AZ" ] || [ ${#AZS[@]} -eq 0 ] || fail "$INSTANCE_TYPE 가용 영역 중 공인 IPv4 를 주는 기본 서브넷이 없다"
}

# AMI 이름·설명으로 드라이버 판과 G7e 지원을 판단한다.
# 1) 설명에 "driver … NNN.xx" 가 있으면 그 주 번호 ≥ 580 인지  2) 없으면 이름 끝 날짜가 릴리스 노트로 확인한 판(20261113) 이상인지
# 3) 그 밖은 「확인 필요」(릴리스 노트 링크). G7e 는 설명의 지원 목록에 있으면 통과, 없으면 날짜로 같은 판단
judge_ami() {
  local name=$1 desc=$2 driver date major
  driver=$(printf '%s' "$desc" | grep -oiE 'driver[^0-9]{0,40}[0-9]{3}\.[0-9]+(\.[0-9]+)?' | grep -oE '[0-9]{3}\.[0-9.]+' | head -n 1 || true)
  date=$(printf '%s' "$name" | grep -oE '[0-9]{8}$' || true)
  if [ -n "$driver" ]; then
    major=${driver%%.*}
    if [ "$major" -ge $MIN_DRIVER_MAJOR ]; then ok "드라이버 $driver (R$MIN_DRIVER_MAJOR 이상, AMI 설명)"
    else fail "드라이버 $driver < R$MIN_DRIVER_MAJOR (CUDA 13 불가) → 다른 AMI(--ami-id)"; fi
  elif [ -n "$date" ] && [ "$date" -ge $DLAMI_VERIFIED_DATE ]; then
    ok "AMI 판 $date ≥ $DLAMI_VERIFIED_DATE (릴리스 노트: 그 판 드라이버 595.112.07, $DLAMI_NOTES)"
  else
    warn "드라이버 판을 설명에서 못 읽었고 판 날짜(${date:-없음})가 확인한 판보다 이르다 → 릴리스 노트에서 확인: $DLAMI_NOTES"
  fi
  case "$INSTANCE_TYPE" in
    g7e.*)
      if printf '%s' "$desc" | grep -q 'G7e'; then ok "AMI 설명의 지원 인스턴스에 G7e"
      elif [ -n "$date" ] && [ "$date" -ge $DLAMI_VERIFIED_DATE ]; then ok "G7e 지원(릴리스 노트 $DLAMI_VERIFIED_DATE 판 supported_ec2_instances)"
      else warn "G7e 지원 여부를 모른다 → 릴리스 노트의 supported_ec2_instances 확인: $DLAMI_NOTES"; fi ;;
  esac
}

discover_ami() {
  if [ -n "$AMI_OVERRIDE" ]; then
    AMI_ID=$AMI_OVERRIDE
  else
    AMI_ID=$(aws_r ssm get-parameter --name "$DLAMI_PARAM" --query Parameter.Value --output text) || {
      fail "DLAMI SSM 공개 파라미터를 못 읽었다($DLAMI_PARAM)"; return; }
  fi
  local row desc
  row=$(aws_r ec2 describe-images --image-ids "$AMI_ID" --query 'Images[0].[Name,RootDeviceName,Description]' --output text) || {
    fail "AMI $AMI_ID 를 못 읽었다"; return; }
  IFS=$'\t' read -r AMI_NAME AMI_ROOT desc <<<"$row"
  ok "AMI $AMI_ID = $AMI_NAME (루트 $AMI_ROOT)"
  if [ -z "$AMI_OVERRIDE" ] && [[ $AMI_NAME != "$DLAMI_NAME_PREFIX"* ]]; then
    warn "AMI 이름이 '$DLAMI_NAME_PREFIX' 로 시작하지 않는다"
  fi
  judge_ami "$AMI_NAME" "${desc:-}"
}

# 보안 그룹 출처: --ec2-ip 우선, 없으면 ssh 로 EC2 공인 IP 를 읽는다(읽기만)
resolve_ec2_ip() {
  if [ -z "$EC2_IP" ]; then
    EC2_IP=$(ssh -o BatchMode=yes -o ConnectTimeout=10 "$EC2_HOST" curl -s --max-time 10 https://checkip.amazonaws.com 2>/dev/null | tr -d '\r\n ' || true)
  fi
  [[ $EC2_IP =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || die "EC2 서버 공인 IP 를 모른다(--ec2-ip 로 준다)"
  EC2_IP_RESOLVED=$EC2_IP
}

# ---------- 자원 상태 ----------
st_role()    { QUIET_ERR=1 aws_exists iam get-role --role-name "$ROLE" --query Role.Arn --output text; }
st_profile() { QUIET_ERR=1 aws_exists iam get-instance-profile --instance-profile-name "$ROLE" --query InstanceProfile.Arn --output text; }
st_policy()  { QUIET_ERR=1 aws_exists iam get-policy --policy-arn "arn:aws:iam::$ACCOUNT:policy/$1" --query Policy.DefaultVersionId --output text; }
st_sg()      { aws_exists ec2 describe-security-groups --filters "Name=group-name,Values=$SG_NAME" ${VPC_ID:+"Name=vpc-id,Values=$VPC_ID"} --query 'SecurityGroups[0].GroupId' --output text; }
st_key()     { aws_exists ec2 describe-key-pairs --key-names "$KEY_NAME" --query 'KeyPairs[0].KeyPairId' --output text; }
st_repo()    { aws_exists ecr describe-repositories --repository-names "$ECR_REPO" --query 'repositories[0].repositoryUri' --output text; }
st_secret()  { aws_exists secretsmanager describe-secret --secret-id "$SECRET_ID" --query ARN --output text; }
st_lt()      { aws_exists ec2 describe-launch-templates --launch-template-names "$LT_NAME" --query 'LaunchTemplates[0].LaunchTemplateId' --output text; }
st_user()    { aws_exists iam get-user --user-name "$LAUNCHER_USER" --query User.Arn --output text; }

status_table() {
  say ""
  say "자원 상태"
  printf '  %-28s %-26s %s\n' "자원" "이름" "상태"
  local row v
  for row in "인스턴스 역할|$ROLE|st_role" "인스턴스 프로파일|$ROLE|st_profile" \
             "정책(인스턴스)|$INSTANCE_POLICY|st_policy $INSTANCE_POLICY" "정책(서버)|$LAUNCHER_POLICY|st_policy $LAUNCHER_POLICY" \
             "보안 그룹|$SG_NAME|st_sg" "키 페어|$KEY_NAME|st_key" "ECR 저장소|$ECR_REPO|st_repo" \
             "Secrets Manager|$SECRET_ID|st_secret" "시작 템플릿|$LT_NAME|st_lt" "IAM 사용자|$LAUNCHER_USER|st_user"; do
    IFS='|' read -r label name fn <<<"$row"
    if v=$(QUIET_ERR=1 $fn 2>/dev/null); then printf '  %-28s %-26s %s\n' "$label" "$name" "$(mask "있음 $v")"
    else printf '  %-28s %-26s %s\n' "$label" "$name" "없음"; fi
  done
  if [ -f "$KEY_FILE" ]; then say "  개인 키 파일 $KEY_FILE 있음"; else say "  개인 키 파일 $KEY_FILE 없음"; fi
}

run_check() {
  say "== 확인(읽기만) =="
  discover_account
  discover_quota
  discover_azs
  discover_subnets
  discover_ami
  status_table
  say ""
  say "결과: 실패 $FAILS, 주의 $WARNS"
  [ $FAILS -eq 0 ]
}

# ---------- apply 단계 ----------
POLICY_CHANGED=""

instance_policy_doc() {
  cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "BootSecret", "Effect": "Allow", "Action": "secretsmanager:GetSecretValue",
      "Resource": "arn:aws:secretsmanager:$REGION:$ACCOUNT:secret:$SECRET_ID-*" },
    { "Sid": "EcrLogin", "Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*" },
    { "Sid": "EcrPull", "Effect": "Allow",
      "Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"],
      "Resource": "arn:aws:ecr:$REGION:$ACCOUNT:repository/$ECR_REPO" }
  ]
}
EOF
}

launcher_policy_doc() {
  local lt=$1
  cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "Describe", "Effect": "Allow",
      "Action": ["ec2:DescribeInstances", "ec2:DescribeInstanceTypes", "ec2:DescribeInstanceTypeOfferings",
                 "ec2:DescribeSubnets", "ec2:DescribeSpotPriceHistory"],
      "Resource": "*", "Condition": { "StringEquals": { "aws:RequestedRegion": "$REGION" } } },
    { "Sid": "Pricing", "Effect": "Allow", "Action": "pricing:GetProducts", "Resource": "*" },
    { "Sid": "RunTaggedInstanceFromTemplate", "Effect": "Allow", "Action": "ec2:RunInstances",
      "Resource": "arn:aws:ec2:$REGION:$ACCOUNT:instance/*",
      "Condition": { "StringEquals": { "aws:RequestTag/simai:managed": "gpu",
                                       "ec2:LaunchTemplate": "arn:aws:ec2:$REGION:$ACCOUNT:launch-template/$lt" },
                     "StringLike": { "ec2:InstanceType": ["g5.*", "g6e.*", "g7e.*", "p4d.*", "p5.*", "p5en.*"] } } },
    { "Sid": "RunOtherResources", "Effect": "Allow", "Action": "ec2:RunInstances",
      "Resource": ["arn:aws:ec2:$REGION::image/*",
                   "arn:aws:ec2:$REGION:$ACCOUNT:launch-template/$lt",
                   "arn:aws:ec2:$REGION:$ACCOUNT:subnet/*",
                   "arn:aws:ec2:$REGION:$ACCOUNT:security-group/*",
                   "arn:aws:ec2:$REGION:$ACCOUNT:network-interface/*",
                   "arn:aws:ec2:$REGION:$ACCOUNT:volume/*",
                   "arn:aws:ec2:$REGION:$ACCOUNT:key-pair/*"] },
    { "Sid": "TagOnCreateOnly", "Effect": "Allow", "Action": "ec2:CreateTags",
      "Resource": ["arn:aws:ec2:$REGION:$ACCOUNT:instance/*", "arn:aws:ec2:$REGION:$ACCOUNT:volume/*"],
      "Condition": { "StringEquals": { "ec2:CreateAction": "RunInstances" } } },
    { "Sid": "TerminateOnlyOurs", "Effect": "Allow", "Action": "ec2:TerminateInstances",
      "Resource": "arn:aws:ec2:$REGION:$ACCOUNT:instance/*",
      "Condition": { "StringEquals": { "aws:ResourceTag/simai:managed": "gpu" } } },
    { "Sid": "PassInstanceRole", "Effect": "Allow", "Action": "iam:PassRole",
      "Resource": "arn:aws:iam::$ACCOUNT:role/$ROLE",
      "Condition": { "StringEquals": { "iam:PassedToService": "ec2.amazonaws.com" } } }
  ]
}
EOF
}

# 고객 관리형 정책: 없으면 만들고, 기본 판과 다르면 새 판(기본으로). 판이 5개면 가장 오래된 비기본 판을 지운다
ensure_policy() {
  local name=$1 doc=$2 arn="arn:aws:iam::$ACCOUNT:policy/$1" ver cur count oldest
  if ver=$(st_policy "$name"); then
    cur=$(aws_r iam get-policy-version --policy-arn "$arn" --version-id "$ver" --query PolicyVersion.Document --output json | strip_ws)
    if [ "$cur" = "$(strip_ws <"$doc")" ]; then say "  정책 $name: 같음, 건너뜀"; return; fi
    count=$(aws_r iam list-policy-versions --policy-arn "$arn" --query 'length(Versions)' --output text)
    if [ "${count:-0}" -ge 5 ]; then
      oldest=$(aws_r iam list-policy-versions --policy-arn "$arn" \
        --query 'sort_by(Versions[?IsDefaultVersion==`false`], &CreateDate)[0].VersionId' --output text)
      aws_w iam delete-policy-version --policy-arn "$arn" --version-id "$oldest" >/dev/null
    fi
    aws_w iam create-policy-version --policy-arn "$arn" --policy-document "$(file_url "$doc")" --set-as-default >/dev/null
    say "  정책 $name: 바뀜 → 새 판"
  else
    aws_w iam create-policy --policy-name "$name" --policy-document "$(file_url "$doc")" >/dev/null
    say "  정책 $name: 만듦"
  fi
}

step_instance_role() {
  say "[역할] $ROLE"
  local doc="$WORK/instance-policy.json" trust="$WORK/trust.json" arn="arn:aws:iam::$ACCOUNT:policy/$INSTANCE_POLICY" attached roles
  instance_policy_doc >"$doc"
  ensure_policy "$INSTANCE_POLICY" "$doc"
  if st_role >/dev/null; then
    say "  역할: 있음, 건너뜀"
  else
    printf '%s\n' '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >"$trust"
    aws_w iam create-role --role-name "$ROLE" --assume-role-policy-document "$(file_url "$trust")" \
      --description "simai GPU instance (S15P31A101-703)" >/dev/null
    say "  역할: 만듦"
  fi
  attached=$(QUIET_ERR=1 aws_r iam list-attached-role-policies --role-name "$ROLE" \
    --query "AttachedPolicies[?PolicyArn=='$arn'].PolicyArn" --output text || true)
  if [ "$attached" = "$arn" ]; then say "  정책 붙임: 있음, 건너뜀"
  else aws_w iam attach-role-policy --role-name "$ROLE" --policy-arn "$arn" >/dev/null; say "  정책 붙임: 붙임"; fi
  if st_profile >/dev/null; then
    say "  인스턴스 프로파일: 있음"
  else
    aws_w iam create-instance-profile --instance-profile-name "$ROLE" >/dev/null
    say "  인스턴스 프로파일: 만듦"
  fi
  roles=$(QUIET_ERR=1 aws_r iam get-instance-profile --instance-profile-name "$ROLE" \
    --query 'InstanceProfile.Roles[].RoleName' --output text || true)
  if printf '%s' "$roles" | tr '\t' '\n' | grep -qx "$ROLE"; then say "  프로파일에 역할: 있음, 건너뜀"
  else aws_w iam add-role-to-instance-profile --instance-profile-name "$ROLE" --role-name "$ROLE" >/dev/null; say "  프로파일에 역할: 넣음"; fi
}

SG_ID=""
sg_allow() {
  local from=$1 to=$2 cidr=$3 have ports=$1
  [ "$from" = "$to" ] || ports="$from-$to"
  have=$(aws_r ec2 describe-security-groups --group-ids "$SG_ID" \
    --query "SecurityGroups[0].IpPermissions[?FromPort==\`$from\` && ToPort==\`$to\`].IpRanges[].CidrIp" --output text || true)
  if printf '%s' "$have" | tr '\t' '\n' | grep -qxF "$cidr"; then
    say "  TCP $ports ← $cidr: 있음"
  else
    aws_w ec2 authorize-security-group-ingress --group-id "$SG_ID" --protocol tcp --port "$from-$to" --cidr "$cidr" >/dev/null
    say "  TCP $ports ← $cidr: 엶"
  fi
}

step_security_group() {
  say "[보안 그룹] $SG_NAME"
  resolve_ec2_ip
  say "  EC2 서버 공인 IP $EC2_IP_RESOLVED"
  if SG_ID=$(st_sg); then
    say "  보안 그룹: 있음 $SG_ID"
  else
    if dry; then aws_w ec2 create-security-group --group-name "$SG_NAME" --vpc-id "$VPC_ID" --description "simai GPU (S15P31A101-703)"; SG_ID=sg-DRYRUN
    else
      SG_ID=$(aws_w ec2 create-security-group --group-name "$SG_NAME" --vpc-id "$VPC_ID" \
        --description "simai GPU (S15P31A101-703)" --query GroupId --output text)
    fi
    say "  보안 그룹: 만듦 $SG_ID"
  fi
  [ "$SG_ID" = sg-DRYRUN ] && { say "  [dry-run] 규칙: 22 ← $EC2_IP_RESOLVED/32 ${SSH_CIDRS[*]:-}, 17000-17001 ← $EC2_IP_RESOLVED/32 ${I0_CIDRS[*]:-}"; return; }
  local c
  sg_allow 22 22 "$EC2_IP_RESOLVED/32"
  for c in "${SSH_CIDRS[@]+"${SSH_CIDRS[@]}"}"; do sg_allow 22 22 "$c"; done
  sg_allow 17000 17001 "$EC2_IP_RESOLVED/32"
  for c in "${I0_CIDRS[@]+"${I0_CIDRS[@]}"}"; do sg_allow 17000 17001 "$c"; done
}

step_key_pair() {
  say "[키 페어] $KEY_NAME → $KEY_FILE"
  local aws_has=0
  st_key >/dev/null && aws_has=1
  if [ $aws_has = 1 ]; then
    if [ -f "$KEY_FILE" ]; then say "  키 페어·개인 키 파일 둘 다 있음, 건너뜀"
    else warn "AWS 에 키 페어는 있는데 개인 키 파일이 없다(되살릴 수 없음). 호스트 SSH 가 필요하면 콘솔에서 키 페어를 지우고 다시 돌린다"; fi
    return
  fi
  if [ -f "$KEY_FILE" ]; then
    # 노트북 키는 건드리지 않고 공개 키만 올린다
    local pub="$WORK/key.pub"
    ssh-keygen -y -f "$KEY_FILE" >"$pub" || die "개인 키 파일에서 공개 키를 못 만든다: $KEY_FILE"
    aws_w ec2 import-key-pair --key-name "$KEY_NAME" --public-key-material "$(file_url "$pub")" >/dev/null
    say "  개인 키 파일이 있어 공개 키만 올림"
    return
  fi
  if dry; then aws_w ec2 create-key-pair --key-name "$KEY_NAME" --key-type ed25519 --query KeyMaterial --output text; return; fi
  mkdir -p "$(dirname "$KEY_FILE")"
  local tmp="$KEY_FILE.tmp.$$"
  ( umask 077; aws "${AWS_BASE[@]}" ec2 create-key-pair --key-name "$KEY_NAME" --key-type ed25519 \
      --query KeyMaterial --output text 2>"$WORK/aws.err" | tr -d '\r' >"$tmp" ) || {
    rm -f "$tmp"; mask_stream <"$WORK/aws.err" >&2; die "키 페어를 못 만들었다"; }
  mv "$tmp" "$KEY_FILE"
  chmod 600 "$KEY_FILE"
  say "  키 페어 만듦, 개인 키는 $KEY_FILE (600) 에만"
}

ECR_IMAGE=""   # <계정>.dkr.ecr.<리전>.amazonaws.com/simai/tier0@sha256:…
step_ecr() {
  say "[ECR] $ECR_REPO"
  local uri registry tag digest src_digest
  if uri=$(st_repo); then say "  저장소: 있음"
  else
    aws_w ecr create-repository --repository-name "$ECR_REPO" --image-tag-mutability MUTABLE >/dev/null
    say "  저장소: 만듦"
  fi
  registry="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"
  uri="$registry/$ECR_REPO"
  if [ -z "$SOURCE_IMAGE" ]; then
    digest=$(QUIET_ERR=1 aws_r ecr describe-images --repository-name "$ECR_REPO" --filter tagStatus=TAGGED \
      --query 'sort_by(imageDetails, &imagePushedAt)[-1].imageDigest' --output text || true)
    if [[ $digest =~ ^sha256:[0-9a-f]{64}$ ]]; then
      ECR_IMAGE="$uri@$digest"; say "  --source-image 없음 → 저장소의 가장 최근 이미지 $(mask "$ECR_IMAGE")"
    else
      warn "--source-image 가 없고 저장소에 이미지도 없다 → 이미지 복사와 .env 의 SIMAI_GPU_AWS_IMAGE·ENABLED 는 건너뜀"
    fi
    return
  fi
  src_digest=${SOURCE_IMAGE##*@sha256:}
  tag="src-${src_digest:0:12}"
  digest=$(QUIET_ERR=1 aws_r ecr describe-images --repository-name "$ECR_REPO" --image-ids "imageTag=$tag" \
    --query 'imageDetails[0].imageDigest' --output text || true)
  if [[ $digest =~ ^sha256:[0-9a-f]{64}$ ]]; then
    ECR_IMAGE="$uri@$digest"; say "  이미지 $tag: 이미 복사됨"; return
  fi
  if dry; then
    say "[dry-run] docker pull $SOURCE_IMAGE → tag $(mask "$uri:$tag") → push" >&2
    ECR_IMAGE="$uri@sha256:DRYRUN"; return
  fi
  # 로그인 비밀번호는 파이프로만(명령줄·화면에 없음)
  aws "${AWS_BASE[@]}" ecr get-login-password 2> >(mask_stream >&2) | tr -d '\r\n' | docker login --username AWS --password-stdin "$registry" 2>&1 | mask_stream
  docker pull "$SOURCE_IMAGE" 2>&1 | mask_stream
  docker tag "$SOURCE_IMAGE" "$uri:$tag"
  # push 에는 시간 제한을 걸지 않는다(큰 이미지, 끊기면 처음부터)
  docker push "$uri:$tag" 2>&1 | mask_stream
  digest=$(aws_r ecr describe-images --repository-name "$ECR_REPO" --image-ids "imageTag=$tag" \
    --query 'imageDetails[0].imageDigest' --output text)
  [[ $digest =~ ^sha256:[0-9a-f]{64}$ ]] || die "push 뒤 ECR 다이제스트를 못 읽었다"
  ECR_IMAGE="$uri@$digest"
  say "  이미지 복사: $(mask "$ECR_IMAGE")"
}

# 비밀 하나를 한 줄로 읽는다: 파일이 있으면 첫 줄, 없으면 터미널에서 보이지 않게
read_secret_line() {
  local file=$1 prompt=$2 var=$3 value=""
  if [ -n "$file" ]; then
    [ -r "$file" ] || die "파일을 못 읽는다: $file"
    IFS= read -r value <"$file" || true
  else
    [ -t 0 ] || die "$prompt: 터미널이 아니다 → 파일 옵션으로 준다"
    IFS= read -rs -p "$prompt (화면에 보이지 않음): " value; printf '\n' >&2
  fi
  value=${value%$'\r'}
  [ -n "$value" ] || die "$prompt 가 비었다"
  printf -v "$var" '%s' "$value"
}

build_secret_json() {
  local out=$1 boot hf
  [ -n "$TLS_KEY" ] && [ -n "$TLS_CERT" ] || die "비밀을 만들려면 --tls-key·--tls-cert 가 필요하다(파일 경로)"
  [ -r "$TLS_KEY" ] && grep -q -- '-----BEGIN' "$TLS_KEY" || die "--tls-key 가 PEM 이 아니다: $TLS_KEY"
  [ -r "$TLS_CERT" ] && grep -q -- '-----BEGIN CERTIFICATE' "$TLS_CERT" || die "--tls-cert 가 인증서 PEM 이 아니다: $TLS_CERT"
  read_secret_line "$BOOT_SECRET_FILE" "부팅 비밀값(SIMAI_GPU_BOOT_SECRET)" boot
  read_secret_line "$HF_TOKEN_FILE" "HF 토큰(HF_TOKEN)" hf
  ( umask 077
    {
      printf '{"SIMAI_GPU_BOOT_SECRET":"'; printf '%s' "$boot" | json_esc
      printf '","SIMAI_TIER0_TLS_PRIVATE_KEY":"'; json_esc tail <"$TLS_KEY"
      printf '","HF_TOKEN":"'; printf '%s' "$hf" | json_esc
      printf '","SIMAI_TIER0_TLS_CERT_PEM":"'; json_esc tail <"$TLS_CERT"
      printf '"}'
    } >"$out" )
  boot=""; hf=""
}

step_secret() {
  say "[Secrets Manager] $SECRET_ID"
  local new="$WORK/secret.json" cur="$WORK/secret.cur"
  if st_secret >/dev/null; then
    if [ $UPDATE_SECRET = 0 ]; then say "  비밀: 있음, 건너뜀(바꾸려면 --update-secret)"; return; fi
    build_secret_json "$new"
    ( umask 077; aws "${AWS_BASE[@]}" secretsmanager get-secret-value --secret-id "$SECRET_ID" \
        --query SecretString --output text >"$cur" 2>/dev/null ) || : >"$cur"
    if [ "$(tr -d '\r\n' <"$cur")" = "$(tr -d '\r\n' <"$new")" ]; then
      say "  비밀: 같음, 건너뜀"
    else
      aws_w secretsmanager put-secret-value --secret-id "$SECRET_ID" --secret-string "$(file_url "$new")" >/dev/null
      say "  비밀: 바뀜 → 새 판"
    fi
  else
    if dry && [ -z "$BOOT_SECRET_FILE$HF_TOKEN_FILE" ]; then
      say "[dry-run] 부팅 비밀값·HF 토큰을 묻고 aws secretsmanager create-secret --name $SECRET_ID --secret-string file://<임시>" >&2
      return
    fi
    build_secret_json "$new"
    aws_w secretsmanager create-secret --name "$SECRET_ID" --description "simai GPU boot secrets (S15P31A101-703)" \
      --secret-string "$(file_url "$new")" >/dev/null
    say "  비밀: 만듦(키 SIMAI_GPU_BOOT_SECRET, SIMAI_TIER0_TLS_PRIVATE_KEY, HF_TOKEN, SIMAI_TIER0_TLS_CERT_PEM)"
  fi
  rm -f "$new" "$cur"
}

LT_ID=""
lt_data() {
  cat <<EOF
{
  "ImageId": "$AMI_ID",
  "KeyName": "$KEY_NAME",
  "IamInstanceProfile": { "Name": "$ROLE" },
  "SecurityGroupIds": ["$SG_ID"],
  "BlockDeviceMappings": [ { "DeviceName": "${AMI_ROOT:-/dev/sda1}",
    "Ebs": { "VolumeSize": 200, "VolumeType": "gp3", "DeleteOnTermination": true } } ],
  "MetadataOptions": { "HttpEndpoint": "enabled", "HttpTokens": "required", "HttpPutResponseHopLimit": 2 }
}
EOF
}

# 지금 기본 판이 원하는 값을 모두 갖고 네트워크 인터페이스·인스턴스 종류·user-data 가 없는지
lt_matches() {
  local cur atom
  cur=$(aws_r ec2 describe-launch-template-versions --launch-template-id "$LT_ID" --versions '$Default' \
    --query 'LaunchTemplateVersions[0].LaunchTemplateData' --output json | strip_ws) || return 1
  for atom in "\"ImageId\":\"$AMI_ID\"" "\"KeyName\":\"$KEY_NAME\"" "\"Name\":\"$ROLE\"" "\"$SG_ID\"" \
              "\"DeviceName\":\"${AMI_ROOT:-/dev/sda1}\"" '"VolumeSize":200' '"VolumeType":"gp3"' '"DeleteOnTermination":true' \
              '"HttpTokens":"required"' '"HttpPutResponseHopLimit":2'; do
    case "$cur" in *"$atom"*) ;; *) return 1 ;; esac
  done
  case "$cur" in *'"NetworkInterfaces"'*|*'"InstanceType"'*|*'"UserData"'*) return 1 ;; esac
  return 0
}

step_launch_template() {
  say "[시작 템플릿] $LT_NAME"
  [ -n "$AMI_ID" ] || die "AMI 를 모른다"
  local data="$WORK/lt.json" ver
  lt_data >"$data"
  if LT_ID=$(st_lt); then
    if lt_matches; then say "  시작 템플릿: 같음 $LT_ID, 건너뜀"; return; fi
    if dry; then aws_w ec2 create-launch-template-version --launch-template-id "$LT_ID" --launch-template-data "$(file_url "$data")"; return; fi
    ver=$(aws_w ec2 create-launch-template-version --launch-template-id "$LT_ID" \
      --launch-template-data "$(file_url "$data")" --query LaunchTemplateVersion.VersionNumber --output text)
    aws_w ec2 modify-launch-template --launch-template-id "$LT_ID" --default-version "$ver" >/dev/null
    say "  시작 템플릿: 바뀜 → 새 판 $ver(기본)"
  else
    if dry; then aws_w ec2 create-launch-template --launch-template-name "$LT_NAME" --launch-template-data "$(file_url "$data")"; LT_ID=lt-DRYRUN; return; fi
    LT_ID=$(aws_w ec2 create-launch-template --launch-template-name "$LT_NAME" \
      --launch-template-data "$(file_url "$data")" --query LaunchTemplate.LaunchTemplateId --output text)
    say "  시작 템플릿: 만듦 $LT_ID"
  fi
}

ENV_LINES="$WORK/env.lines"   # EC2 .env 에 쓸 KEY=VALUE(비밀 포함, 600)
NEW_KEY_ID=""

step_launcher_user() {
  say "[IAM 사용자] $LAUNCHER_USER"
  local doc="$WORK/launcher-policy.json" arn="arn:aws:iam::$ACCOUNT:policy/$LAUNCHER_POLICY" attached keys n pair
  launcher_policy_doc "$LT_ID" >"$doc"
  ensure_policy "$LAUNCHER_POLICY" "$doc"
  if st_user >/dev/null; then say "  사용자: 있음"
  else aws_w iam create-user --user-name "$LAUNCHER_USER" >/dev/null; say "  사용자: 만듦"; fi
  attached=$(QUIET_ERR=1 aws_r iam list-attached-user-policies --user-name "$LAUNCHER_USER" \
    --query "AttachedPolicies[?PolicyArn=='$arn'].PolicyArn" --output text || true)
  if [ "$attached" = "$arn" ]; then say "  정책 붙임: 있음, 건너뜀"
  else aws_w iam attach-user-policy --user-name "$LAUNCHER_USER" --policy-arn "$arn" >/dev/null; say "  정책 붙임: 붙임"; fi
  keys=$(QUIET_ERR=1 aws_r iam list-access-keys --user-name "$LAUNCHER_USER" --query 'AccessKeyMetadata[].AccessKeyId' --output text || true)
  n=$(printf '%s' "$keys" | tr '\t' '\n' | grep -c . || true)
  if [ "$n" -gt 0 ] && [ $ROTATE_KEY = 0 ]; then
    say "  액세스 키: ${n}개 있음, 건너뜀(EC2 .env 에 있다고 본다. 다시 만들려면 --rotate-key)"
    return
  fi
  [ "$n" -lt 2 ] || die "액세스 키가 이미 2개다 → 안 쓰는 키를 IAM 콘솔에서 지운 뒤 다시"
  if dry; then say "[dry-run] aws iam create-access-key --user-name $LAUNCHER_USER → EC2 .env(ssh stdin)" >&2; return; fi
  # 응답은 변수로만 받고 출력하지 않는다
  pair=$(aws "${AWS_BASE[@]}" iam create-access-key --user-name "$LAUNCHER_USER" \
    --query 'AccessKey.[AccessKeyId,SecretAccessKey]' --output text 2>"$WORK/aws.err" | tr -d '\r') || {
    mask_stream <"$WORK/aws.err" >&2; die "액세스 키를 못 만들었다"; }
  NEW_KEY_ID=${pair%%$'\t'*}
  ( umask 077
    printf 'SIMAI_GPU_AWS_ACCESS_KEY_ID=%s\n' "$NEW_KEY_ID" >>"$ENV_LINES"
    printf 'SIMAI_GPU_AWS_SECRET_ACCESS_KEY=%s\n' "${pair#*$'\t'}" >>"$ENV_LINES" )
  pair=""
  say "  액세스 키: 새로 만듦(…${NEW_KEY_ID: -4}), 값은 EC2 .env 로만 보낸다"
  [ "$n" -eq 0 ] || say "  예전 키 $n개는 서버 재시작 확인 뒤 IAM 콘솔에서 지운다"
}

# EC2 .env 에 KEY=VALUE 를 넣는다(있는 줄은 바꾸고 없으면 덧붙임, 백업 .env.bak-<시각>). 값은 stdin 으로만
REMOTE_MERGE='set -eu
f=$2
[ -f "$f" ] || { echo "$f 가 없다" >&2; exit 3; }
umask 077
cp -p "$f" "$f.bak-$1"
tmp=$(mktemp "$f.tmp.XXXXXX")
awk '"'"'NR == FNR { i = index($0, "="); if (i > 1) { k = substr($0, 1, i - 1); v[k] = $0; order[++n] = k } next }
  { i = index($0, "="); k = (i > 1) ? substr($0, 1, i - 1) : ""
    if (k != "" && (k in v)) { if (!(k in done)) print v[k]; done[k] = 1; next }
    print }
  END { for (j = 1; j <= n; j++) if (!(order[j] in done)) { print v[order[j]]; done[order[j]] = 1 } }'"'"' - "$f" >"$tmp"
cat "$tmp" >"$f"
rm -f "$tmp"'

step_ec2_env() {
  say "[EC2 .env] $EC2_HOST:$REMOTE_ENV"
  if [ -n "$ECR_IMAGE" ]; then
    local want_enabled="SIMAI_GPU_AWS_ENABLED=true" want_image="SIMAI_GPU_AWS_IMAGE=$ECR_IMAGE" cur=""
    if [ "$ECR_IMAGE" = "${ECR_IMAGE%DRYRUN}" ]; then
      cur=$(ssh -o BatchMode=yes "$EC2_HOST" "sudo -n grep -E '^(SIMAI_GPU_AWS_ENABLED|SIMAI_GPU_AWS_IMAGE)=' $REMOTE_ENV" 2>/dev/null | tr -d '\r' || true)
    fi
    if printf '%s\n' "$cur" | grep -qxF "$want_enabled" && printf '%s\n' "$cur" | grep -qxF "$want_image" && [ ! -s "$ENV_LINES" ]; then
      say "  SIMAI_GPU_AWS_ENABLED·SIMAI_GPU_AWS_IMAGE: 같음, 건너뜀"
      return
    fi
    ( umask 077; printf '%s\n%s\n' "$want_enabled" "$want_image" >>"$ENV_LINES" )
  fi
  [ -s "$ENV_LINES" ] || { say "  쓸 줄 없음"; return; }
  local keys ts
  keys=$(cut -d= -f1 <"$ENV_LINES" | tr '\n' ' ')
  if dry; then say "[dry-run] ssh $EC2_HOST (sudo) $REMOTE_ENV 에 $keys(백업 .env.bak-<시각>)" >&2; return; fi
  ts=$(date -u +%Y%m%dT%H%M%SZ)
  if ssh -o BatchMode=yes "$EC2_HOST" "sudo -n bash -c $(printf '%q' "$REMOTE_MERGE") _ $ts $REMOTE_ENV" <"$ENV_LINES"; then
    say "  썼다: $keys(백업 $REMOTE_ENV.bak-$ts)"
  else
    [ -z "$NEW_KEY_ID" ] || say "  새 액세스 키(…${NEW_KEY_ID: -4})가 EC2 에 들어가지 못했다 → IAM 콘솔에서 그 키를 지우고 --rotate-key 로 다시"
    die "EC2 .env 쓰기 실패"
  fi
  rm -f "$ENV_LINES"
}

run_apply() {
  say "== 적용$( dry && printf ' (dry-run: 쓰기 없음)') =="
  discover_account
  discover_quota
  discover_azs
  discover_subnets
  discover_ami
  [ -n "$AMI_ID" ] || die "AMI 를 못 찾아 멈춘다"
  [ ${#AZS[@]} -gt 0 ] || die "$INSTANCE_TYPE 가용 영역이 없어 멈춘다"
  [ -n "$VPC_ID" ] || die "기본 VPC 가 없어 멈춘다(이 스크립트는 기본 VPC 만 다룬다)"
  say ""
  step_instance_role
  step_security_group
  step_key_pair
  step_ecr
  step_secret
  step_launch_template
  step_launcher_user
  step_ec2_env
  say ""
  say "== 끝 =="
  say "ECR 이미지        ${ECR_IMAGE:-(없음)}"
  say "시작 템플릿 ID    ${LT_ID:-?}"
  say "보안 그룹         ${SG_ID:-?}"
  say "가용 영역         ${AZS[*]}"
  say ""
  say "화면 「GPU 설정」에 넣을 값"
  say "  공급자            AWS"
  say "  선호 인스턴스 종류 $INSTANCE_TYPE"
  say "  선호 가용 영역    ${PREFERRED_AZ:-${AZS[0]:-?}}"
  say "  시작 템플릿 ID    ${LT_ID:-?}"
  say "  서브넷·보안 그룹  비움(기본 VPC·시작 템플릿)"
  say "  온디맨드/스팟     온디맨드"
  say ""
  say "서버 재시작은 하지 않았다. Jenkins 배포 중이 아닐 때(「배포 완료」 로그 뒤) 다음 배포 또는 EC2 에서"
  say "  docker compose --env-file /opt/simai/.env -f /opt/simai/repo/deploy/ec2/compose.yaml up -d server"
  say "로 .env 를 다시 읽힌다. 실패 $FAILS, 주의 $WARNS"
}

case "$CMD" in
  check) run_check ;;
  apply) run_apply ;;
esac
