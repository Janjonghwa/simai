#!/usr/bin/env bash
# 로비 게이트웨이 노트북 원격 제어 CLI (개발 노트북 Git Bash, S15P31A101-394·414·424).
# EC2 역터널(ssh simai-gw)을 통해 게이트웨이의 simai-gateway.ps1 명령을 호출한다.
set -euo pipefail

GW_REPO='C:\simai\S15P31A101'
PS1_PATH="${GW_REPO}\\gateway\\ops\\simai-gateway.ps1"
GW_SSH_HOST="${GW_SSH_HOST:-simai-gw}"
EC2_SSH_HOST="${EC2_SSH_HOST:-simai-ec2}"

show_help() {
  cat <<'EOF'
사용법: gateway/ops/gw.sh <명령> [인자...]

명령:
  status                                  게이트웨이 전체 상태(모드·창·녹화·sshd·역터널·카메라·커밋·디스크·절전·heal 로그)
  live <PodIP> <17000 매핑> <17001 매핑> [--no-record] [--no-remote-cctv]
                                          관제 시작(run-remote: 터널·경보 단말·영상 중계·동시 녹화 및 카메라 헬퍼) 후 status
                                          (live·preview·stop 은 수동 명령이라 자동 전환이 새 GPU 세션 등록 전까지 덮어쓰지 않는다)
  preview                                 카메라 미리보기와 헬퍼(preview-remote: Tier 0 전송 없음) 후 status
  stop [--no-upload]                      관제·미리보기 끄기 후 status. 관제를 끄면 그날 작업 구간 녹화를 S3 로 백그라운드 업로드(-668)
  upload                                  녹화 작업 구간 업로드를 지금 백그라운드로 시작(upload-recordings) 후 status
  update                                  원격 갱신(update-remote: git pull --ff-only + uv sync 후 이전 모드 복원)
  heal                                    자가 복구 즉시 실행(sshd·역터널·절전·미리보기 점검·복구) 후 status
  logs <relay|helper|tunnel|update|heal|upload|auto> [N]  지정한 로그의 마지막 N줄 출력(기본 40줄)
  auto [status|on|off|once]               GPU 세션 자동 전환(-701) 상태·켜기(수동 우선도 풂)·끄기(지금 모드 그대로)·한 번 판단 후 status
  restart-tunnel                          역터널 재시작(EC2 옛 포트 정리 free-ports.sh + 예약 작업 재시작) 후 status
  ssh                                     게이트웨이 대화형 SSH 셸 접속
  help                                    이 도움말 출력 (인자 없이 실행해도 출력)
EOF
}

diagnose_connection() {
  echo "" >&2
  echo "[gw] 게이트웨이($GW_SSH_HOST) SSH 연결 실패 — EC2($EC2_SSH_HOST) 역터널 상태를 진단합니다(읽기 전용)..." >&2
  local ec2_out=""
  local ec2_rc=0
  ec2_out=$(ssh -o BatchMode=yes -o ConnectTimeout=10 "$EC2_SSH_HOST" '
    ss_line=$(sudo -n ss -ltnpH "sport = :2222" 2>/dev/null || ss -ltnH "sport = :2222" 2>/dev/null || true)
    if [ -z "$ss_line" ]; then
      echo "LISTEN=0"
    else
      pid=$(printf "%s\n" "$ss_line" | grep -o "pid=[0-9]*" | head -n1 | cut -d= -f2)
      age=""
      start_time=""
      if [ -n "$pid" ]; then
        age=$(ps -o etime= -p "$pid" 2>/dev/null | tr -d " " || true)
        start_time=$(ps -o lstart= -p "$pid" 2>/dev/null || true)
      fi
      echo "LISTEN=1"
      echo "PID=${pid:-알수없음}"
      echo "AGE=${age:-알수없음}"
      echo "START=${start_time:-알수없음}"
      printf "RAW=%s\n" "$ss_line"
    fi
  ' 2>/dev/null) || ec2_rc=$?

  if [ "$ec2_rc" -ne 0 ]; then
    echo "[진단] EC2($EC2_SSH_HOST) SSH 연결 자체가 실패했습니다(코드 $ec2_rc)." >&2
    echo "  → 개발 노트북 인터넷/망 연결, ~/.ssh/config 의 $EC2_SSH_HOST 설정, ~/.ssh/K15A101T.pem 키를 확인하세요." >&2
    return
  fi

  local listen pid age start_time raw
  listen=$(printf "%s\n" "$ec2_out" | sed -n 's/^LISTEN=//p' | head -n1)
  pid=$(printf "%s\n" "$ec2_out" | sed -n 's/^PID=//p' | head -n1)
  age=$(printf "%s\n" "$ec2_out" | sed -n 's/^AGE=//p' | head -n1)
  start_time=$(printf "%s\n" "$ec2_out" | sed -n 's/^START=//p' | head -n1)
  raw=$(printf "%s\n" "$ec2_out" | sed -n 's/^RAW=//p' | head -n1)

  if [ "$listen" = "0" ]; then
    echo "[진단] EC2 127.0.0.1:2222 리슨 없음 → 게이트웨이 역터널이 닫혀 있습니다." >&2
    echo "  → 원인: 게이트웨이 역터널 예약 작업(simai-gateway-remote) 중단, 노트북 절전·전원 꺼짐·로그아웃·네트워크 끊김." >&2
    echo "  → 조치: heal 예약 작업이 켜져 있으면 최대 5분 안에 역터널을 다시 시작합니다. 계속 안 열리면 현장에서 전원·로그인·랜선을 확인하거나 메뉴 6(원격 열기 설정)을 실행하세요." >&2
  else
    echo "[진단] EC2 127.0.0.1:2222 리슨 있음 (EC2 sshd PID=${pid}, 세션 나이=${age}, 시작=${start_time})" >&2
    [ -n "$raw" ] && echo "  소켓: $raw" >&2
    echo "  → 원인 1: 터널은 있는데 게이트웨이 sshd 무응답(종료됨, kex_exchange_identification: Connection closed 등) → 현장에서 메뉴 6 또는 heal 대기(5분)." >&2
    echo "  → 원인 2: 게이트웨이 망이 끊기면서 EC2 에 옛 sshd 세션(나이 ${age})이 2222 포트를 쥔 채 남음 → 게이트웨이 재연결(free-ports.sh) 또는 heal 대기(5분)." >&2
  fi
}

gw() {
  local rc=0
  ssh -o BatchMode=yes -o ConnectTimeout=15 "$GW_SSH_HOST" \
    "chcp 65001 >nul & powershell -NoProfile -ExecutionPolicy Bypass -File $PS1_PATH $*" || rc=$?
  if [ "$rc" -ne 0 ]; then
    if [ "$rc" -eq 255 ]; then
      diagnose_connection
    fi
    return "$rc"
  fi
}

if [ $# -eq 0 ]; then
  show_help
  exit 0
fi

cmd="$1"
case "$cmd" in
  help|-h|--help)
    show_help
    ;;
  status)
    gw status
    ;;
  live)
    [ $# -ge 4 ] || { echo "사용: gw.sh live <Pod IP> <17000 매핑> <17001 매핑> [--no-record|-NoRecord] [--no-remote-cctv|-NoRemoteCctv]" >&2; exit 2; }
    pod_ip="$2"
    ctrl_port="$3"
    video_port="$4"
    shift 4
    extra_args=()
    for arg in "$@"; do
      case "$arg" in
        --no-record|-NoRecord)
          extra_args+=("-NoRecord")
          ;;
        --no-remote-cctv|-NoRemoteCctv)
          extra_args+=("-NoRemoteCctv")
          ;;
        *)
          echo "알 수 없는 live 옵션: $arg (사용: gw.sh live <Pod IP> <17000 매핑> <17001 매핑> [--no-record] [--no-remote-cctv])" >&2
          exit 2
          ;;
      esac
    done
    gw run-remote -PodIp "$pod_ip" -ControlPort "$ctrl_port" -VideoPort "$video_port" "${extra_args[@]}"
    gw status
    ;;
  preview)
    gw preview-remote
    gw status
    ;;
  stop)
    if [ "${2:-}" = "--no-upload" ] || [ "${2:-}" = "-NoRecordingUpload" ]; then gw stop -NoRecordingUpload; else gw stop; fi
    gw status
    ;;
  update)
    # S15P31A101-414 뒤: update-remote 가 로그인 세션 예약 작업으로 git pull·uv sync 를 하고 이전 모드를 되살린다
    # (SSH 세션은 Windows 자격 증명 저장소를 못 써 setup 의 git pull 이 실패한다).
    gw update-remote
    gw status
    ;;
  heal)
    gw heal
    gw status
    ;;
  logs)
    [ $# -ge 2 ] || { echo "사용: gw.sh logs <relay|helper|tunnel|update|heal|upload|auto> [줄수(기본 40)]" >&2; exit 2; }
    lines="${3:-40}"
    gw logs "$2" -Lines "$lines"
    ;;
  restart-tunnel)
    gw restart-tunnel
    gw status
    ;;
  auto)
    sub="${2:-status}"
    case "$sub" in
      status|on|off|once) ;;
      *) echo "사용: gw.sh auto [status|on|off|once]" >&2; exit 2 ;;
    esac
    gw auto "$sub"
    [ "$sub" = "status" ] || gw status
    ;;
  upload)
    gw upload-recordings
    gw status
    ;;
  ssh)
    rc=0
    ssh -o ConnectTimeout=15 "$GW_SSH_HOST" || rc=$?
    if [ "$rc" -eq 255 ]; then
      diagnose_connection
    fi
    exit "$rc"
    ;;
  *)
    show_help >&2
    exit 2
    ;;
esac
