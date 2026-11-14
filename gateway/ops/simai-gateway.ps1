# simai 로비 게이트웨이 노트북 도우미 (Windows PowerShell 5.1 이상, S15P31A101-393 / S15P31A101-424). 설명은 같은 폴더 README.md.
# 더블클릭: simai-gateway.cmd  →  메뉴(1 설치·갱신 · 2 점검 · 3 관제 시작 · 4 관제 끄기 · 5 미리보기만 · 6~8 원격 · 9 상태 · h 자가 복구)
# 명령줄: simai-gateway.cmd run -PodIp 1.2.3.4 -ControlPort 40001 -VideoPort 40002   (CCTV 원격 시청 업로드를 끄려면 -NoRemoteCctv)
# 원격(S15P31A101-394·424): 게이트웨이 remote-setup(관리자 1회) · 개발 노트북 gw.sh (status/live/preview/stop/update/heal/logs/restart-tunnel/upload/ssh)
# 녹화 업로드(S15P31A101-693): 관제 시작 때 작업자를 띄우고 웹에서 끝난 작업의 닫힌 파일만 S3에 백그라운드로 올린다. 끄려면 -NoRecordingUpload 또는 .env SIMAI_RECORDING_UPLOAD=off
# GPU 세션 자동 전환(S15P31A101-701): 예약 작업 simai-gateway-auto 가 기기 토큰으로 EC2 를 15초마다 조회해 팀원이 켠 GPU 세션이 준비되면 관제로,
#   세션이 끝나거나 5분 넘게 안 보이면 미리보기로 바꾼다. 수동 명령이 우선한다. 끄기 auto off(또는 .env SIMAI_GATEWAY_AUTO_SWITCH=off), 상태 auto status
# 비밀 값(.env·토큰·키)은 화면에 찍지 않는다. 키 이름만 확인한다.
param(
  [Parameter(Position = 0)]
  [ValidateSet('menu', 'setup', 'check', 'run', 'stop', 'preview', 'remote-setup', 'run-remote', 'preview-remote', 'update', 'update-remote', 'status', 'dev-remote', 'rdp', 'heal', 'logs', 'restart-tunnel', 'upload-recordings', 'upload-worker', 'auto')][string]$Command = 'menu',
  [Parameter(Position = 1)]
  [string]$LogKind = '',       # logs 대상: relay | helper | tunnel | update | heal | upload | auto. auto 의 하위 명령: status | on | off | once | worker
  [Parameter(Position = 2)]
  [int]$Lines = 40,            # logs 출력 줄 수(기본 40)
  [string]$PodIp = '',
  [int]$ControlPort = 0,
  [int]$VideoPort = 0,
  [string]$Root = '',          # 비우면: 이 스크립트가 저장소 안이면 그 저장소, 아니면 C:\simai\S15P31A101
  [string]$RelayConfig = '',   # 비우면 <저장소>\gateway\relay\configs\lobby.toml
  [string]$Bundle = '',        # 비우면 스크립트 옆 bundle\ 또는 C:\simai-gateway\bundle\
  [string]$LocalDir = 'C:\simai-local',
  # 관제(run)·미리보기(preview)는 기본으로 CCTV 원격 시청 업로드(중계 --remote-cctv, 보는 사람이 있을 때만 EC2 로)를 켠다.
  # 관제 중 Tier 0 지연이 늘면 이 스위치로 끈다(S15P31A101-400). 로컬 미리보기(127.0.0.1)는 그대로다
  [switch]$NoRemoteCctv,
  # 관제(run, run-remote)는 기본으로 C:\simai-local\recordings\live-<yyyyMMdd-HHmmss> 에 중계 송신 프레임을 그대로 녹화한다(S15P31A101-554).
  # 녹화를 끄려면 -NoRecord 를 준다. 미리보기(preview, preview-remote)는 녹화하지 않는다.
  [switch]$NoRecord,
  [string]$RecordDir = '',
  # 관제를 끈 뒤(stop) 녹화의 작업 구간 업로드를 이번만 띄우지 않는다(S15P31A101-668). 늘 끄려면 .env SIMAI_RECORDING_UPLOAD=off
  [switch]$NoRecordingUpload,
  # 자동 전환(S15P31A101-701)이 띄운 run·preview 라는 표시(모드 주인 auto). 사람이 친 명령은 주인 manual 이 되어 자동 전환이 덮어쓰지 않는다
  [switch]$Auto,
  # heal·update-remote 가 이전 모드를 되살릴 때: 모드 주인을 바꾸지 않는다
  [switch]$KeepOwner
)
$ErrorActionPreference = 'Stop'
$Utf8 = New-Object System.Text.UTF8Encoding $false
try { [Console]::OutputEncoding = $Utf8; $OutputEncoding = $Utf8 } catch { }

$RemoteCctvArg = if ($NoRemoteCctv) { '' } else { ' --remote-cctv' }
$RemoteCctvLabel = if ($NoRemoteCctv) { '원격 시청 끔' } else { '원격 시청 켬' }
$RecordingsRoot = Join-Path $LocalDir 'recordings'
$RecordDirFile = Join-Path $LocalDir 'gateway-record-dir.txt'
$RepoUrl = 'https://lab.ssafy.com/s15-final/S15P31A101.git'
$InRepo = Test-Path (Join-Path $PSScriptRoot '..\..\AGENTS.md')
if (-not $Root) {
  if ($InRepo) { $Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path } else { $Root = 'C:\simai\S15P31A101' }
}
if (-not $Bundle) {
  $Bundle = Join-Path $PSScriptRoot 'bundle'
  if (-not (Test-Path $Bundle)) { $Bundle = 'C:\simai-gateway\bundle' }
}
$RelayCfg = $RelayConfig
if (-not $RelayCfg) { $RelayCfg = Join-Path $Root 'gateway\relay\configs\lobby.toml' }
$StateFile = Join-Path $LocalDir 'gateway-pod.json'
$PidFile = Join-Path $LocalDir 'gateway-pids.json'
$ModeFile = Join-Path $LocalDir 'gateway-mode.txt'   # 지금 모드(관제·미리보기), 끄면 지운다
$GatewayUserFile = Join-Path $LocalDir 'gateway-user.txt'
$RelayLog = Join-Path $LocalDir 'gateway-relay.log'
$TunnelLog = Join-Path $LocalDir 'gateway-remote-tunnel.log'
$UpdateLog = Join-Path $LocalDir 'gateway-update.log'
$HealLog = Join-Path $LocalDir 'gateway-heal.log'
$SshDir = Join-Path $env:USERPROFILE '.ssh'
$Pem = Join-Path $SshDir 'K15A101T.pem'
$RelayDir = Join-Path $Root 'gateway\relay'
$AlertDir = Join-Path $Root 'gateway\site_alert'
$HelperDir = Join-Path $Root 'gateway\camera_helper'
$HelperPidFile = Join-Path $LocalDir 'gateway-helper.pid'
$HelperLog = Join-Path $LocalDir 'gateway-helper.log'
$UploadPidFile = Join-Path $LocalDir 'gateway-upload.pid'
$UploadLog = Join-Path $LocalDir 'gateway-upload.log'
$UploadTask = 'simai-gateway-upload'
$AutoStateFile = Join-Path $LocalDir 'gateway-auto.json'        # 자동 전환 마지막 판단(상태 표시·다음 판단의 유예 기준)
$AutoOffFile = Join-Path $LocalDir 'gateway-auto-off.txt'       # 있으면 자동 전환 끔(auto off)
$OwnerFile = Join-Path $LocalDir 'gateway-mode-owner.json'      # 지금 모드를 누가 정했나(auto | manual)
$AutoLog = Join-Path $LocalDir 'gateway-auto.log'
$AutoTask = 'simai-gateway-auto'
$AutoPollSeconds = 15            # EC2 조회 주기(설계 문서 근거)
$AutoGraceSecondsDefault = 300   # 관제 대상이 안 보여도 미리보기로 되돌리지 않는 유예(설계 문서 근거). .env SIMAI_GATEWAY_AUTO_REVERT_SECONDS, 60 미만은 60
$AutoReportSeconds = 300         # 판단이 그대로여도 EC2 에 다시 알리는 주기(EC2 재배포로 메모리가 비면 다시 채운다)
$AutoMaxFailures = 3             # 30분 안에 전환이 이만큼 실패하면 잠시 멈춘다(heal 의 되살리기 상한과 같은 수)
$TunnelControlPort = 17000       # Do-Run 터널이 여는 로컬 포트(EC2 터널 방식에서 tier0_endpoint 가 가리키는 곳)
$TunnelVideoPort = 17001

function Say($m) { Write-Host "[gateway] $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "[gateway] 주의: $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "[gateway] 실패: $m" -ForegroundColor Red; throw $m }
function Need($exe) {
  if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { Fail "$exe 가 PATH 에 없다. 설치한 뒤 새 창에서 다시 실행한다." }
}
function Test-Tcp($h, $p, $ms = 1500) {
  $c = New-Object System.Net.Sockets.TcpClient
  try { $a = $c.BeginConnect($h, $p, $null, $null); return ($a.AsyncWaitHandle.WaitOne($ms) -and $c.Connected) }
  catch { return $false } finally { $c.Close() }
}
function Get-EnvKeys {
  $p = Join-Path $Root '.env'
  if (-not (Test-Path $p)) { return @() }
  $keys = @()
  foreach ($line in [IO.File]::ReadAllLines($p)) { if ($line -match '^\s*([A-Za-z0-9_]+)\s*=\s*\S') { $keys += $Matches[1] } }
  return $keys
}
function Rotate-LogIfNeeded([string]$path, [long]$maxBytes = 1048576) {
  if ((Test-Path $path) -and ((Get-Item $path).Length -gt $maxBytes)) {
    Move-Item -LiteralPath $path -Destination "$path.old" -Force
  }
}

# ---------- 설치·갱신 ----------
function Install-Bundle {
  if (-not (Test-Path $Bundle)) { Say "옮길 비밀 묶음이 없다($Bundle). .env·키가 이미 있으면 괜찮다."; return }
  $envSrc = Join-Path $Bundle 'gateway.env'
  if (Test-Path $envSrc) {
    $dst = Join-Path $Root '.env'
    $new = [ordered]@{}
    foreach ($line in [IO.File]::ReadAllLines($envSrc)) { if ($line -match '^\s*([A-Za-z0-9_]+)\s*=') { $new[$Matches[1]] = $line } }
    $keep = @()
    if (Test-Path $dst) {
      foreach ($line in [IO.File]::ReadAllLines($dst)) {
        if ($line -match '^\s*([A-Za-z0-9_]+)\s*=' -and $new.Contains($Matches[1])) { continue }
        $keep += $line
      }
    }
    [IO.File]::WriteAllLines($dst, [string[]](@($keep) + @($new.Values)), $Utf8)
    Say ".env 에 키 $($new.Count)개를 넣었다(값은 표시하지 않음)"
  }
  $pemSrc = Join-Path $Bundle 'K15A101T.pem'
  if (Test-Path $pemSrc) {
    Copy-Item $pemSrc $Pem -Force
    icacls $Pem /inheritance:r /grant:r "$($env:USERNAME):R" | Out-Null
    Say "EC2 키 → $Pem (본인만 읽기)"
  }
  $a = Read-Host "비밀 묶음($Bundle, 토큰·키 포함)을 지울까? [Y/n]"
  if ($a -ne 'n') { Remove-Item $Bundle -Recurse -Force; Say '묶음을 지웠다. USB 사본도 지운다.' }
}

function Ensure-SshConfig {
  $cfg = Join-Path $SshDir 'config'
  $text = ''
  if (Test-Path $cfg) { $text = [IO.File]::ReadAllText($cfg) }
  if ($text -match '(?m)^\s*Host\s+simai-ec2\s*$') { Say 'SSH 설정 simai-ec2 있음'; return }
  $block = "`r`nHost simai-ec2`r`n    HostName simai.live`r`n    User ubuntu`r`n    IdentityFile ~/.ssh/K15A101T.pem`r`n    IdentitiesOnly yes`r`n    ServerAliveInterval 15`r`n    ServerAliveCountMax 3`r`n"
  [IO.File]::AppendAllText($cfg, $block, $Utf8)
  Say 'SSH 설정 simai-ec2 추가'
}

function Check-Env {
  $keys = Get-EnvKeys
  $miss = @('SIMAI_EC2_BASE_URL', 'SIMAI_DEVICE_TOKEN', 'SIMAI_TIER0_PINNED_SPKI_SHA256', 'SIMAI_LAPTOP_ID') | Where-Object { $keys -notcontains $_ }
  foreach ($c in 'A', 'B', 'C', 'D') {
    if (($keys -notcontains "RTSP_PASS_CAM_$c") -and ($keys -notcontains 'RTSP_PASS')) { $miss += "RTSP_PASS_CAM_$c" }
  }
  if ($miss.Count) { Warn ".env 에 빠진 키: $($miss -join ', ')" } else { Say '.env 키 확인 끝' }
  if (-not (Test-Path $RelayCfg)) { Warn "카메라 설정이 없다: $RelayCfg" }
  if (-not (Test-Path $Pem)) { Warn "EC2 키가 없다: $Pem" }
}

function Sync-Clock {
  if (-not (Test-Admin)) { Warn '관리자 권한이 아니라 시계 동기화를 건너뛴다. 관리자 PowerShell 에서: w32tm /resync'; return }
  w32tm /resync | Out-Null
  if ($LASTEXITCODE) {
    w32tm /config /manualpeerlist:time.windows.com /syncfromflags:manual /update | Out-Null
    w32tm /resync | Out-Null
  }
  if ($LASTEXITCODE) { Warn '시계 동기화 실패(w32tm /resync 를 다시 해 본다)' } else { Say '시계 동기화 끝' }
}

function Ensure-Shortcut {
  $target = Join-Path $Root 'gateway\ops\simai-gateway.cmd'
  if (-not (Test-Path $target)) { return }
  $lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) 'simai 게이트웨이.lnk'
  $sh = New-Object -ComObject WScript.Shell
  $s = $sh.CreateShortcut($lnk); $s.TargetPath = $target; $s.WorkingDirectory = (Split-Path $target); $s.Save()
  Say "바탕화면 바로가기: $lnk (다음부터는 이것으로 연다)"
}

function Do-Setup {
  Need git; Need uv; Need ssh
  if (Test-Path $PidFile) { Fail '관제가 켜져 있다. 4(관제 끄기) 뒤에 갱신한다(Windows 는 실행 중 파일이 잠겨 설치가 실패한다).' }
  Stop-AutoTask   # 설치 중 자동 전환이 관제를 띄우면 uv sync 가 잠긴 파일로 실패한다. 끝에 다시 등록·시작한다
  New-Item -ItemType Directory -Force (Split-Path $Root), $LocalDir, $SshDir | Out-Null
  Save-GatewayUser
  $selfBefore = $null
  if ($InRepo) { $selfBefore = (Get-FileHash $PSCommandPath).Hash }
  if (-not (Test-Path (Join-Path $Root '.git'))) {
    Say "저장소 받기 → $Root (GitLab 로그인 창이 뜨면 본인 계정으로)"
    git clone --branch dev $RepoUrl $Root
    if ($LASTEXITCODE) { Fail 'git clone' }
  } else {
    $dirty = git -C $Root status --porcelain --untracked-files=no
    if ($dirty) { Warn '저장소에 고친 파일이 있어 갱신을 건너뛴다(게이트웨이에서는 코드를 고치지 않는다)' }
    else {
      git -C $Root fetch origin; git -C $Root checkout dev; git -C $Root pull --ff-only origin dev
      if ($LASTEXITCODE) { Fail 'git pull' }
      Say "저장소 갱신: $(git -C $Root log --oneline -1)"
    }
  }
  foreach ($d in $RelayDir, $AlertDir, $HelperDir) {
    Say "의존성 설치: $d"
    Push-Location $d
    try { uv sync; if ($LASTEXITCODE) { Fail "uv sync ($d)" } } finally { Pop-Location }
  }
  Install-Bundle
  Ensure-SshConfig
  Check-Env
  Sync-Clock
  Register-HealTask
  Register-AutoTask
  Start-AutoTask
  Ensure-Shortcut
  Say '설치·갱신 끝. 처음이면 다음은 2(점검).'
  if ($InRepo -and ((Get-FileHash $PSCommandPath).Hash -ne $selfBefore)) {
    Warn '이 도우미 스크립트도 새 판으로 바뀌었다. 창을 닫고 바로가기로 다시 연다.'
    exit 0
  }
}

# ---------- 점검 ----------
function Do-Check {
  Need uv; Need ssh
  Check-Env
  Say 'EC2 SSH 확인'
  ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new simai-ec2 echo ok
  if ($LASTEXITCODE) { Fail 'EC2 SSH 실패: 망(공유기 WAN), 키, SSH 설정을 확인한다' }
  if (Test-Path $RelayCfg) {
    $hosts = Select-String -Path $RelayCfg -Pattern '^\s*host\s*=\s*"([^"]+)"' | ForEach-Object { $_.Matches[0].Groups[1].Value }
    foreach ($h in $hosts) { if (Test-Tcp $h 554) { Say "카메라 $h RTSP 554 열림" } else { Warn "카메라 $h RTSP 554 응답 없음(전원·Wi-Fi 확인)" } }
  }
  Say '카메라 4대 30초 수신 시험(전송 없음)'
  Push-Location $RelayDir
  try { uv run simai-relay run --config $RelayCfg --seconds 30 } finally { Pop-Location }
  Say '경보 단말 오프라인 시험 창을 연다. 시험 경보 소리·화면을 확인하고 창을 닫는다.'
  Start-Process powershell -WorkingDirectory $AlertDir -ArgumentList '-NoExit', '-Command', "`$Host.UI.RawUI.WindowTitle='simai 경보 시험'; uv run simai-site-alert run --offline"
}

# ---------- 관제 시작·끄기 ----------
function Load-State {
  $s = @{ PodIp = ''; ControlPort = 0; VideoPort = 0 }
  if (Test-Path $StateFile) {
    $j = Get-Content $StateFile -Raw | ConvertFrom-Json
    $s.PodIp = [string]$j.PodIp; $s.ControlPort = [int]$j.ControlPort; $s.VideoPort = [int]$j.VideoPort
  }
  return $s
}

function Start-CameraHelper {
  if (Test-CameraHelper) { return }
  Rotate-LogIfNeeded $HelperLog
  $cmd = "`$env:PYTHONUTF8='1'; uv run simai-camera-helper-service *>> '$HelperLog'"
  $proc = Start-Process powershell -PassThru -WindowStyle Hidden -WorkingDirectory $HelperDir -ArgumentList '-NoProfile', '-Command', $cmd
  Set-Content -LiteralPath $HelperPidFile -Value $proc.Id -Encoding ascii
}
function Test-CameraHelper {
  if (-not (Test-Path $HelperPidFile)) { return $false }
  $helperId = 0
  if (-not [int]::TryParse((Get-Content $HelperPidFile -Raw).Trim(), [ref]$helperId)) { return $false }
  $proc = Get-Process -Id $helperId -ErrorAction SilentlyContinue
  return [bool]($proc -and $proc.ProcessName -match '^(powershell|pwsh)$')
}
function Stop-CameraHelper {
  if (Test-CameraHelper) { taskkill /PID ([int](Get-Content $HelperPidFile -Raw)) /T /F | Out-Null }
  Remove-Item $HelperPidFile -Force -ErrorAction SilentlyContinue
}

function Finalize-RecordingManifest([string]$recDir = '') {
  $target = $recDir
  if ((-not $target) -and (Test-Path $RecordDirFile)) {
    $target = ([IO.File]::ReadAllText($RecordDirFile, $Utf8)).Trim()
  }
  if ((-not $target) -or (-not (Test-Path (Join-Path $target 'session.json')))) { return }
  if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { return }
  $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
  Push-Location $RelayDir
  try {
    uv run python -c "import sys; from pathlib import Path; from simai_recorder.manifest import finalize_stopped_session; finalize_stopped_session(Path(sys.argv[1]))" "$target" 2>$null | Out-Null
  } catch { } finally {
    Pop-Location
    $ErrorActionPreference = $old
  }
}

# 관제를 끈 뒤 녹화 업로드를 띄울지(S15P31A101-668). 관제(run) 모드를 끌 때만이고 미리보기·켜 둔 것 없음이면 띄우지 않는다
function Test-UploadAfterStop([string]$ModeText, [switch]$NoUpload) {
  if ($NoUpload) { return $false }
  return [bool]($ModeText -and $ModeText.Trim().StartsWith('관제'))
}

function Do-Stop([switch]$Quiet, [switch]$NoUpload) {
  Stop-CameraHelper
  $modeText = if (Test-Path $ModeFile) { (Get-Content $ModeFile -Raw -Encoding UTF8).Trim() } else { '' }
  $upload = Test-UploadAfterStop -ModeText $modeText -NoUpload:($NoUpload -or $NoRecordingUpload)
  if (-not (Test-Path $PidFile)) {
    # PID 기록이 없으면 작성자 종료를 증명할 수 없어 열린 파일은 확정하지 않는다.
    Remove-Item $ModeFile -Force -ErrorAction SilentlyContinue
    if (-not $Quiet) { Say '켜 둔 관제 창이 없다' }
    if ($upload) { Start-RecordingUpload }
    return
  }
  foreach ($p in (Get-Content $PidFile -Raw | ConvertFrom-Json)) {
    if (Get-Process -Id $p -ErrorAction SilentlyContinue) {
      taskkill /PID $p /T /F | Out-Null
      # taskkill 은 종료를 요청만 한다. 프로세스 트리가 실제로 끝날 때까지 잠시 기다린 뒤 판정한다(바로 보면 종료 중인 창을 실패로 본다)
      # 녹화(4대) 트리는 10초를 조금 넘겨 끝나는 일이 잦아(11-14 갱신에서 두 번) 30초까지 기다린다
      $deadline = (Get-Date).AddSeconds(30)
      while ((Get-Process -Id $p -ErrorAction SilentlyContinue) -and ((Get-Date) -lt $deadline)) { Start-Sleep -Milliseconds 200 }
      if (Get-Process -Id $p -ErrorAction SilentlyContinue) { Fail '녹화 프로세스를 종료하지 못해 파일 확정을 미룬다' }
    }
  }
  Finalize-RecordingManifest
  Remove-Item $PidFile -Force
  Remove-Item $ModeFile -Force -ErrorAction SilentlyContinue
  if (-not $Quiet) { Say '관제·미리보기 창을 껐다' }
  if ($upload) { Start-RecordingUpload }
}

# ---------- 녹화 작업 구간 업로드 (S15P31A101-668, 설계 docs/design/S15P31A101-668-recording-s3-playback.md) ----------
# 기존 기기 REST를 10초 간격으로 조회한다. 종료 작업의 닫힌 파일만 올린다. 중계 venv 의 simai_recorder(경로 의존)로 C:\simai-local\recordings\live-* 중 끝나지 않은 녹화를 이어 올린다.
# 상시 소켓은 추가하지 않는다. 관제가 끝나고 남은 업로드가 없으면 작업자를 종료한다.
function Get-RecordingUploadArgs {
  return @('run', 'python', '-m', 'simai_recorder', 'upload', '--root', $RecordingsRoot, '--mode-file', $ModeFile,
    '--env-file', (Join-Path $Root '.env'))
}
function Test-RecordingUpload {
  if (-not (Test-Path $UploadPidFile)) { return $false }
  $uploadId = 0
  if (-not [int]::TryParse((Get-Content $UploadPidFile -Raw).Trim(), [ref]$uploadId)) { return $false }
  $proc = Get-Process -Id $uploadId -ErrorAction SilentlyContinue
  return [bool]($proc -and $proc.ProcessName -match '^(powershell|pwsh)$')
}
function Stop-RecordingUpload {
  if (Test-RecordingUpload) {
    taskkill /PID ([int](Get-Content $UploadPidFile -Raw)) /T /F | Out-Null
    Say '녹화 업로드를 멈췄다(다음 관제 종료 때 이어서)'
  }
  Remove-Item $UploadPidFile -Force -ErrorAction SilentlyContinue
}
function Start-RecordingUpload {
  if (Test-RecordingUpload) { Say '녹화 업로드가 이미 돌고 있다'; return }
  if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Warn 'uv 가 없어 녹화 업로드를 띄우지 않았다'; return }
  $scriptPath = Join-Path $Root 'gateway\ops\simai-gateway.ps1'
  if (-not (Test-Path $scriptPath)) { $scriptPath = $PSCommandPath }
  if ($env:SSH_CONNECTION) {
    # ssh 세션에서 띄운 프로세스는 세션이 끝나면 함께 죽는다 → 로그인 사용자 세션의 예약 작업으로 띄운다(Start-InSession 과 같은 이유)
    $arg = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$scriptPath`" upload-worker"
    $act = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg
    $pri = New-ScheduledTaskPrincipal -UserId (Get-GatewayUser) -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $UploadTask -Action $act -Principal $pri -Force | Out-Null
    Start-ScheduledTask -TaskName $UploadTask
  } else {
    Start-Process powershell -WindowStyle Hidden -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $scriptPath, 'upload-worker' | Out-Null
  }
  Say "녹화 작업 구간 업로드를 백그라운드로 시작했다(로그 $UploadLog, gw.sh logs upload)"
}
function Do-UploadWorker {
  $uploadMutex = [Threading.Mutex]::new($false, 'Local\SimaiRecordingUpload')
  try { $acquired = $uploadMutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $acquired = $true }
  if (-not $acquired) { $uploadMutex.Dispose(); return }
  New-Item -ItemType Directory -Force $LocalDir | Out-Null
  Set-Content -LiteralPath $UploadPidFile -Value $PID -Encoding ascii
  Rotate-LogIfNeeded $UploadLog
  [IO.File]::AppendAllText($UploadLog, ("=== {0:yyyy-MM-dd HH:mm:ss} 녹화 업로드 ===`r`n" -f (Get-Date)), $Utf8)
  $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
  Push-Location $RelayDir
  try {
    $env:PYTHONUTF8 = '1'
    $uploadArgs = Get-RecordingUploadArgs
    do {
      & uv @uploadArgs 2>&1 | ForEach-Object { Add-Content -LiteralPath $UploadLog -Value "$_" -Encoding UTF8 }
      $code = $LASTEXITCODE
      if ($code -eq 2) { break }
      $monitoring = (Test-Path $ModeFile) -and (Get-Content $ModeFile -Raw -Encoding UTF8).Trim().StartsWith('관제')
      if (-not $monitoring -and $code -eq 0) { break }
      Start-Sleep -Seconds 10
    } while ($true)
    Add-Content -LiteralPath $UploadLog -Value "=== 끝(코드 $code) ===" -Encoding UTF8
  } finally {
    Pop-Location
    $ErrorActionPreference = $old
    Remove-Item $UploadPidFile -Force -ErrorAction SilentlyContinue
    $uploadMutex.ReleaseMutex()
    $uploadMutex.Dispose()
  }
}

function Build-RelayCommand([string]$title, [string]$sendArg, [string]$recordDir = '') {
  Rotate-LogIfNeeded $RelayLog
  [IO.File]::AppendAllText($RelayLog, ("=== {0:yyyy-MM-dd HH:mm:ss} $title ===`r`n" -f (Get-Date)), $Utf8)
  $recArg = if ($recordDir) { " --record-dir '$recordDir'" } else { '' }
  return "`$Host.UI.RawUI.WindowTitle='$title'; `$ErrorActionPreference='Continue'; `$env:PYTHONUTF8='1'; uv run simai-relay run --config '$RelayCfg'$sendArg --preview-port 47032$RemoteCctvArg$recArg 2>&1 | ForEach-Object { `$l = `"`$_`"; Write-Host `$l; Add-Content -LiteralPath '$RelayLog' -Value `$l -Encoding UTF8 }"
}

function Do-Run {
  Need uv; Need ssh
  New-Item -ItemType Directory -Force $LocalDir | Out-Null
  $s = Load-State
  if ($PodIp) { $s.PodIp = $PodIp }
  if ($ControlPort) { $s.ControlPort = $ControlPort }
  if ($VideoPort) { $s.VideoPort = $VideoPort }
  if ($s.PodIp -and -not $PodIp) {
    $a = Read-Host "지난 Pod $($s.PodIp) 제어 $($s.ControlPort) 영상 $($s.VideoPort) 를 쓸까? [Y/n]"
    if ($a -eq 'n') { $s.PodIp = ''; $s.ControlPort = 0; $s.VideoPort = 0 }
  }
  if (-not $s.PodIp) { $s.PodIp = (Read-Host 'Pod 공개 IP').Trim() }
  if (-not $s.ControlPort) { $s.ControlPort = [int](Read-Host 'Pod 17000 의 바깥 매핑 포트') }
  if (-not $s.VideoPort) { $s.VideoPort = [int](Read-Host 'Pod 17001 의 바깥 매핑 포트') }
  # 이전 세션을 정리한 뒤 종료 작업 업로더를 하나만 유지한다.
  # Pod 주소는 이전 창을 끈 뒤에 쓴다: 끄기가 실패하면(녹화 확정 보류) 상태 파일이 아직 도는 이전 관제를 가리켜야 heal·자동 전환이 헷갈리지 않는다(S15P31A101-701)
  Do-Stop -Quiet -NoUpload
  ($s | ConvertTo-Json) | Set-Content $StateFile -Encoding ascii
  if ((Test-Tcp '127.0.0.1' 17000 300) -or (Test-Tcp '127.0.0.1' 17001 300)) { Fail '127.0.0.1:17000/17001 을 이미 다른 프로그램이 쓰고 있다(예전 터널 창을 닫는다)' }

  # EC2 까지만 SSH 하나, EC2→Pod 는 연결마다 따로 TCP(카메라별 독립 연결, 명세 12.2). TLS 는 Tier 0 가 끝낸다.
  $fw = "-L 127.0.0.1:17000:$($s.PodIp):$($s.ControlPort) -L 127.0.0.1:17001:$($s.PodIp):$($s.VideoPort)"
  $tunnel = "`$Host.UI.RawUI.WindowTitle='simai 터널(EC2 경유)'; while (`$true) { Write-Host ('[터널] 연결 ' + (Get-Date -Format HH:mm:ss)); ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 simai-ec2 $fw; Write-Host '[터널] 끊김, 3초 뒤 다시'; Start-Sleep 3 }"
  $p1 = Start-Process powershell -PassThru -ArgumentList '-NoExit', '-Command', $tunnel
  $ok = $false
  for ($i = 0; $i -lt 20; $i++) { Start-Sleep -Milliseconds 500; if (Test-Tcp '127.0.0.1' 17000 300) { $ok = $true; break } }
  if (-not $ok) { Warn '터널이 10초 안에 열리지 않았다. 터널 창의 오류를 본다(계속 다시 시도한다).' } else { Say '터널 열림' }

  $recDir = ''
  if (-not $NoRecord) {
    $recDir = if ($RecordDir) { $RecordDir } else { Join-Path $RecordingsRoot ("live-{0:yyyyMMdd-HHmmss}" -f (Get-Date)) }
    New-Item -ItemType Directory -Force (Split-Path -Parent $recDir) | Out-Null
    [IO.File]::WriteAllText($RecordDirFile, $recDir, $Utf8)
  } else {
    Remove-Item $RecordDirFile -Force -ErrorAction SilentlyContinue
  }
  $recordLabel = if ($NoRecord) { '녹화 끔' } else { "녹화 $recDir" }

  $p2 = Start-Process powershell -PassThru -WorkingDirectory $AlertDir -ArgumentList '-NoExit', '-Command', "`$Host.UI.RawUI.WindowTitle='simai 현장 경보 단말'; uv run simai-site-alert run"
  Start-Sleep 3
  $relayCmd = Build-RelayCommand 'simai 영상 중계' ' --send' $recDir
  $p3 = Start-Process powershell -PassThru -WorkingDirectory $RelayDir -ArgumentList '-NoExit', '-Command', $relayCmd
  Start-CameraHelper
  (@($p1.Id, $p2.Id, $p3.Id) | ConvertTo-Json) | Set-Content $PidFile -Encoding ascii
  "관제 Pod $($s.PodIp) 제어 $($s.ControlPort) 영상 $($s.VideoPort), $RemoteCctvLabel, $recordLabel ($(Get-Date -Format 'MM-dd HH:mm'))" | Set-Content $ModeFile -Encoding utf8
  if (-not $NoRecordingUpload) { Start-RecordingUpload }
  Say "관제 시작: 창 3개(터널·경보 단말·영상 중계), CCTV $RemoteCctvLabel, $recordLabel. 끌 때는 메뉴 4"
}

function Do-Preview {
  # GPU Pod 없이 카메라 → 관제 앱 CCTV 미리보기만(Tier 0 로 보내지 않음). 같은 노트북 브라우저는 로컬(127.0.0.1)로,
  # 노트북 밖 관리자는 원격 시청(EC2 경유, 보는 사람이 있을 때만 올림)으로 simai.live 관제 화면에서 본다.
  Need uv
  New-Item -ItemType Directory -Force $LocalDir | Out-Null
  Do-Stop -Quiet
  $relayCmd = Build-RelayCommand 'simai 카메라 미리보기(전송 없음)' ''
  $p = Start-Process powershell -PassThru -WorkingDirectory $RelayDir -ArgumentList '-NoExit', '-Command', $relayCmd
  Start-CameraHelper
  (@($p.Id) | ConvertTo-Json) | Set-Content $PidFile -Encoding ascii
  "미리보기(전송 없음), $RemoteCctvLabel ($(Get-Date -Format 'MM-dd HH:mm'))" | Set-Content $ModeFile -Encoding utf8
  Say "미리보기 시작(전송 없음, CCTV $RemoteCctvLabel). Pod 가 뜨면 3(관제 시작)으로 바꾼다. 끌 때는 4"
}

# ---------- 원격·자가 복구 (S15P31A101-394 / S15P31A101-424) ----------
# 게이트웨이가 EC2 로 역터널을 걸어 둔다: EC2 의 127.0.0.1:2222 → 게이트웨이 sshd, 127.0.0.1:13389 → 게이트웨이 원격 데스크톱.
# EC2 sshd 는 GatewayPorts 기본값(no)이라 EC2 루프백에만 열린다. 개발 노트북은 EC2 를 거쳐(ProxyJump·로컬 포워드) 들어온다.
$RemoteDir = '~/simai-gateway'           # EC2 ubuntu 홈 아래. 공개키·게이트웨이 사용자 이름·포트 정리 스크립트만 둔다(비밀 아님)
$TunnelScript = Join-Path $LocalDir 'gateway-remote-tunnel.ps1'
$TunnelTask = 'simai-gateway-remote'
$RunTask = 'simai-gateway-run'
$HealTask = 'simai-gateway-heal'

function Test-Admin {
  return ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
function Save-GatewayUser {
  if ($env:USERNAME -and ($env:USERNAME -ne 'SYSTEM') -and ($env:USERNAME -notlike '*$')) {
    New-Item -ItemType Directory -Force $LocalDir | Out-Null
    [IO.File]::WriteAllText($GatewayUserFile, $env:USERNAME.Trim(), $Utf8)
  }
}
function Get-GatewayUser {
  if (Test-Path $GatewayUserFile) {
    $u = ([IO.File]::ReadAllText($GatewayUserFile, $Utf8)).Trim()
    if ($u -and ($u -ne 'SYSTEM')) { return $u }
  }
  $t = Get-ScheduledTask -TaskName $TunnelTask -ErrorAction SilentlyContinue
  if ($t -and $t.Principal.UserId) {
    $tu = ($t.Principal.UserId -split '\\')[-1].Trim()
    if ($tu -and ($tu -ne 'SYSTEM')) { return $tu }
  }
  if ($env:USERNAME -and ($env:USERNAME -ne 'SYSTEM') -and ($env:USERNAME -notlike '*$')) {
    return $env:USERNAME
  }
  return 'SSAFY'
}
function To-B64([string]$s) { return [Convert]::ToBase64String($Utf8.GetBytes(($s -replace "`r", ''))) }
function Ec2([string]$cmd) {
  $out = ssh -o BatchMode=yes -o ConnectTimeout=10 simai-ec2 $cmd
  if ($LASTEXITCODE) { Fail "EC2 명령 실패: $cmd" }
  return $out
}

$OpenSshMsiUrl = 'https://github.com/PowerShell/Win32-OpenSSH/releases/download/10.0.0.0p2-Preview/OpenSSH-Win64-v10.0.0.0.msi'
$OpenSshMsiSha256 = 'DDEC9C53864280759CF9F74791CEFD387100E3946AA849A1C138A4ED1B96B7D9'   # GitHub 릴리스 자산 digest
function Install-OpenSshServer {
  Say 'OpenSSH 서버 설치(공식 Win32-OpenSSH MSI, 1~2분)'
  $msi = Join-Path $env:TEMP 'OpenSSH-Win64-v10.0.0.0.msi'
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  $oldPp = $ProgressPreference; $ProgressPreference = 'SilentlyContinue'   # PS 5.1 진행 표시는 내려받기를 크게 늦춘다
  try { Invoke-WebRequest -UseBasicParsing -Uri $OpenSshMsiUrl -OutFile $msi } finally { $ProgressPreference = $oldPp }
  $fs = [IO.File]::OpenRead($msi)
  try { $hash = -join ([Security.Cryptography.SHA256]::Create().ComputeHash($fs) | ForEach-Object { $_.ToString('X2') }) } finally { $fs.Dispose() }
  if ($hash -ne $OpenSshMsiSha256) { Remove-Item $msi -Force; Fail 'OpenSSH MSI 해시가 다르다(설치하지 않음)' }
  $p = Start-Process msiexec.exe -ArgumentList "/i `"$msi`" /qn ADDLOCAL=Server" -Wait -PassThru
  Remove-Item $msi -Force -ErrorAction SilentlyContinue
  if ($p.ExitCode -ne 0 -and $p.ExitCode -ne 3010) { Fail "OpenSSH MSI 설치 실패(msiexec 코드 $($p.ExitCode))" }
  if (-not (Get-Service sshd -ErrorAction SilentlyContinue)) { Fail 'OpenSSH MSI 설치 뒤에도 sshd 서비스가 없다' }
}

function Test-SshdFailureConfigured([string]$qfText = '') {
  if (-not $qfText) {
    $qfText = (@(sc.exe qfailure sshd 2>$null) -join "`n")
  }
  return (($qfText -match '(?i)86400') -and (([regex]::Matches($qfText, '(?i)RESTART')).Count -ge 3))
}

function Set-SshdFailureOptions {
  sc.exe failure sshd reset= 86400 actions= restart/5000/restart/5000/restart/60000 | Out-Null
}

function Get-PowerCfgAcIndex([string]$sub, [string]$setting) {
  $out = @(powercfg /qh SCHEME_CURRENT $sub $setting 2>$null)
  foreach ($line in $out) {
    if ($line -match 'AC[^:]*:\s*0x([0-9a-fA-F]+)') { return [int]"0x$($Matches[1])" }
  }
  return -1
}

function Get-PowerCfgStatus {
  $mon = Get-PowerCfgAcIndex 'SUB_VIDEO' 'VIDEOIDLE'
  $slp = Get-PowerCfgAcIndex 'SUB_SLEEP' 'STANDBYIDLE'
  $lid = Get-PowerCfgAcIndex 'SUB_BUTTONS' 'LIDACTION'
  $ok = ($mon -eq 0) -and ($slp -eq 0) -and ($lid -eq 0)
  return @{ Ok = $ok; MonitorAc = $mon; StandbyAc = $slp; LidAc = $lid }
}

function Set-GatewayPowerCfg {
  powercfg /change monitor-timeout-ac 0 | Out-Null
  powercfg /change standby-timeout-ac 0 | Out-Null
  powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0 | Out-Null
  powercfg /setactive SCHEME_CURRENT | Out-Null
}

function Register-HealTask {
  Save-GatewayUser
  if (-not (Test-Admin)) {
    Warn "관리자 권한이 아니라 자가 복구 예약 작업($HealTask) 등록을 건너뛴다(관리자 권한으로 1 또는 6 실행)."
    return
  }
  $scriptPath = Join-Path $Root 'gateway\ops\simai-gateway.ps1'
  if (-not (Test-Path $scriptPath)) { $scriptPath = $PSCommandPath }
  $arg = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$scriptPath`" heal"
  $act = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg
  $tBoot = New-ScheduledTaskTrigger -AtStartup
  $tLogon = New-ScheduledTaskTrigger -AtLogOn
  $tRep = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
  $pri = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
  $set = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 4) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
  Register-ScheduledTask -TaskName $HealTask -Action $act -Trigger @($tBoot, $tLogon, $tRep) -Principal $pri -Settings $set -Force | Out-Null
  Say "자가 복구 예약 작업 등록·갱신: $HealTask (부팅·로그온·5분마다, SYSTEM)"
}

function Do-RemoteSetup {
  if (-not (Test-Admin)) { Fail '관리자 권한으로 연다(simai-gateway.cmd 우클릭 → 관리자 권한으로 실행)' }
  Need ssh
  New-Item -ItemType Directory -Force $LocalDir | Out-Null
  Save-GatewayUser
  # 1) OpenSSH 서버: 127.0.0.1 에서만 듣고 키 인증만, 실패 시 자동 재시작 설정
  #    Windows 기능(Add-WindowsCapability)은 SSAFY 관리 PC 에서 20분 넘게 멈춘다(S15P31A101-398).
  #    sshd 서비스가 없을 때만 공식 Win32-OpenSSH MSI(버전·해시 고정)의 서버 기능만 설치한다.
  if (-not (Get-Service sshd -ErrorAction SilentlyContinue)) { Install-OpenSshServer }
  Set-Service sshd -StartupType Automatic
  Set-SshdFailureOptions
  Start-Service sshd
  $cfg = 'C:\ProgramData\ssh\sshd_config'
  for ($i = 0; $i -lt 10 -and -not (Test-Path $cfg); $i++) { Start-Sleep -Seconds 1 }   # 첫 시작 때 기본 설정 파일이 생긴다
  $text = [IO.File]::ReadAllText($cfg)
  $mark = '# simai-gateway (S15P31A101-394)'
  if (-not $text.Contains($mark)) {
    # sshd 는 처음 나온 값을 쓰므로 맨 앞에 둔다(Match 블록·기본 설정은 건드리지 않음)
    $head = "$mark`r`nListenAddress 127.0.0.1`r`nPasswordAuthentication no`r`nPubkeyAuthentication yes`r`n`r`n"
    [IO.File]::WriteAllText($cfg, $head + $text, (New-Object System.Text.UTF8Encoding $false))
  }
  Restart-Service sshd
  Say 'OpenSSH 서버: 127.0.0.1 전용, 키 인증만, 실패 시 자동 재시작(5s/5s/60s)'

  # 2) 개발 노트북 공개키(개발 노트북 dev-remote 가 EC2 에 올려 둔 것)
  $keys = @(Ec2 "cat $RemoteDir/dev_keys.pub 2>/dev/null; true") | Where-Object { $_ -match '^(ssh-ed25519|ssh-rsa|ecdsa-sha2-\S+) \S+' }
  $ak = 'C:\ProgramData\ssh\administrators_authorized_keys'    # 관리자 계정은 이 파일만 본다(Windows OpenSSH)
  if ($keys.Count) {
    [IO.File]::WriteAllLines($ak, [string[]]$keys, $Utf8)
    icacls $ak /inheritance:r /grant '*S-1-5-32-544:F' /grant '*S-1-5-18:F' | Out-Null
    Say "개발 노트북 공개키 $($keys.Count)개 등록"
  } else {
    Warn '개발 노트북 공개키가 EC2 에 아직 없다. 개발 노트북에서 7(원격 준비)을 한 뒤 6 을 다시 한다.'
  }
  $gwUser = Get-GatewayUser
  Ec2 "mkdir -p $RemoteDir; echo $(To-B64 $gwUser) | base64 -d > $RemoteDir/gateway_user" | Out-Null

  # 3) 원격 데스크톱 켜기 + 절전 해제(AC 화면·절전·덮개 동작 0)
  Set-ItemProperty 'HKLM:\System\CurrentControlSet\Control\Terminal Server' -Name fDenyTSConnections -Value 0
  if ((Get-ItemProperty 'HKLM:\System\CurrentControlSet\Control\Terminal Server').fDenyTSConnections -ne 0) { Warn '원격 데스크톱이 정책으로 막혀 있다(SSH 는 된다)' } else { Say '원격 데스크톱 켬' }
  Set-GatewayPowerCfg
  Say '절전 설정 해제(AC 화면 끄기 0, 절전 0, 덮개 닫기: 아무 것도 안 함)'

  # 4) EC2 에 옛 역터널 포트 정리 스크립트(끊긴 세션이 포트를 쥐고 있으면 새 역터널이 실패한다)
  #    다른 sshd 세션(같은 ubuntu 라도 privsep 자식)의 PID 는 sudo 없이 ss -p 에 안 보여 옛 세션이 2222 를 쥔 채 남았다(10차 세션) → sudo -n(EC2 ubuntu 는 비밀번호 없는 sudo, S15P31A101-414)
  $free = "#!/bin/sh`n# simai gateway reverse tunnel: kill stale sshd holding 127.0.0.1:2222/13389 (S15P31A101-394, sudo -n S15P31A101-414)`nfor p in 2222 13389; do`n  for pid in `$(sudo -n ss -ltnpH `"sport = :`$p`" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u); do sudo -n kill `"`$pid`" 2>/dev/null; done`ndone`nexit 0`n"
  Ec2 "echo $(To-B64 $free) | base64 -d > $RemoteDir/free-ports.sh; chmod +x $RemoteDir/free-ports.sh" | Out-Null

  # 5) 역터널: 로그인 때 숨김 창으로 뜨고 끊기면 다시 붙는다
  $tunnel = @'
# simai 게이트웨이 원격 역터널 (S15P31A101-394). 예약 작업 simai-gateway-remote 가 로그인 때 띄운다. 도우미 6 이 다시 쓴다.
$log = Join-Path $PSScriptRoot 'gateway-remote-tunnel.log'
while ($true) {
  if ((Test-Path $log) -and ((Get-Item $log).Length -gt 1048576)) { Move-Item -LiteralPath $log -Destination "$log.old" -Force }
  Add-Content $log ("{0:yyyy-MM-dd HH:mm:ss} 연결" -f (Get-Date)) -Encoding UTF8
  $t0 = Get-Date
  ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -R 127.0.0.1:2222:127.0.0.1:22 -R 127.0.0.1:13389:127.0.0.1:3389 simai-ec2
  Add-Content $log ("{0:yyyy-MM-dd HH:mm:ss} 끊김(코드 {1})" -f (Get-Date), $LASTEXITCODE) -Encoding UTF8
  if (((Get-Date) - $t0).TotalSeconds -lt 20) { ssh -o BatchMode=yes -o ConnectTimeout=10 simai-ec2 '~/simai-gateway/free-ports.sh' }
  Start-Sleep 5
}
'@
  [IO.File]::WriteAllText($TunnelScript, $tunnel, (New-Object System.Text.UTF8Encoding $true))
  $act = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$TunnelScript`""
  $trg = New-ScheduledTaskTrigger -AtLogOn -User $gwUser
  $pri = New-ScheduledTaskPrincipal -UserId $gwUser -LogonType Interactive -RunLevel Limited
  $set = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
  Register-ScheduledTask -TaskName $TunnelTask -Action $act -Trigger $trg -Principal $pri -Settings $set -Force | Out-Null
  Stop-ScheduledTask -TaskName $TunnelTask -ErrorAction SilentlyContinue
  Start-ScheduledTask -TaskName $TunnelTask
  # 6) 자가 복구 감시 예약 작업(부팅·로그온·5분마다)
  Register-HealTask
  # 7) GPU 세션 자동 전환 작업자(로그인 때, S15P31A101-701)
  Register-AutoTask
  Start-AutoTask
  Start-Sleep 5
  $on = Ec2 "ss -ltnH 'sport = :2222' | wc -l"
  if ([int]($on | Select-Object -Last 1) -ge 1) { Say '역터널 열림(EC2 127.0.0.1:2222·13389). 개발 노트북에서 gw.sh status / ssh simai-gw / 8(원격 데스크톱)' }
  else { Warn "역터널이 아직 안 열렸다. 로그: $TunnelLog" }
  Warn '게이트웨이는 전원 연결·절전 해제·Windows 로그인 상태로 둔다(역터널은 로그인 때 뜬다).'
}

function Start-InSession([string]$modeArgs, [string]$label) {
  # SSH 세션이나 SYSTEM 예약 작업에서 띄운 창은 게이트웨이 화면에 안 보이거나 세션 종료 시 죽는다 → 로그인한 사용자 세션의 예약 작업으로 띄운다.
  $gwUser = Get-GatewayUser
  $scriptPath = Join-Path $Root 'gateway\ops\simai-gateway.ps1'
  if (-not (Test-Path $scriptPath)) { $scriptPath = $PSCommandPath }
  $arg = "-NoProfile -WindowStyle Minimized -ExecutionPolicy Bypass -File `"$scriptPath`" $modeArgs"
  $act = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg
  $pri = New-ScheduledTaskPrincipal -UserId $gwUser -LogonType Interactive -RunLevel Limited
  Register-ScheduledTask -TaskName $RunTask -Action $act -Principal $pri -Force | Out-Null
  $t0 = Get-Date
  Start-ScheduledTask -TaskName $RunTask
  $fresh = $false
  for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 500
    if ((Test-Path $PidFile) -and (Get-Item $PidFile).LastWriteTime -ge $t0) { $fresh = $true; break }
  }
  if ($fresh) { Say "게이트웨이 화면에서 $label 시작. PID $((Get-Content $PidFile -Raw | ConvertFrom-Json) -join ', ')" }
  else { Warn '20초 안에 시작 기록이 없다. 게이트웨이에 사용자가 로그인돼 있는지, 원격 데스크톱으로 화면을 본다.' }
}

function Do-RunRemote {
  if (-not $PodIp -or -not $ControlPort -or -not $VideoPort) { Fail 'run-remote 는 -PodIp -ControlPort -VideoPort 를 모두 준다' }
  $extra = ''
  if ($NoRemoteCctv) { $extra += ' -NoRemoteCctv' }
  if ($NoRecord) { $extra += ' -NoRecord' }
  if ($RecordDir) { $extra += " -RecordDir `"$RecordDir`"" }
  Start-InSession "run -PodIp $PodIp -ControlPort $ControlPort -VideoPort $VideoPort$extra" '관제(창 3개)'
}

function Do-PreviewRemote {
  $extra = if ($NoRemoteCctv) { ' -NoRemoteCctv' } else { '' }
  Start-InSession "preview$extra" '카메라 미리보기'
}

# ---------- 원격 갱신 (S15P31A101-414) ----------
# ssh simai-gw 세션은 Windows 자격 증명 관리자(wincredman)를 못 써 setup 의 git pull 이 "could not read Username" 으로 실패한다(10차 세션).
# update-remote 는 지금 모드를 멈추고, 로그인 세션 예약 작업으로 update(git pull --ff-only + uv sync)를 돌려 로그 파일의 exit 코드를 기다린 뒤 이전 모드로 되돌린다.
$UpdateTask = 'simai-gateway-update'

function Log-Update([string]$m) { [IO.File]::AppendAllText($UpdateLog, ("{0:yyyy-MM-dd HH:mm:ss} {1}`r`n" -f (Get-Date), $m), $Utf8) }

function Invoke-Logged([string]$what, [scriptblock]$sb) {
  Log-Update "> $what"
  $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'   # git 은 진행 상황을 stderr 로 쓴다(5.1 은 2>&1 을 오류로 바꾼다)
  try { $out = @(& $sb 2>&1 | ForEach-Object { "$_" }); $code = $LASTEXITCODE } finally { $ErrorActionPreference = $old }
  if ($out.Count) { [IO.File]::AppendAllText($UpdateLog, (($out -join "`r`n") + "`r`n"), $Utf8) }
  Log-Update "  코드 $code"
  return $code
}

function Do-Update {
  # 로그인 세션(예약 작업)에서 돈다. 대화형 질문 없이 저장소·의존성만 갱신한다(비밀 묶음·SSH·시계는 setup).
  New-Item -ItemType Directory -Force $LocalDir | Out-Null
  $code = 0
  try {
    if (Test-Path $PidFile) { Log-Update '관제·미리보기가 켜져 있어 갱신하지 않는다(Windows 는 실행 중 파일이 잠긴다)'; $code = 2 }
    elseif (-not (Test-Path (Join-Path $Root '.git'))) { Log-Update "저장소가 없다: $Root (게이트웨이에서 1 설치·갱신을 먼저)"; $code = 3 }
    elseif (git -C $Root status --porcelain --untracked-files=no) { Log-Update '저장소에 고친 파일이 있어 갱신하지 않는다(게이트웨이에서는 코드를 고치지 않는다)'; $code = 4 }
    else {
      $steps = @(
        @('git fetch', { git -C $Root fetch origin }),
        @('git checkout dev', { git -C $Root checkout dev }),
        @('git pull --ff-only', { git -C $Root pull --ff-only origin dev }),
        @("uv sync ($RelayDir)", { Push-Location $RelayDir; try { uv sync } finally { Pop-Location } }),
        @("uv sync ($AlertDir)", { Push-Location $AlertDir; try { uv sync } finally { Pop-Location } }),
        @("uv sync ($HelperDir)", { Push-Location $HelperDir; try { uv sync } finally { Pop-Location } }))
      foreach ($s in $steps) { $code = Invoke-Logged $s[0] $s[1]; if ($code) { break } }
      if (-not $code) { Log-Update "저장소: $(git -C $Root log --oneline -1)" }
    }
  } catch { Log-Update "멈춤: $($_.Exception.Message)"; if (-not $code) { $code = 1 } }
  Log-Update "exit=$code"
  exit $code
}

function Get-ResumeMode {
  # 지금 모드 파일(Do-Run·Do-Preview 가 쓴다)에서 갱신 뒤 되돌릴 명령을 만든다. 꺼져 있으면 $null
  if (-not (Test-Path $ModeFile)) { return $null }
  $m = (Get-Content $ModeFile -Raw).Trim()
  $extra = if ($m -match '원격 시청 끔') { ' -NoRemoteCctv' } else { '' }
  $recExtra = if ($m -match '녹화 끔') { ' -NoRecord' } else { '' }
  if ($m.StartsWith('관제')) {
    $s = Load-State
    if ($s.PodIp -and $s.ControlPort -and $s.VideoPort) {
      return @{ Mode = 'run'; Args = "run -PodIp $($s.PodIp) -ControlPort $($s.ControlPort) -VideoPort $($s.VideoPort)$extra$recExtra -KeepOwner"; Label = '관제(창 3개)' }
    }
    return @{ Mode = 'run'; Args = ''; Label = '관제(창 3개)' }
  }
  if ($m.StartsWith('미리보기')) { return @{ Mode = 'preview'; Args = "preview$extra -KeepOwner"; Label = '카메라 미리보기' } }
  return $null
}

function Do-UpdateRemote {
  # 갱신하는 동안 자동 전환이 꺼진 모드를 보고 미리보기를 띄우지 않게 작업자를 멈췄다가, 끝나면 새 코드로 다시 띄운다(S15P31A101-701)
  Stop-AutoTask
  try { Invoke-UpdateRemote } finally { Start-AutoTask }
}

function Invoke-UpdateRemote {
  $resume = Get-ResumeMode
  if (Test-Path $PidFile) { Say "갱신하려고 지금 모드를 멈춘다(끝나면 되돌림: $(if ($resume) { $resume.Label } else { '없음' }))"; Do-Stop -Quiet }
  New-Item -ItemType Directory -Force $LocalDir | Out-Null
  Remove-Item $UpdateLog -Force -ErrorAction SilentlyContinue
  if (Test-Path $UpdateLog) { Fail "지난 갱신 로그를 지우지 못했다(다른 갱신이 도는 중일 수 있다): $UpdateLog" }
  $gwUser = Get-GatewayUser
  $arg = "-NoProfile -WindowStyle Minimized -ExecutionPolicy Bypass -File `"$PSCommandPath`" update"
  $act = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg
  $pri = New-ScheduledTaskPrincipal -UserId $gwUser -LogonType Interactive -RunLevel Limited
  Register-ScheduledTask -TaskName $UpdateTask -Action $act -Principal $pri -Force | Out-Null
  Start-ScheduledTask -TaskName $UpdateTask
  Say "로그인 세션에서 갱신 중(git pull --ff-only + uv sync, 로그 $UpdateLog)"
  $code = $null
  for ($i = 0; $i -lt 900 -and $null -eq $code; $i++) {   # 최대 15분(처음 uv sync 는 몇 분 걸린다)
    Start-Sleep -Seconds 1
    try { $text = [IO.File]::ReadAllText($UpdateLog, $Utf8) } catch { continue }   # 아직 없음·쓰는 중
    if ($text -match '(?m) exit=(\d+)\s*$') { $code = [int]$Matches[1] }
  }
  if (Test-Path $UpdateLog) { Get-Content $UpdateLog -Encoding UTF8 -Tail 15 | ForEach-Object { Write-Host "  $_" } }
  if ($null -eq $code) {
    Warn "15분 안에 갱신이 끝나지 않았다. 이전 모드로 되돌리지 않는다(설치 중 파일이 잠긴다). 로그를 보고 끝나면 run-remote/preview-remote 로 띄운다."
    return
  }
  if ($code) { Warn "갱신 실패(exit=$code). 이전 코드 그대로 둔다." } else { Say '갱신 끝' }
  if ($resume -and $resume.Args) { Start-InSession $resume.Args $resume.Label } else { Say '갱신 전 꺼져 있어 그대로 둔다(평상시는 preview-remote)' }
}

# ---------- 자가 복구(heal)·역터널 재시작·상태·로그 (S15P31A101-424) ----------
function Get-TunnelLogInfo {
  # 순수 함수: 역터널 로그 줄 목록과 ssh 프로세스 생존 여부에서 마지막 연결/끊김/성공 시각을 계산한다.
  param(
    [string[]]$Lines = @(),
    [bool]$ProcessAlive = $false,
    [datetime]$Now = (Get-Date)
  )
  $lastConn = $null
  $lastDisc = $null
  $lastSuccess = $null
  $firstTs = $null
  $lastKind = ''
  foreach ($raw in $Lines) {
    $line = ([string]$raw).TrimStart([char]0xFEFF)   # 파일 중간에 낀 BOM 때문에 줄을 놓치지 않는다(S15P31A101-709)
    if ($line -match '^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(.+)$') {
      $ts = [datetime]::ParseExact($Matches[1], 'yyyy-MM-dd HH:mm:ss', [Globalization.CultureInfo]::InvariantCulture)
      $msg = $Matches[2]
      if ($null -eq $firstTs) { $firstTs = $ts }
      if ($msg -match '^연결') {
        $lastConn = $ts
        $lastKind = 'connect'
      } elseif ($msg -match '^끊김') {
        if (($null -ne $lastConn) -and ((($ts - $lastConn).TotalSeconds) -ge 20)) {
          $lastSuccess = $ts
        }
        $lastDisc = $ts
        $lastKind = 'disconnect'
      }
    }
  }
  if (($lastKind -eq 'connect') -and $ProcessAlive -and ($null -ne $lastConn)) {
    $lastSuccess = $Now
  } elseif (($null -eq $lastSuccess) -and ($null -ne $firstTs)) {
    $lastSuccess = $firstTs
  }
  return @{
    LastConnectAt    = $lastConn
    LastDisconnectAt = $lastDisc
    LastSuccessAt    = $lastSuccess
    LastEvent        = $lastKind
  }
}

function ConvertFrom-TunnelLogBytes {
  # 순수 함수(S15P31A101-709): 역터널 로그 바이트를 줄 목록으로. 줄마다 UTF-8 로 읽고, UTF-8 이 아니면 대체 인코딩(기본: 시스템 ANSI, 한국어 Windows 는 CP949)으로 읽는다.
  # -394 판 역터널 스크립트는 Add-Content 에 -Encoding 이 없어 PS 5.1 기본(ANSI)으로 '연결' 을 썼다. 이 줄을 UTF-8 로만 읽으면 '연결' 이 깨져
  # 마지막 연결이 '기록 없음' 이 된다(-424 판 스크립트는 UTF-8 로 쓰지만, 게이트웨이의 스크립트 파일은 6(원격 열기 설정)을 다시 돌려야 바뀐다).
  param([byte[]]$Bytes = @(), [Text.Encoding]$Fallback = [Text.Encoding]::Default)
  $strict = New-Object System.Text.UTF8Encoding($false, $true)
  $lines = New-Object System.Collections.Generic.List[string]
  $start = 0
  for ($i = 0; $i -le $Bytes.Length; $i++) {
    if (($i -lt $Bytes.Length) -and ($Bytes[$i] -ne 10)) { continue }
    $len = $i - $start
    if (($len -gt 0) -and ($Bytes[$start + $len - 1] -eq 13)) { $len-- }
    if ($len -gt 0) {
      try { $text = $strict.GetString($Bytes, $start, $len) } catch { $text = $Fallback.GetString($Bytes, $start, $len) }
      $lines.Add($text.TrimStart([char]0xFEFF))
    }
    $start = $i + 1
  }
  return ,([string[]]$lines.ToArray())
}

function Test-TunnelRecordFresh {
  # 순수 함수(S15P31A101-709): 역터널 마지막 성공 기록이 StaleSeconds 안이면 true. 기록이 없으면(설치 직후·로그 없음) false = 모른다(오래됨과 다르다).
  param([nullable[datetime]]$LastSuccessAt = $null, [datetime]$Now = (Get-Date), [int]$StaleSeconds = 300)
  return (($null -ne $LastSuccessAt) -and ((($Now - $LastSuccessAt).TotalSeconds) -le $StaleSeconds))
}

function Get-HealPlan {
  # 순수 함수: 현재 게이트웨이 상태(서비스·예약 작업·터널 로그 시각·절전·모드)를 받아 수행할 복구 조치 목록을 돌려준다.
  param(
    [string]$SshdStatus = 'Running',
    [string]$SshdStartType = 'Automatic',
    [bool]$SshdFailureConfigured = $true,
    [string]$TunnelTaskState = 'Running',
    [bool]$TunnelProcessAlive = $true,
    [nullable[datetime]]$TunnelLastSuccessAt = $null,
    [datetime]$Now = (Get-Date),
    [int]$TunnelStaleSeconds = 300,
    [nullable[bool]]$TunnelEc2Listening = $null,  # EC2 127.0.0.1:2222 리슨 여부(null = 안 봤거나 못 봄, S15P31A101-709)
    [bool]$PowerCfgOk = $true,
    [string]$SavedMode = '',
    [bool]$RelayAlive = $true,
    [bool]$RunArgsKnown = $false,      # 관제(run)를 다시 띄울 Pod 주소·포트가 상태 파일에 있다(S15P31A101-652)
    [int]$RecentModeRestarts = 0,      # 최근 창 되살리기 횟수(Get-RecentModeRestartCount)
    [int]$MaxModeRestarts = 3,
    [string]$AutoTaskState = 'Running' # 자동 전환 작업자 예약 작업 상태(Missing 이면 등록, S15P31A101-701)
  )
  $actions = @()
  if ($SshdStatus -ne 'Missing') {
    if ($SshdStartType -ne 'Automatic') { $actions += 'set-sshd-automatic' }
    if ($SshdStatus -ne 'Running') { $actions += 'start-sshd' }
    if (-not $SshdFailureConfigured) { $actions += 'set-sshd-failure' }
  }
  if ($TunnelTaskState -ne 'Missing') {
    if ($TunnelTaskState -ne 'Running') {
      $actions += 'start-tunnel-task'
    }
    # 재시작은 진짜 끊겼을 때만(S15P31A101-709): ssh 가 없거나, 기록이 최근이 아닌데 EC2 가 포트가 없다고 답했을 때.
    # ssh 가 살아 있으면 ServerAliveInterval 15·CountMax 3 이라 끊긴 연결은 45초 안에 ssh 가 끝나고 루프가 다시 붙는다.
    # 기록이 없거나 오래됐다는 것만으로는 재시작하지 않는다: 16차 세션에서 '기록 없음'을 오래됨으로 봐 5분마다 멀쩡한 터널을 끊었다.
    $fresh = Test-TunnelRecordFresh -LastSuccessAt $TunnelLastSuccessAt -Now $Now -StaleSeconds $TunnelStaleSeconds
    $ec2Down = ($null -ne $TunnelEc2Listening) -and (-not $TunnelEc2Listening)
    if ((-not $TunnelProcessAlive) -or ((-not $fresh) -and $ec2Down)) {
      $actions += 'restart-tunnel'
    }
  }
  if (-not $PowerCfgOk) {
    $actions += 'fix-powercfg'
  }
  if ($AutoTaskState -eq 'Missing') { $actions += 'register-auto-task' }
  elseif ($AutoTaskState -ne 'Running') { $actions += 'start-auto-task' }
  # 창이 죽었으면 이전 모드로 다시 띄운다(-652). 같은 원인으로 거듭 죽으면(최근 MaxModeRestarts 번) 되살리지 않고 알리기만 한다
  $canRestart = $RecentModeRestarts -lt $MaxModeRestarts
  if (($SavedMode -eq 'preview') -and (-not $RelayAlive)) {
    $actions += $(if ($canRestart) { 'restart-preview' } else { 'notify-mode-flapping' })
  } elseif (($SavedMode -eq 'run') -and (-not $RelayAlive)) {
    $actions += $(if ($RunArgsKnown -and $canRestart) { 'restart-run' } elseif ($RunArgsKnown) { 'notify-mode-flapping' } else { 'notify-run-dead' })
  }
  return ,$actions
}

function Test-ModeTreeAlive {
  # 순수 함수(S15P31A101-652): 모드 창 PID 목록·PID 파일 시각·프로세스 표(Id·Name·ParentId·Created)로 창이 제대로 살아 있는지 본다.
  # 창은 'powershell -NoExit' 라 안의 프로그램(uv → simai-relay·simai-site-alert)이 죽어도 창은 열린 채 남는다. 11-09 미리보기가
  # 죽었는데 heal 은 창 PID 만 보고 '이상 없음'이었다. 그래서 ① PID 마다 powershell/pwsh 가 있고 ② PID 파일을 쓴 뒤(+StartSlackSeconds)
  # 시작한 프로세스가 아니며(PID 재사용) ③ ChildRequiredPids 의 창은 uv·python·simai-* 자식이 하나 이상 있어야 한다(conhost 는 세지 않는다).
  param(
    [int[]]$Pids = @(),
    [object[]]$Processes = @(),
    [nullable[datetime]]$PidFileTime = $null,
    [int[]]$ChildRequiredPids = @(),
    [int]$StartSlackSeconds = 5
  )
  if ($Pids.Count -eq 0) { return $false }
  foreach ($p in $Pids) {
    $proc = @($Processes | Where-Object { $_.Id -eq $p }) | Select-Object -First 1
    if ((-not $proc) -or ([string]$proc.Name -notmatch '^(powershell|pwsh)(\.exe)?$')) { return $false }
    if (($null -ne $PidFileTime) -and ($null -ne $proc.Created) -and ($proc.Created -gt $PidFileTime.AddSeconds($StartSlackSeconds))) { return $false }
  }
  foreach ($p in $ChildRequiredPids) {
    $kids = @($Processes | Where-Object { ($_.ParentId -eq $p) -and ([string]$_.Name -match '^(uv|python|pythonw|simai-[a-z-]+)(\.exe)?$') })
    if ($kids.Count -eq 0) { return $false }
  }
  return $true
}

function Get-RecentModeRestartCount {
  # 순수 함수(S15P31A101-652): heal 로그 줄에서 최근 WindowMinutes 분 안의 창 되살리기(미리보기·관제) 횟수.
  param([string[]]$Lines = @(), [datetime]$Now = (Get-Date), [int]$WindowMinutes = 30)
  $n = 0
  foreach ($line in $Lines) {
    if ($line -notmatch '^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(.+)$') { continue }
    $stamp, $msg = $Matches[1], $Matches[2]  # 다음 -match 가 $Matches 를 바꾸기 전에 꺼낸다
    if ($msg -notmatch '창이 죽어 다시 시작') { continue }
    $ts = [datetime]::ParseExact($stamp, 'yyyy-MM-dd HH:mm:ss', [Globalization.CultureInfo]::InvariantCulture)
    if ((($Now - $ts).TotalMinutes) -le $WindowMinutes) { $n++ }
  }
  return $n
}

function Write-HealLog([string]$m) {
  New-Item -ItemType Directory -Force $LocalDir | Out-Null
  Rotate-LogIfNeeded $HealLog
  $line = "{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), ($m -replace '[\r\n]+', ' ')
  [IO.File]::AppendAllText($HealLog, "$line`r`n", $Utf8)
}

function Test-TunnelSshAlive {
  $procs = @(Get-CimInstance Win32_Process -Filter "Name = 'ssh.exe'" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*127.0.0.1:2222:127.0.0.1:22*' })
  return ($procs.Count -gt 0)
}

function Stop-TunnelProcesses {
  $procs = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    ($_.Name -eq 'ssh.exe' -and $_.CommandLine -like '*127.0.0.1:2222:127.0.0.1:22*') -or
    ($_.Name -match '^(powershell|pwsh)\.exe$' -and $_.CommandLine -like '*gateway-remote-tunnel.ps1*')
  })
  foreach ($p in $procs) {
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
  }
}

function Get-GatewayEc2SshArgs {
  # heal(SYSTEM)에는 자기 .ssh 가 없어 게이트웨이 사용자의 설정·키·known_hosts 로 EC2 에 붙는다(known_hosts 가 없으면 BatchMode 에서 호스트 키 확인이 실패한다)
  $gwUser = Get-GatewayUser
  $uSsh = "C:\Users\$gwUser\.ssh"
  $uSshCfg = Join-Path $uSsh 'config'
  $uPem = Join-Path $uSsh 'K15A101T.pem'
  $uKnown = Join-Path $uSsh 'known_hosts'
  $a = @()
  if ((Test-Path $uSshCfg) -and (Test-Path $uPem)) { $a += @('-F', $uSshCfg, '-i', $uPem) }
  if (Test-Path $uKnown) { $a += @('-o', "UserKnownHostsFile=$uKnown") }
  return $a + @('-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', 'simai-ec2')
}

function Invoke-FreePortsOnEc2 {
  $sshArgs = Get-GatewayEc2SshArgs
  $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
  try {
    ssh @sshArgs '~/simai-gateway/free-ports.sh' 2>$null | Out-Null
  } catch { } finally { $ErrorActionPreference = $old }
}

function Test-TunnelListeningOnEc2 {
  # S15P31A101-709: EC2 127.0.0.1:2222 에 역터널이 열려 있으면 $true, 없으면 $false, EC2 에 못 붙거나 답을 못 읽으면 $null(모름 → 재시작 근거로 쓰지 않는다)
  $sshArgs = Get-GatewayEc2SshArgs
  $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
  try {
    $out = @(ssh @sshArgs "ss -ltnH 'sport = :2222' | wc -l" 2>$null)
    if ($LASTEXITCODE -ne 0) { return $null }
    $n = 0
    if ([int]::TryParse(([string]($out | Select-Object -Last 1)).Trim(), [ref]$n)) { return ($n -ge 1) }
    return $null
  } catch { return $null } finally { $ErrorActionPreference = $old }
}

function Read-TunnelLogLines([int]$Tail = 200) {
  # 역터널 스크립트가 쓰는 중에도 읽게 공유 모드로 연다. 옛 판(ANSI)·새 판(UTF-8) 줄이 섞여도 줄마다 맞게 읽는다(S15P31A101-709)
  if (-not (Test-Path $TunnelLog)) { return ,@() }
  try {
    $fs = New-Object System.IO.FileStream($TunnelLog, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    try {
      $ms = New-Object System.IO.MemoryStream
      $fs.CopyTo($ms)
      $lines = ConvertFrom-TunnelLogBytes -Bytes $ms.ToArray()
    } finally { $fs.Dispose() }
  } catch { return ,@() }
  if ($lines.Count -gt $Tail) { $lines = $lines[($lines.Count - $Tail)..($lines.Count - 1)] }
  return ,([string[]]$lines)
}

function Get-TunnelState {
  $now = Get-Date
  $t = Get-ScheduledTask -TaskName $TunnelTask -ErrorAction SilentlyContinue
  $tState = if ($t) { [string]$t.State } else { 'Missing' }
  $sshAlive = Test-TunnelSshAlive
  $lines = Read-TunnelLogLines -Tail 200
  $info = Get-TunnelLogInfo -Lines $lines -ProcessAlive $sshAlive -Now $now
  $lastConnLabel = if ($null -ne $info.LastConnectAt) {
    $s = $info.LastConnectAt.ToString('yyyy-MM-dd HH:mm:ss')
    if ($sshAlive -and ($info.LastEvent -eq 'connect')) { "$s (연결 유지 중)" }
    elseif ($null -ne $info.LastDisconnectAt) { "$s (마지막 끊김 $($info.LastDisconnectAt.ToString('HH:mm:ss')))" }
    else { $s }
  } else { '기록 없음' }
  # 기록이 없으면 null 로 둔다(모름). 예전에는 [datetime]::MinValue 로 바꿔 heal 이 '오래됨'으로 보고 5분마다 재시작했다(S15P31A101-709)
  return @{
    TaskState        = $tState
    ProcessAlive     = $sshAlive
    LastConnectAt    = $info.LastConnectAt
    LastDisconnectAt = $info.LastDisconnectAt
    LastSuccessAt    = $info.LastSuccessAt
    LastConnectLabel = $lastConnLabel
  }
}

function Test-ModeProcessesAlive {
  # 창 PID 와 그 안의 프로그램(자식)을 함께 본다(Test-ModeTreeAlive, S15P31A101-652). 관제는 [터널, 경보 단말, 영상 중계] 순이고
  # 터널 창은 끊겨 3초 쉬는 동안 자식이 없을 수 있어 자식은 보지 않는다. 미리보기는 [영상 중계] 하나다.
  if (-not (Test-Path $PidFile)) { return $false }
  $pids = @(Get-Content $PidFile -Raw | ConvertFrom-Json | ForEach-Object { [int]$_ })
  if ($pids.Count -eq 0) { return $false }
  $need = if ($pids.Count -ge 3) { @($pids[1..($pids.Count - 1)]) } else { $pids }
  $procs = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | ForEach-Object {
    [pscustomobject]@{ Id = [int]$_.ProcessId; Name = [string]$_.Name; ParentId = [int]$_.ParentProcessId; Created = $_.CreationDate }
  })
  if ($procs.Count -eq 0) { return $true }  # 프로세스 표를 못 읽으면 모른다: 멀쩡한 창을 죽이지 않는다
  return Test-ModeTreeAlive -Pids $pids -Processes $procs -PidFileTime (Get-Item $PidFile).LastWriteTime -ChildRequiredPids $need
}

function Do-RestartTunnel([switch]$Quiet) {
  $t = Get-ScheduledTask -TaskName $TunnelTask -ErrorAction SilentlyContinue
  if (-not $t) {
    if (-not $Quiet) { Warn "원격 역터널 예약 작업($TunnelTask)이 없다. 6(원격 열기 설정)을 먼저 실행한다." }
    return
  }
  if (-not $Quiet) { Say '역터널 재시작: 예약 작업 중지 → 옛 프로세스·EC2 포트 정리(free-ports.sh) → 다시 시작' }
  Stop-ScheduledTask -TaskName $TunnelTask -ErrorAction SilentlyContinue
  Stop-TunnelProcesses
  Invoke-FreePortsOnEc2
  Start-ScheduledTask -TaskName $TunnelTask
  if (-not $Quiet) {
    Start-Sleep -Seconds 3
    $ts = Get-TunnelState
    Say "역터널 상태: 작업=$($ts.TaskState), 프로세스=$(if ($ts.ProcessAlive) { '살아 있음' } else { '대기 중' }), 마지막 연결=$($ts.LastConnectLabel)"
  }
}

function Do-Heal {
  $now = Get-Date
  if ((Test-Admin) -and (-not (Get-ScheduledTask -TaskName $HealTask -ErrorAction SilentlyContinue))) {
    Register-HealTask
  }
  $svc = Get-Service sshd -ErrorAction SilentlyContinue
  $sshdStatus = if ($svc) { [string]$svc.Status } else { 'Missing' }
  $sshdStart = if ($svc) { [string]$svc.StartType } else { 'Missing' }
  $sshdFailOk = if ($svc) { Test-SshdFailureConfigured } else { $false }
  $tState = Get-TunnelState
  # ssh 는 살아 있는데 성공 기록이 최근이 아니면(기록 없음·옛 판 로그) 그것만으로 재시작하지 않고 EC2 에 포트가 열렸는지 물어본다(S15P31A101-709)
  $tunnelEc2 = $null
  if (($tState.TaskState -ne 'Missing') -and $tState.ProcessAlive -and (-not (Test-TunnelRecordFresh -LastSuccessAt $tState.LastSuccessAt -Now $now))) {
    $tunnelEc2 = Test-TunnelListeningOnEc2
  }
  $pcfg = Get-PowerCfgStatus
  $resume = Get-ResumeMode
  $savedMode = if ($resume) { $resume.Mode } else { '' }
  if ($savedMode -and -not (Test-CameraHelper)) {
    Start-CameraHelper
    Write-HealLog '카메라 헬퍼 재시작'
  }
  $relayAlive = if ($savedMode) { Test-ModeProcessesAlive } else { $true }
  $healLines = if (Test-Path $HealLog) { @(Get-Content -LiteralPath $HealLog -Encoding UTF8 -Tail 200) } else { @() }
  $recentRestarts = Get-RecentModeRestartCount -Lines $healLines -Now $now

  $plan = Get-HealPlan `
    -SshdStatus $sshdStatus `
    -SshdStartType $sshdStart `
    -SshdFailureConfigured $sshdFailOk `
    -TunnelTaskState $tState.TaskState `
    -TunnelProcessAlive $tState.ProcessAlive `
    -TunnelLastSuccessAt $tState.LastSuccessAt `
    -TunnelEc2Listening $tunnelEc2 `
    -Now $now `
    -PowerCfgOk $pcfg.Ok `
    -SavedMode $savedMode `
    -RelayAlive $relayAlive `
    -RunArgsKnown ([bool]($resume -and $resume.Args)) `
    -RecentModeRestarts $recentRestarts `
    -AutoTaskState (Get-AutoTaskState)

  if ($plan.Count -eq 0) {
    Say '자가 복구(heal): 이상 없음'
    return
  }

  $needAdmin = ($plan -contains 'set-sshd-automatic') -or ($plan -contains 'start-sshd') -or ($plan -contains 'set-sshd-failure') -or ($plan -contains 'fix-powercfg') -or ($plan -contains 'register-auto-task')
  if ($needAdmin -and (-not (Test-Admin))) {
    $ht = Get-ScheduledTask -TaskName $HealTask -ErrorAction SilentlyContinue
    if ($ht) {
      Say "관리자 권한 조치를 SYSTEM 예약 작업($HealTask)으로 실행한다"
      Start-ScheduledTask -TaskName $HealTask -ErrorAction SilentlyContinue
      Start-Sleep -Seconds 3
    } else {
      Warn 'sshd·절전 복구는 관리자 권한이 필요하다(관리자 권한으로 실행하거나 6 으로 예약 작업 등록).'
    }
  }

  if (Test-Admin) {
    if ($plan -contains 'set-sshd-automatic') {
      Set-Service sshd -StartupType Automatic
      Write-HealLog "sshd 시작 유형을 Automatic 으로 복구(이전: $sshdStart)"
      Say '복구: sshd 시작 유형 Automatic'
    }
    if ($plan -contains 'start-sshd') {
      Start-Service sshd
      Write-HealLog "sshd 서비스 시작(이전 상태: $sshdStatus)"
      Say '복구: sshd 서비스 시작'
    }
    if ($plan -contains 'set-sshd-failure') {
      Set-SshdFailureOptions
      Write-HealLog 'sshd 서비스 실패 시 재시작 옵션 설정(5s/5s/60s, reset 86400)'
      Say '복구: sshd 실패 시 재시작 옵션 설정'
    }
    if ($plan -contains 'fix-powercfg') {
      Set-GatewayPowerCfg
      Write-HealLog "절전 설정(AC) 복구(이전: 화면=$($pcfg.MonitorAc)초, 절전=$($pcfg.StandbyAc)초, 덮개=$($pcfg.LidAc))"
      Say '복구: 절전 설정(AC 화면·절전·덮개 동작 0)'
    }
    if ($plan -contains 'register-auto-task') {
      # git pull(update)로 이 기능이 들어온 게이트웨이는 다음 heal 에서 작업자가 생긴다
      Register-AutoTask
      Start-AutoTask
      Write-HealLog "자동 전환 작업자 예약 작업($AutoTask) 등록·시작"
      Say '복구: 자동 전환 작업자 등록·시작'
    }
  }
  if ($plan -contains 'start-auto-task') {
    # 로그인한 사용자 세션 작업이라 로그인 전에는 시작되지 않는다(다음 heal 이 다시 본다). 기록은 남기지 않는다(5분마다 쌓이지 않게)
    Start-AutoTask
    Say '복구: 자동 전환 작업자 시작'
  }

  if ($plan -contains 'restart-tunnel') {
    $reason = if ($tState.TaskState -ne 'Running') { "작업 상태=$($tState.TaskState)" } elseif (-not $tState.ProcessAlive) { 'ssh 프로세스 없음' } else { "EC2 에 역터널 포트 없음(마지막 연결 $($tState.LastConnectLabel))" }
    Do-RestartTunnel -Quiet
    Write-HealLog "역터널 재시작($reason)"
    Say "복구: 역터널 재시작($reason)"
  } elseif ($plan -contains 'start-tunnel-task') {
    Start-ScheduledTask -TaskName $TunnelTask
    Write-HealLog "역터널 예약 작업 시작(이전 상태: $($tState.TaskState))"
    Say '복구: 역터널 예약 작업 시작'
  }

  if (($plan -contains 'restart-preview') -or ($plan -contains 'restart-run')) {
    if ($resume -and $resume.Args) {
      # 새 모드가 시작하며 남은 창(안의 프로그램만 죽은 창 포함)을 먼저 닫는다(Do-Run·Do-Preview 의 Do-Stop)
      Start-InSession $resume.Args $resume.Label
      $what = if ($resume.Mode -eq 'run') { '관제' } else { '미리보기' }
      Write-HealLog "$what 창이 죽어 다시 시작($($resume.Args))"
      Say "복구: $what 창 다시 시작"
    }
  } elseif ($plan -contains 'notify-mode-flapping') {
    $lastHealLine = if ($healLines.Count) { [string]$healLines[-1] } else { '' }
    if ($lastHealLine -notmatch '되살리지 않음') {
      Write-HealLog "창이 30분 안에 $recentRestarts 번 다시 죽어 되살리지 않음(로그 확인: logs relay)"
    }
    Warn "창이 30분 안에 $recentRestarts 번 다시 죽었다. 자동으로 되살리지 않는다: logs relay 로 원인을 보고 다시 시작한다."
  } elseif ($plan -contains 'notify-run-dead') {
    $lastHealLine = if (Test-Path $HealLog) { [string](Get-Content -LiteralPath $HealLog -Encoding UTF8 -Tail 1) } else { '' }
    if ($lastHealLine -notmatch '관제가 꺼짐') {
      Write-HealLog '관제가 꺼짐(창 죽음 — Pod 주소 기록이 없어 자동 복구 안 함)'
    }
    Warn '관제가 꺼짐: 관제(run) 창이 죽어 있고 Pod 주소 기록이 없다(자동 재시작 안 함). run-remote(gw.sh live)로 다시 시작한다.'
  }
}

function Get-RecordingStatusInfo {
  # 녹화 폴더·용량·버린 프레임 수를 계산한다(S15P31A101-554).
  param(
    [string]$ModeText = '',
    [string]$RecordDirPath = '',
    [string]$RecordingsRootDir = $RecordingsRoot,
    [string[]]$RelayLogLines = @()
  )
  if ($ModeText -match '녹화 끔') {
    return @{
      Enabled        = $false
      RecordDir      = ''
      Exists         = $false
      TotalBytes     = [long]0
      SizeLabel      = '0.0 MB'
      DroppedFrames  = 0
      RecordedFrames = 0
      HasManifest    = $false
      Summary        = '끔 (-NoRecord)'
    }
  }
  $dir = $RecordDirPath
  if ((-not $dir) -and ($ModeText -match '녹화\s+([^\s,()]+)')) {
    $dir = $Matches[1]
  }
  if ((-not $dir) -and (Test-Path $RecordDirFile)) {
    $dir = ([IO.File]::ReadAllText($RecordDirFile, $Utf8)).Trim()
  }
  if ((-not $dir) -and $RecordingsRootDir -and (Test-Path $RecordingsRootDir)) {
    $latest = Get-ChildItem -LiteralPath $RecordingsRootDir -Directory -Filter 'live-*' -ErrorAction SilentlyContinue |
      Sort-Object Name -Descending | Select-Object -First 1
    if ($latest) { $dir = $latest.FullName }
  }
  if (-not $dir) {
    return @{
      Enabled        = $false
      RecordDir      = ''
      Exists         = $false
      TotalBytes     = [long]0
      SizeLabel      = '0.0 MB'
      DroppedFrames  = 0
      RecordedFrames = 0
      HasManifest    = $false
      Summary        = '기록 없음'
    }
  }

  $exists = Test-Path -LiteralPath $dir
  [long]$totalBytes = 0
  $dropped = 0
  $recorded = 0
  $hasManifest = $false

  if ($exists) {
    $files = @(Get-ChildItem -LiteralPath $dir -Recurse -File -ErrorAction SilentlyContinue)
    foreach ($f in $files) { $totalBytes += [long]$f.Length }
    $hasManifest = Test-Path -LiteralPath (Join-Path $dir 'manifest.json')
    $sessPath = Join-Path $dir 'session.json'
    if (Test-Path -LiteralPath $sessPath) {
      try {
        $sess = Get-Content -LiteralPath $sessPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($sess.recording_stats) {
          $dropped = [int]$sess.recording_stats.dropped_frames
          $recorded = [int]$sess.recording_stats.recorded_frames
        }
      } catch { }
    }
    $evFiles = @(Get-ChildItem -LiteralPath $dir -Recurse -Filter 'events.jsonl' -File -ErrorAction SilentlyContinue)
    if ($evFiles.Count -gt 0) {
      $evDropped = 0
      $evRecorded = 0
      $foundClosed = $false
      foreach ($ef in $evFiles) {
        foreach ($line in @(Get-Content -LiteralPath $ef.FullName -Encoding UTF8 -ErrorAction SilentlyContinue)) {
          if ($line -match '"event"\s*:\s*"closed"') {
            try {
              $ev = $line | ConvertFrom-Json
              $evDropped += [int]$ev.dropped_frames
              $evRecorded += [int]$ev.recorded_frames
              $foundClosed = $true
            } catch { }
          }
        }
      }
      if ($foundClosed) {
        if ($evDropped -gt $dropped) { $dropped = $evDropped }
        if ($evRecorded -gt $recorded) { $recorded = $evRecorded }
      }
    }
  }

  foreach ($line in $RelayLogLines) {
    if ($line -match '녹화 상태\s+(\{.+\})\s*$') {
      try {
        $snap = $Matches[1] | ConvertFrom-Json
        if ((-not $snap.record_dir) -or ($snap.record_dir -eq $dir)) {
          $dropped = [int]$snap.dropped_frames
          $recorded = [int]$snap.recorded_frames
        }
      } catch { }
    } elseif ($line -match '동시 녹화 완료:.*저장 프레임\s+(\d+),\s*바이트\s+\d+,\s*버린 프레임\s+(\d+)') {
      $recorded = [int]$Matches[1]
      $dropped = [int]$Matches[2]
    }
  }

  $sizeLabel = if ($totalBytes -ge 1GB) {
    ("{0:N2} GB" -f ($totalBytes / 1GB))
  } else {
    ("{0:N1} MB" -f ($totalBytes / 1MB))
  }
  $manifestLabel = if ($hasManifest) { ', manifest 있음' } elseif ($exists) { ', 녹화 중/manifest 전' } else { ', 폴더 아직 없음' }
  return @{
    Enabled        = $true
    RecordDir      = $dir
    Exists         = $exists
    TotalBytes     = $totalBytes
    SizeLabel      = $sizeLabel
    DroppedFrames  = $dropped
    RecordedFrames = $recorded
    HasManifest    = $hasManifest
    Summary        = "폴더 $dir, 용량 $sizeLabel, 버린 프레임 $dropped (저장 $recorded$manifestLabel)"
  }
}

function Do-Status {
  Say "카메라 헬퍼: $(if (Test-CameraHelper) { '실행 중' } else { '꺼짐' }) (로그 $HelperLog)"
  Say "녹화 업로드: $(if (Test-RecordingUpload) { '올리는 중' } else { '쉬는 중' }) (로그 $UploadLog)"
  $rawMode = '꺼짐'
  if (Test-Path $ModeFile) { $rawMode = (Get-Content $ModeFile -Raw).Trim() }
  $pids = @()
  if (Test-Path $PidFile) { $pids = @(Get-Content $PidFile -Raw | ConvertFrom-Json) }
  $modeAlive = if ($rawMode -ne '꺼짐') { Test-ModeProcessesAlive } else { $true }
  if ($rawMode.StartsWith('관제') -and (-not $modeAlive)) {
    Warn "모드: 관제가 꺼짐 (마지막 설정: $rawMode)"
  } elseif ($rawMode.StartsWith('미리보기') -and (-not $modeAlive)) {
    Warn "모드: 미리보기 창 죽음 ($rawMode)"
  } else {
    Say "모드: $rawMode"
  }
  foreach ($p in $pids) {
    $proc = Get-Process -Id $p -ErrorAction SilentlyContinue
    $alive = [bool]($proc -and ($proc.ProcessName -match '^(powershell|pwsh)$'))
    Say "  창 PID $p : $(if ($alive) { '살아 있음' } else { '죽음' })"
  }
  Show-AutoStatus

  $relayTail = if (Test-Path $RelayLog) { @(Get-Content -LiteralPath $RelayLog -Encoding UTF8 -Tail 120 -ErrorAction SilentlyContinue) } else { @() }
  $recInfo = Get-RecordingStatusInfo -ModeText $rawMode -RelayLogLines $relayTail
  if ($recInfo.Enabled -and ($recInfo.DroppedFrames -gt 0)) {
    Warn "녹화: $($recInfo.Summary)"
  } else {
    Say "녹화: $($recInfo.Summary)"
  }

  $sshd = Get-Service sshd -ErrorAction SilentlyContinue
  if ($sshd) {
    $failOk = Test-SshdFailureConfigured
    Say "sshd 서비스: $($sshd.Status), 시작 유형=$($sshd.StartType), 실패 시 재시작=$(if ($failOk) { '설정됨' } else { '미설정' })"
  } else {
    Warn 'sshd 서비스: 없음(6 원격 열기 설정 필요)'
  }

  $ts = Get-TunnelState
  $ht = Get-ScheduledTask -TaskName $HealTask -ErrorAction SilentlyContinue
  $htLabel = if ($ht) { [string]$ht.State } else { '없음' }
  Say "원격 역터널: 작업=$($ts.TaskState), ssh=$(if ($ts.ProcessAlive) { '살아 있음' } else { '없음' }), 마지막 연결=$($ts.LastConnectLabel), heal작업=$htLabel"

  if (Test-Path $RelayCfg) {
    $hosts = @(Select-String -Path $RelayCfg -Pattern '^\s*host\s*=\s*"([^"]+)"' | ForEach-Object { $_.Matches[0].Groups[1].Value })
    $up = @($hosts | Where-Object { Test-Tcp $_ 554 800 }).Count
    Say "카메라 RTSP 응답: $up / $($hosts.Count)"
  }

  if (Test-Path (Join-Path $Root '.git')) {
    $commit = (git -C $Root log --oneline -1 2>$null)
    Say "저장소 커밋: $commit"
  } else {
    Warn "저장소: 없음($Root)"
  }

  $driveName = (Split-Path $Root -Qualifier).TrimEnd(':')
  if (-not $driveName) { $driveName = 'C' }
  $drv = Get-PSDrive -Name $driveName -ErrorAction SilentlyContinue
  if ($drv) {
    $freeGb = [math]::Round($drv.Free / 1GB, 1)
    $totalGb = [math]::Round(($drv.Free + $drv.Used) / 1GB, 1)
    Say "디스크 여유($($driveName):): ${freeGb} GB / ${totalGb} GB"
  }

  $pcfg = Get-PowerCfgStatus
  if ($pcfg.Ok) {
    Say '절전 설정(AC): 정상 (화면 0, 절전 0, 덮개 0)'
  } else {
    Warn "절전 설정(AC): 이상 (화면=$($pcfg.MonitorAc)초, 절전=$($pcfg.StandbyAc)초, 덮개=$($pcfg.LidAc))"
  }

  if (Test-Path $HealLog) {
    $tail = @(Get-Content -LiteralPath $HealLog -Encoding UTF8 -Tail 3)
    if ($tail.Count -gt 0) {
      Say "heal 로그(마지막 $($tail.Count)줄):"
      foreach ($l in $tail) { Write-Host "  $l" }
    } else {
      Say 'heal 로그: 기록 없음(이상 없었음)'
    }
  } else {
    Say 'heal 로그: 기록 없음(이상 없었음)'
  }
}

function Do-Logs {
  $map = @{
    'helper' = $HelperLog
    'relay'  = $RelayLog
    'tunnel' = $TunnelLog
    'update' = $UpdateLog
    'heal'   = $HealLog
    'upload' = $UploadLog
    'auto'   = $AutoLog
  }
  $k = if ($LogKind) { $LogKind.Trim().ToLower() } else { '' }
  if (-not $map.ContainsKey($k)) {
    Fail "logs 종류는 relay | helper | tunnel | update | heal | upload | auto 중 하나여야 한다(사용: simai-gateway.ps1 logs <relay|helper|tunnel|update|heal|upload|auto> [-Lines 40])"
  }
  $path = $map[$k]
  if (-not (Test-Path $path)) {
    Say "로그 파일이 아직 없다: $path"
    return
  }
  $n = if ($Lines -gt 0) { $Lines } else { 40 }
  Say "=== $k 로그 ($path, 마지막 ${n}줄) ==="
  Get-Content -LiteralPath $path -Encoding UTF8 -Tail $n | ForEach-Object { Write-Host $_ }
}

function Do-DevRemote {
  # 개발 노트북에서 1회: 공개키를 EC2 에 올리고(게이트웨이 6 이 가져감) ~/.ssh/config 에 simai-gw 를 둔다.
  Need ssh
  $pub = Join-Path $SshDir 'id_ed25519.pub'
  if (-not (Test-Path $pub)) { Fail "공개키가 없다: $pub (ssh-keygen -t ed25519 로 먼저 만든다)" }
  $k = ([IO.File]::ReadAllText($pub)).Trim()
  Ec2 "mkdir -p $RemoteDir; echo $(To-B64 $k) | base64 -d > $RemoteDir/.k; echo >> $RemoteDir/.k; touch $RemoteDir/dev_keys.pub; grep -qxFf $RemoteDir/.k $RemoteDir/dev_keys.pub || cat $RemoteDir/.k >> $RemoteDir/dev_keys.pub; rm -f $RemoteDir/.k" | Out-Null
  Say '공개키를 EC2 에 올렸다(게이트웨이에서 6 을 하면 등록된다)'
  $user = (@(Ec2 "cat $RemoteDir/gateway_user 2>/dev/null; true") -join '').Trim()
  if (-not $user) { Warn '게이트웨이가 아직 6(원격 열기)을 하지 않았다. 게이트웨이에서 6 을 한 뒤 여기서 7 을 한 번 더 한다(SSH 설정 simai-gw 를 그때 만든다).'; return }
  $cfg = Join-Path $SshDir 'config'
  $text = ''
  if (Test-Path $cfg) { $text = [IO.File]::ReadAllText($cfg) }
  if ($text -match '(?m)^\s*Host\s+simai-gw\s*$') { Say 'SSH 설정 simai-gw 있음' }
  else {
    $block = "`r`nHost simai-gw`r`n    HostName 127.0.0.1`r`n    Port 2222`r`n    User $user`r`n    ProxyJump simai-ec2`r`n    HostKeyAlias simai-gw`r`n    StrictHostKeyChecking accept-new`r`n    ServerAliveInterval 15`r`n"
    [IO.File]::AppendAllText($cfg, $block, $Utf8)
    Say "SSH 설정 simai-gw 추가(사용자 $user)"
  }
  Say '확인: gateway/ops/gw.sh status  /  ssh simai-gw hostname  /  원격 데스크톱: 8'
}

function Do-Rdp {
  # 개발 노트북: EC2 로컬 포워드(127.0.0.2:13389 → EC2 127.0.0.1:13389 → 게이트웨이 3389) 뒤 mstsc. 127.0.0.1 은 mstsc 가 거절할 수 있어 .2 를 쓴다.
  Need ssh
  $p = Start-Process ssh -PassThru -WindowStyle Hidden -ArgumentList '-N', '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=15', '-L', '127.0.0.2:13389:127.0.0.1:13389', 'simai-ec2'
  try {
    $ok = $false
    for ($i = 0; $i -lt 20; $i++) { Start-Sleep -Milliseconds 500; if (Test-Tcp '127.0.0.2' 13389 300) { $ok = $true; break } }
    if (-not $ok) { Fail 'EC2 포워드가 열리지 않았다' }
    Say '원격 데스크톱 연결. 경보 소리가 로비에서 나도록 "로컬 리소스 → 원격 오디오 → 원격 컴퓨터에서 재생" 으로 둔다.'
    Start-Process mstsc -ArgumentList '/v:127.0.0.2:13389' -Wait
  } finally { if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force } }
}

# ---------- GPU 세션 자동 전환 (S15P31A101-701, 설계 docs/design/S15P31A101-701-gateway-auto-switch.md) ----------
# 예약 작업 simai-gateway-auto(로그인 사용자 세션, 숨김)가 15초마다 기기 토큰으로 EC2 를 조회한다:
#   GET /api/devices/gpu-session(계약 gpu_session_status) → 세션이 살아 있나, EC2 터널이 붙을 실제 주소·바깥 포트, 그 세션 Tier 0 가 I1 에 붙었나
#   GET /api/endpoints/tier0(계약 tier0_endpoint, 정본)   → 고정 지문 확인, 루프백(터널) 포트가 17000·17001 인지
# 준비되면 run(관제), 관제 대상이 유예(5분) 넘게 안 보이면 preview(미리보기)로 바꾼다. 전환은 사람이 쓰는 원격 명령과 같은 길(Start-InSession → run·preview
# → Do-Stop 으로 녹화 확정)을 쓴다. 조회가 실패하면(모름) 모드를 바꾸지 않는다. 사람이 run·preview·stop 을 치면 그 뒤 새로 등록된 세션이 나올 때까지 손대지 않는다.

function Get-EnvValue([string]$Name) {
  # .env 값 하나(없으면 ''). 기기 토큰 같은 비밀 값이 있어 화면·로그에 찍지 않는다
  $p = Join-Path $Root '.env'
  if (-not (Test-Path $p)) { return '' }
  foreach ($line in [IO.File]::ReadAllLines($p)) {
    if (($line -match '^\s*([A-Za-z0-9_]+)\s*=\s*(.*?)\s*$') -and ($Matches[1] -eq $Name)) {
      $v = $Matches[2]
      if (($v.Length -ge 2) -and (($v.StartsWith('"') -and $v.EndsWith('"')) -or ($v.StartsWith("'") -and $v.EndsWith("'")))) { $v = $v.Substring(1, $v.Length - 2) }
      return $v
    }
  }
  return ''
}

function Get-NowMs { return [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() }

function Write-AutoLog([string]$m) {
  New-Item -ItemType Directory -Force $LocalDir | Out-Null
  Rotate-LogIfNeeded $AutoLog
  [IO.File]::AppendAllText($AutoLog, ("{0:yyyy-MM-dd HH:mm:ss} {1}`r`n" -f (Get-Date), ($m -replace '[\r\n]+', ' ')), $Utf8)
}

function Get-AutoSettings {
  # 켬·끔과 유예 시간. 끔: .env SIMAI_GATEWAY_AUTO_SWITCH=off(배포 설정) 또는 auto off(현장에서 잠시)
  $grace = 0
  [void][int]::TryParse((Get-EnvValue 'SIMAI_GATEWAY_AUTO_REVERT_SECONDS').Trim(), [ref]$grace)
  if ($grace -le 0) { $grace = $AutoGraceSecondsDefault } elseif ($grace -lt 60) { $grace = 60 }
  $envOff = (Get-EnvValue 'SIMAI_GATEWAY_AUTO_SWITCH').Trim().ToLower() -in @('off', '0', 'false', 'no')
  $localOff = Test-Path $AutoOffFile
  $by = if ($envOff) { 'env' } elseif ($localOff) { 'local' } else { '' }
  return @{ Enabled = (-not $by); DisabledBy = $by; GraceSeconds = $grace }
}

function Get-ModeOwner {
  # @{ Owner = 'auto' | 'manual' | ''; AtMs; Command }. 파일이 없으면 Owner ''(이 기능 전의 게이트웨이)
  $o = @{ Owner = ''; AtMs = [long]0; Command = '' }
  if (Test-Path $OwnerFile) {
    try {
      $j = Get-Content -LiteralPath $OwnerFile -Raw -Encoding UTF8 | ConvertFrom-Json
      if ([string]$j.owner -in @('auto', 'manual')) { $o.Owner = [string]$j.owner; $o.AtMs = [long]$j.at_ms; $o.Command = [string]$j.command }
    } catch { }
  }
  return $o
}

function Set-ModeOwner([string]$Owner, [string]$CommandName) {
  New-Item -ItemType Directory -Force $LocalDir | Out-Null
  $doc = [ordered]@{ owner = $Owner; at_ms = (Get-NowMs); command = $CommandName }
  [IO.File]::WriteAllText($OwnerFile, ($doc | ConvertTo-Json -Compress), $Utf8)
}

function Set-OwnerForCommand([string]$CommandName) {
  # run·preview·stop 을 시작할 때 부른다. -KeepOwner(heal·갱신 복원)는 그대로 두고, -Auto 는 auto, 그 밖(사람)은 manual
  if ($KeepOwner) { return }
  try {
    if ($Auto) { Set-ModeOwner 'auto' $CommandName }
    else { Set-ModeOwner 'manual' $CommandName; Write-AutoLog "수동 명령 $CommandName → 자동 전환은 새 GPU 세션이 등록될 때까지 모드를 바꾸지 않는다" }
  } catch { Warn "모드 주인 기록 실패: $($_.Exception.Message)" }
}

function Get-ModeInfo([string]$ModeText = '') {
  # 순수 함수: 모드 파일 글(Do-Run 은 '관제 Pod <IP> 제어 <포트> 영상 <포트>, …', Do-Preview 는 '미리보기(…') → 모드와 실제로 띄운 관제 대상
  $t = if ($ModeText) { $ModeText.Trim() } else { '' }
  if ($t.StartsWith('관제')) {
    if ($t -match '^관제 Pod (\S+) 제어 (\d+) 영상 (\d+)') {
      return @{ Mode = 'run'; Target = @{ Host = $Matches[1]; ControlPort = [int]$Matches[2]; VideoPort = [int]$Matches[3] } }
    }
    return @{ Mode = 'run'; Target = $null }
  }
  if ($t.StartsWith('미리보기')) { return @{ Mode = 'preview'; Target = $null } }
  return @{ Mode = ''; Target = $null }
}

function Test-SameTarget($A, $B) {
  if ((-not $A) -or (-not $B)) { return $false }
  return (([string]$A.Host -eq [string]$B.Host) -and ([int]$A.ControlPort -eq [int]$B.ControlPort) -and ([int]$A.VideoPort -eq [int]$B.VideoPort))
}

function Test-PortNumber($v) {
  return ((($v -is [int]) -or ($v -is [long])) -and ([long]$v -ge 1) -and ([long]$v -le 65535))
}

function ConvertTo-GpuTarget($Session) {
  # 순수 함수: gpu_session_status.session → 터널 대상 @{ Ok; Target = @{ Host; ControlPort; VideoPort }; Error; Detail }.
  # 값이 run 명령줄(Start-InSession)에 들어가므로 계약 모양 그대로인지 엄격히 본다(주소 문자 제한·정수 포트)
  $bad = { param($d) @{ Ok = $false; Target = $null; Error = 'PORTS_UNSUPPORTED'; Detail = $d } }
  if (-not $Session) { return (& $bad '세션 없음') }
  $h = [string]$Session.public_host
  if (($h.Length -gt 253) -or ($h -notmatch '^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$')) { return (& $bad '공개 주소 형식이 틀렸다') }
  $ctrl = @(); $video = @()
  foreach ($p in @($Session.ports)) {
    if (-not $p) { continue }
    if (-not (Test-PortNumber $p.public_port)) { return (& $bad '포트가 1~65535 정수가 아니다') }
    if ([string]$p.role -eq 'CONTROL') { $ctrl += [int]$p.public_port }
    elseif ([string]$p.role -eq 'VIDEO') { $video += [int]$p.public_port }
  }
  $ctrl = @($ctrl | Sort-Object -Unique); $video = @($video | Sort-Object -Unique)
  if ($ctrl.Count -ne 1) { return (& $bad "CONTROL 포트가 하나가 아니다($($ctrl.Count))") }
  if ($video.Count -ne 1) { return (& $bad "VIDEO 포트가 한 번호가 아니다($($video.Count), 터널은 영상 포트 하나만 연다)") }
  return @{ Ok = $true; Target = @{ Host = $h; ControlPort = $ctrl[0]; VideoPort = $video[0] }; Error = ''; Detail = '' }
}

function Get-PinnedFingerprints([string]$Raw = '') {
  # .env SIMAI_TIER0_PINNED_SPKI_SHA256(쉼표로 여러 개, 키 교체 기간) → 소문자 목록. 형식이 틀린 항목은 버린다
  return @(($Raw -split ',') | ForEach-Object { $_.Trim().ToLower() } | Where-Object { $_ -match '^sha256:[0-9a-f]{64}$' })
}

function Get-EndpointCheck($Endpoint, [string[]]$Pins = @(), [string]$SessionFingerprint = '') {
  # 순수 함수: tier0_endpoint(조회 응답 봉투)와 고정 지문 목록 → @{ Ok; Reason; Fingerprint; Detail }.
  # 조회 값만으로 신뢰할 키를 바꾸지 않는다(relay·site_alert 와 같은 규칙). 지문이 목록에 없으면 전환하지 않는다
  $p = if ($Endpoint) { $Endpoint.payload } else { $null }
  if ((-not $p) -or ([string]$Endpoint.msg_type -ne 'tier0_endpoint')) { return @{ Ok = $false; Reason = 'ENDPOINT_UNAVAILABLE'; Fingerprint = ''; Detail = 'tier0_endpoint 형식이 아니다' } }
  $fp = ([string]$p.tls_public_key_sha256).ToLower()
  if ($fp -notmatch '^sha256:[0-9a-f]{64}$') { return @{ Ok = $false; Reason = 'ENDPOINT_UNAVAILABLE'; Fingerprint = ''; Detail = '지문 형식이 틀렸다' } }
  if ($Pins.Count -eq 0) { return @{ Ok = $false; Reason = 'PIN_MISMATCH'; Fingerprint = $fp; Detail = '고정 지문이 없다(.env SIMAI_TIER0_PINNED_SPKI_SHA256)' } }
  if ($Pins -notcontains $fp) { return @{ Ok = $false; Reason = 'PIN_MISMATCH'; Fingerprint = $fp; Detail = "엔드포인트 지문 $($fp.Substring(7, 12))… 이 고정 목록에 없다" } }
  $sfp = if ($SessionFingerprint) { $SessionFingerprint.ToLower() } else { '' }
  if ($sfp -and ($Pins -notcontains $sfp)) { return @{ Ok = $false; Reason = 'PIN_MISMATCH'; Fingerprint = $sfp; Detail = "세션 등록 지문 $($sfp.Substring(7, 12))… 이 고정 목록에 없다" } }
  if ([string]$p.public_ip -match '^127\.') {
    # EC2 터널 방식: 중계·경보 단말은 127.0.0.1 의 이 포트로 붙는다. 터널이 여는 17000·17001 과 다르면 관제가 이어지지 않는다
    $ctrl = @($p.ports | Where-Object { [string]$_.role -eq 'CONTROL' } | ForEach-Object { [int]$_.public_port } | Sort-Object -Unique)
    $video = @($p.ports | Where-Object { [string]$_.role -eq 'VIDEO' } | ForEach-Object { [int]$_.public_port } | Sort-Object -Unique)
    if (($ctrl.Count -ne 1) -or ($ctrl[0] -ne $TunnelControlPort) -or ($video.Count -ne 1) -or ($video[0] -ne $TunnelVideoPort)) {
      return @{ Ok = $false; Reason = 'ENDPOINT_PORTS_MISMATCH'; Fingerprint = $fp; Detail = "루프백 엔드포인트 포트(제어 $($ctrl -join '/') 영상 $($video -join '/'))가 터널($TunnelControlPort·$TunnelVideoPort)과 다르다" }
    }
  }
  return @{ Ok = $true; Reason = ''; Fingerprint = $fp; Detail = '' }
}

function Get-AutoSwitchPlan {
  # 순수 함수: 지금 모드·주인·조회 결과로 이번 판단을 정한다. 돌려줌 @{ Action = none|run|preview; Reason; Detail; Target; ReleaseManual; LastGoodAtMs }.
  # 규칙(설계 문서): 꺼짐·바쁨·조회 실패(모름)는 그대로 둔다. 수동 주인은 그 뒤 등록된 세션이 나와야 푼다. 준비된 새 대상이면 바로 관제로,
  # 관제 대상이 안 보이면(세션 끝·없음·Tier 0 끊김·다른 세션) 마지막으로 본 때부터 GraceSeconds 가 지나야 미리보기로. 자동 주인인데 꺼져 있으면 미리보기.
  param(
    [bool]$Enabled = $true,
    [bool]$Busy = $false,
    [string]$Owner = 'auto',
    [long]$ManualAtMs = 0,
    [string]$Mode = '',
    [hashtable]$CurrentTarget = $null,
    [string]$FetchError = '',          # '' | EC2_UNREACHABLE | AUTH_FAILED | INVALID_RESPONSE
    [object]$Session = $null,          # gpu_session_status.session
    [hashtable]$Target = $null,        # ConvertTo-GpuTarget 의 Target(없으면 null)
    [string]$TargetError = '',
    [string]$EndpointReason = '',      # '' 이면 엔드포인트·지문 괜찮음
    [string]$EndpointDetail = '',
    [long]$NowMs = 0,
    [long]$LastGoodAtMs = 0,
    [int]$GraceSeconds = 300,
    [int]$RecentFailures = 0,
    [int]$MaxFailures = 3
  )
  $r = @{ Action = 'none'; Reason = ''; Detail = ''; Target = $null; ReleaseManual = $false; LastGoodAtMs = $LastGoodAtMs }
  if (-not $Enabled) { $r.Reason = 'DISABLED'; $r.LastGoodAtMs = 0; return $r }   # 다시 켜면 유예를 새로 센다
  if ($Busy) { $r.Reason = 'BUSY'; return $r }
  if ($FetchError) { $r.Reason = $FetchError; $r.Detail = '모르면 모드를 그대로 둔다'; return $r }
  $active = [bool]($Session -and ([string]$Session.state -eq 'ACTIVE'))
  $tier0Up = $active -and ([string]$Session.tier0_link -eq 'CONNECTED')
  if ($Owner -eq 'manual') {
    if ($active -and ([long]$Session.registered_at_ms -gt $ManualAtMs)) { $r.ReleaseManual = $true }
    else { $r.Reason = 'MANUAL_HOLD'; $r.Detail = '수동 명령 뒤 새로 등록된 GPU 세션이 없다(auto on 으로 풀기)'; $r.Target = $CurrentTarget; $r.LastGoodAtMs = 0; return $r }
  }
  $isRun = ($Mode -eq 'run')
  if (-not $isRun) { $r.LastGoodAtMs = 0 }
  $sameTarget = $isRun -and (Test-SameTarget $CurrentTarget $Target)
  if ($sameTarget -and $tier0Up) {
    $r.Reason = 'RUNNING'; $r.Target = $CurrentTarget; $r.LastGoodAtMs = $NowMs; return $r
  }
  if ($isRun -and ($r.LastGoodAtMs -le 0)) { $r.LastGoodAtMs = $NowMs }   # 본 기록이 없으면(작업자 재시작·수동 관제를 넘겨받음) 지금부터 유예
  $ready = $tier0Up -and $Target -and (-not $EndpointReason)
  if ($ready) {
    if ($RecentFailures -ge $MaxFailures) { $r.Reason = 'BACKOFF'; $r.Detail = "30분 안에 전환 $RecentFailures 번 실패(logs auto)"; return $r }
    $r.Action = 'run'; $r.Reason = 'READY'; $r.Target = $Target; return $r
  }
  # 새 관제 대상이 없다: 왜인지
  $why = $EndpointReason
  if (-not $Session) { $why = 'NO_SESSION' }
  elseif (-not $active) { $why = 'SESSION_ENDED' }
  elseif (-not $Target) { $why = $(if ($TargetError) { $TargetError } else { 'PORTS_UNSUPPORTED' }) }
  elseif (-not $tier0Up) { $why = $(if ($sameTarget) { 'TIER0_LOST' } else { 'WAITING_TIER0' }) }
  $whyDetail = if ($EndpointReason -and ($why -eq $EndpointReason)) { $EndpointDetail } else { '' }
  if ($isRun) {
    $r.Target = $CurrentTarget
    $leftMs = ($GraceSeconds * 1000) - ($NowMs - $r.LastGoodAtMs)
    if ($leftMs -gt 0) { $r.Reason = 'GRACE'; $r.Detail = "$why, 미리보기까지 $([math]::Ceiling($leftMs / 1000))초"; return $r }
    if ($RecentFailures -ge $MaxFailures) { $r.Reason = 'BACKOFF'; $r.Detail = "30분 안에 전환 $RecentFailures 번 실패(logs auto)"; return $r }
    $r.Action = 'preview'; $r.Target = $null
    $r.Reason = if ($why -in @('NO_SESSION', 'SESSION_ENDED', 'TIER0_LOST')) { $why } else { 'SESSION_ENDED' }
    $r.Detail = "관제 대상이 $GraceSeconds 초 넘게 안 보임($why)"
    return $r
  }
  if ($Mode -eq '') {
    if ($RecentFailures -ge $MaxFailures) { $r.Reason = 'BACKOFF'; $r.Detail = "30분 안에 전환 $RecentFailures 번 실패(logs auto)"; return $r }
    $r.Action = 'preview'; $r.Reason = 'MODE_OFF'; $r.Detail = "자동 주인인데 꺼져 있다($why)"; return $r
  }
  $r.Reason = $why; $r.Detail = $whyDetail
  return $r
}

function Invoke-DeviceRequest([string]$Method, [string]$Path, [string]$Body = '') {
  # 기기 토큰 REST. @{ Status = HTTP 코드(연결 실패 0); Json; Error }. 토큰은 헤더에만 싣고 오류 문장에 넣지 않는다.
  $base = (Get-EnvValue 'SIMAI_EC2_BASE_URL').Trim().TrimEnd('/')
  $token = (Get-EnvValue 'SIMAI_DEVICE_TOKEN').Trim()
  if ((-not $base) -or (-not $token)) { return @{ Status = 0; Json = $null; Error = '.env 에 SIMAI_EC2_BASE_URL·SIMAI_DEVICE_TOKEN 이 없다' } }
  if (($base -notmatch '^https://') -and ($base -notmatch '^http://(127\.0\.0\.1|localhost)(:\d+)?$')) { return @{ Status = 0; Json = $null; Error = 'EC2 주소는 https 만 쓴다' } }
  try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch { }
  $req = @{ Uri = "$base$Path"; Method = $Method; UseBasicParsing = $true; TimeoutSec = 8; MaximumRedirection = 0; ErrorAction = 'Stop'
    Headers = @{ Authorization = "Bearer $token"; Accept = 'application/json' } }
  if ($Body) { $req.Body = [Text.Encoding]::UTF8.GetBytes($Body); $req.ContentType = 'application/json; charset=utf-8' }
  try {
    $resp = Invoke-WebRequest @req
    $json = $null
    if ($resp.Content) { try { $json = $resp.Content | ConvertFrom-Json } catch { return @{ Status = [int]$resp.StatusCode; Json = $null; Error = '응답이 JSON 이 아니다' } } }
    return @{ Status = [int]$resp.StatusCode; Json = $json; Error = '' }
  } catch {
    $code = 0
    try { if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode } } catch { }
    $kind = if ($code) { "HTTP $code" } else { $_.Exception.GetType().Name }
    return @{ Status = $code; Json = $null; Error = $kind }
  }
}

function Get-AutoObservation([hashtable]$Settings) {
  # EC2 두 곳을 조회해 판단 재료를 만든다. 실패는 까닭 코드로만 돌려준다(모드를 바꾸지 않는 쪽)
  $o = @{ FetchError = ''; FetchDetail = ''; Session = $null; Target = $null; TargetError = ''; TargetDetail = ''; EndpointReason = ''; EndpointDetail = ''; Fingerprint = '' }
  $r = Invoke-DeviceRequest 'GET' '/api/devices/gpu-session'
  if ($r.Status -eq 200 -and $r.Json -and ([string]$r.Json.schema_version -match '^1\.')) {
    $o.Session = $r.Json.session
  } elseif ($r.Status -in @(401, 403)) { $o.FetchError = 'AUTH_FAILED'; $o.FetchDetail = "$($r.Error) (기기 토큰·LAPTOP 기기인지 확인)"; return $o }
  elseif (($r.Status -eq 0) -or ($r.Status -ge 500)) { $o.FetchError = 'EC2_UNREACHABLE'; $o.FetchDetail = $r.Error; return $o }
  else { $o.FetchError = 'INVALID_RESPONSE'; $o.FetchDetail = "$($r.Error) $(if ($r.Status -eq 404) { '(EC2 가 이 기능 전 판일 수 있다)' })".Trim(); return $o }

  $s = $o.Session
  if ((-not $s) -or ([string]$s.state -ne 'ACTIVE')) { return $o }
  $t = ConvertTo-GpuTarget $s
  if ($t.Ok) { $o.Target = $t.Target } else { $o.TargetError = $t.Error; $o.TargetDetail = $t.Detail; return $o }
  if ([string]$s.tier0_link -ne 'CONNECTED') { return $o }
  $e = Invoke-DeviceRequest 'GET' '/api/endpoints/tier0'
  if ($e.Status -eq 404) { $o.EndpointReason = 'ENDPOINT_MISSING'; $o.EndpointDetail = 'Tier 0 엔드포인트 등록이 아직 없다'; return $o }
  if (($e.Status -ne 200) -or (-not $e.Json)) { $o.EndpointReason = 'ENDPOINT_UNAVAILABLE'; $o.EndpointDetail = $e.Error; return $o }
  $sfp = if ($s.tls_public_key_sha256) { [string]$s.tls_public_key_sha256 } else { '' }
  $c = Get-EndpointCheck -Endpoint $e.Json -Pins (Get-PinnedFingerprints (Get-EnvValue 'SIMAI_TIER0_PINNED_SPKI_SHA256')) -SessionFingerprint $sfp
  $o.Fingerprint = $c.Fingerprint
  if (-not $c.Ok) { $o.EndpointReason = $c.Reason; $o.EndpointDetail = $c.Detail }
  return $o
}

function Read-AutoState {
  $st = @{ last_good_at_ms = [long]0; failures = @(); last_reported_at_ms = [long]0; last_report_key = ''; last_log_key = ''
    last_switch = $null; last = $null }
  if (Test-Path $AutoStateFile) {
    try {
      $j = Get-Content -LiteralPath $AutoStateFile -Raw -Encoding UTF8 | ConvertFrom-Json
      $st.last_good_at_ms = [long]$j.last_good_at_ms
      $st.failures = @($j.failures | Where-Object { $_ } | ForEach-Object { [long]$_ })
      $st.last_reported_at_ms = [long]$j.last_reported_at_ms
      $st.last_report_key = [string]$j.last_report_key
      $st.last_log_key = [string]$j.last_log_key
      $st.last_switch = $j.last_switch
      $st.last = $j.last
    } catch { }
  }
  return $st
}

function Write-AutoState([hashtable]$State) {
  New-Item -ItemType Directory -Force $LocalDir | Out-Null
  [IO.File]::WriteAllText($AutoStateFile, ($State | ConvertTo-Json -Depth 6), $Utf8)
}

function Test-SwitchBusy {
  # 다른 전환(run·preview 예약 작업)이나 원격 갱신이 도는 중이면 이번 판단을 건너뛴다
  foreach ($name in @($RunTask, 'simai-gateway-update')) {
    $t = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($t -and ([string]$t.State -eq 'Running')) { return $true }
  }
  return $false
}

function Get-AutoTaskState {
  $t = Get-ScheduledTask -TaskName $AutoTask -ErrorAction SilentlyContinue
  if ($t) { return [string]$t.State } else { return 'Missing' }
}

function Register-AutoTask {
  # 로그인한 사용자 세션의 숨김 작업. 관제 창을 띄우는 예약 작업(simai-gateway-run)을 고쳐야 해서 관리자 권한(Highest, 게이트웨이 계정은 관리자)으로 돈다.
  # 등록은 관리자(1·6)나 heal(SYSTEM)이 한다. 죽으면 1분 뒤 다시 뜬다
  try {
    $gwUser = Get-GatewayUser
    $scriptPath = Join-Path $Root 'gateway\ops\simai-gateway.ps1'
    if (-not (Test-Path $scriptPath)) { $scriptPath = $PSCommandPath }
    $arg = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$scriptPath`" auto worker"
    $act = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg
    $trg = New-ScheduledTaskTrigger -AtLogOn -User $gwUser
    $pri = New-ScheduledTaskPrincipal -UserId $gwUser -LogonType Interactive -RunLevel Highest
    $set = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $AutoTask -Action $act -Trigger $trg -Principal $pri -Settings $set -Force | Out-Null
    Say "자동 전환 작업자 예약 작업 등록·갱신: $AutoTask (로그인 때, $gwUser 세션)"
  } catch { Warn "자동 전환 작업자 등록 실패(관리자 권한으로 1 또는 6, 또는 다음 heal): $($_.Exception.Message)" }
}

function Start-AutoTask {
  if (-not (Get-ScheduledTask -TaskName $AutoTask -ErrorAction SilentlyContinue)) { return }
  try { Start-ScheduledTask -TaskName $AutoTask -ErrorAction Stop } catch { }
}

function Stop-AutoTask {
  if (-not (Get-ScheduledTask -TaskName $AutoTask -ErrorAction SilentlyContinue)) { return }
  try { Stop-ScheduledTask -TaskName $AutoTask -ErrorAction Stop } catch { }
}

function Start-AutoMode([string]$ModeArgs, [string]$Label) {
  # 사람이 쓰는 원격 명령(run-remote·preview-remote)·heal 과 같은 길: 로그인 사용자 세션 예약 작업(simai-gateway-run)으로 run·preview 를 띄운다.
  # 작업자의 자식으로 띄우지 않는다: 갱신·설치가 작업자 예약 작업을 멈출 때 작업 스케줄러가 자식 창(관제·녹화)까지 끝내지 않게 하려는 것이다.
  # 그래서 작업자는 예약 작업을 고칠 수 있게 관리자 권한(RunLevel Highest)으로 돈다(Register-AutoTask).
  Start-InSession $ModeArgs $Label
}

function Wait-ModeApplied([string]$WantMode, [hashtable]$WantTarget, [int]$Seconds = 60) {
  # 모드 파일이 새 모드를 가리킬 때까지 본다(Do-Run·Do-Preview 는 창을 띄운 뒤 마지막에 쓴다). Start-InSession 은 20초만 기다리는데
  # Do-Run 은 이전 창 끄기·녹화 확정(최대 10초)·터널 대기(최대 10초)·경보 단말 3초라 그보다 길 수 있어 60초까지 본다(맞으면 바로 끝)
  for ($i = 0; $i -le ($Seconds * 2); $i++) {
    $text = if (Test-Path $ModeFile) { (Get-Content $ModeFile -Raw -Encoding UTF8) } else { '' }
    $mi = Get-ModeInfo $text
    if (($mi.Mode -eq $WantMode) -and (($WantMode -ne 'run') -or (Test-SameTarget $mi.Target $WantTarget))) { return $true }
    if ($i -lt ($Seconds * 2)) { Start-Sleep -Milliseconds 500 }
  }
  return $false
}

function ConvertTo-AutoReport([hashtable]$Plan, [hashtable]$Obs, [bool]$Enabled, [string]$Owner, [string]$ModeAfter, $SwitchOk, [long]$NowMs) {
  # 순수 함수: 판단 → gateway_switch_state 1.0.0 본문(계약). 비밀 값은 싣지 않는다
  $modeCode = @{ 'run' = 'RUN'; 'preview' = 'PREVIEW'; '' = 'OFF' }[$ModeAfter]
  $decision = @{ 'none' = 'NONE'; 'run' = 'SWITCH_RUN'; 'preview' = 'SWITCH_PREVIEW' }[$Plan.Action]
  $sid = $null
  if ($Obs -and $Obs.Session -and ([string]$Obs.Session.session_id -match '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')) { $sid = [string]$Obs.Session.session_id }
  $target = $null
  if ($Plan.Target -and ([string]$Plan.Target.Host -match '^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$')) {
    $target = [ordered]@{ public_host = [string]$Plan.Target.Host; control_port = [int]$Plan.Target.ControlPort; video_port = [int]$Plan.Target.VideoPort }
  }
  $doc = [ordered]@{
    schema_version = '1.0.0'; observed_at_ms = $NowMs; auto_enabled = $Enabled
    owner = $(if ($Owner -eq 'manual') { 'MANUAL' } else { 'AUTO' }); mode = $modeCode; decision = $decision
    switch_ok = $(if ($decision -eq 'NONE') { $null } else { [bool]$SwitchOk }); reason_code = $Plan.Reason; session_id = $sid; target = $target
  }
  if (($Plan.Reason -eq 'PIN_MISMATCH') -and $Obs -and ($Obs.Fingerprint -match '^sha256:[0-9a-f]{64}$')) { $doc.tls_public_key_sha256 = $Obs.Fingerprint }
  return $doc
}

function Invoke-AutoSwitchCycle {
  # 한 번 판단하고(필요하면 전환), 상태 파일·로그·EC2 에 남긴다. 작업자가 15초마다 부른다
  $now = Get-NowMs
  $st = Read-AutoState
  $settings = Get-AutoSettings
  $own = Get-ModeOwner
  $modeText = if (Test-Path $ModeFile) { (Get-Content $ModeFile -Raw -Encoding UTF8) } else { '' }
  $mi = Get-ModeInfo $modeText
  if (-not $own.Owner) {
    # 이 기능 전부터 켜 둔 모드: 관제는 사람이 켠 것으로(모드 파일 시각부터 수동), 미리보기·꺼짐은 평상시라 자동으로 넘겨받는다
    $at = if (Test-Path $ModeFile) { [DateTimeOffset]::new((Get-Item $ModeFile).LastWriteTimeUtc).ToUnixTimeMilliseconds() } else { $now }
    $own = @{ Owner = $(if ($mi.Mode -eq 'run') { 'manual' } else { 'auto' }); AtMs = $at; Command = '이전 모드 넘겨받음' }
    [IO.File]::WriteAllText($OwnerFile, ([ordered]@{ owner = $own.Owner; at_ms = $own.AtMs; command = $own.Command } | ConvertTo-Json -Compress), $Utf8)
  }
  $busy = $settings.Enabled -and (Test-SwitchBusy)
  $obs = @{ FetchError = ''; FetchDetail = ''; Session = $null; Target = $null; TargetError = ''; TargetDetail = ''; EndpointReason = ''; EndpointDetail = ''; Fingerprint = '' }
  if ($settings.Enabled -and -not $busy) { $obs = Get-AutoObservation $settings }
  $window = $now - 30 * 60 * 1000
  $failures = @($st.failures | Where-Object { $_ -ge $window })
  $plan = Get-AutoSwitchPlan -Enabled $settings.Enabled -Busy $busy -Owner $own.Owner -ManualAtMs $own.AtMs -Mode $mi.Mode -CurrentTarget $mi.Target `
    -FetchError $obs.FetchError -Session $obs.Session -Target $obs.Target -TargetError $obs.TargetError -EndpointReason $obs.EndpointReason `
    -EndpointDetail $obs.EndpointDetail -NowMs $now -LastGoodAtMs $st.last_good_at_ms -GraceSeconds $settings.GraceSeconds `
    -RecentFailures $failures.Count -MaxFailures $AutoMaxFailures
  if ($obs.FetchError) { $plan.Detail = (@($obs.FetchDetail, '모르면 모드를 그대로 둔다') | Where-Object { $_ }) -join ', ' }
  elseif ((-not $plan.Detail) -and $obs.TargetDetail) { $plan.Detail = $obs.TargetDetail }
  if ($plan.ReleaseManual) {
    Set-ModeOwner 'auto' '새 GPU 세션 등록'
    $own.Owner = 'auto'
    Write-AutoLog "수동 우선 해제: 수동 명령($($own.Command)) 뒤 새 GPU 세션 $([string]$obs.Session.session_id) 이 등록됨"
  }

  $switchOk = $null
  $modeAfter = $mi.Mode
  if ($plan.Action -ne 'none') {
    # 사람이 고른 원격 시청·녹화 끔은 이어 간다(기본은 켬)
    $extra = ''
    if ($modeText -match '원격 시청 끔') { $extra += ' -NoRemoteCctv' }
    if ($plan.Action -eq 'run') {
      if ($modeText -match '녹화 끔') { $extra += ' -NoRecord' }
      $t = $plan.Target
      Write-AutoLog "관제로 전환: $($t.Host) 제어 $($t.ControlPort) 영상 $($t.VideoPort) (세션 $([string]$obs.Session.session_id), 까닭 $($plan.Reason))"
      Start-AutoMode "run -PodIp $($t.Host) -ControlPort $($t.ControlPort) -VideoPort $($t.VideoPort) -Auto$extra" '관제(자동 전환)'
      $switchOk = Wait-ModeApplied 'run' $t
    } else {
      Write-AutoLog "미리보기로 전환: $($plan.Reason) ($($plan.Detail))"
      Start-AutoMode "preview -Auto$extra" '카메라 미리보기(자동 전환)'
      $switchOk = Wait-ModeApplied 'preview' $null
    }
    $after = Get-ModeInfo $(if (Test-Path $ModeFile) { (Get-Content $ModeFile -Raw -Encoding UTF8) } else { '' })
    $modeAfter = $after.Mode
    if ($switchOk) {
      if ($plan.Action -eq 'run') { $plan.LastGoodAtMs = $now }
      Write-AutoLog "전환 끝: $($plan.Action)"
    } else {
      $failures += $now
      Write-AutoLog "전환 실패: $($plan.Action) 새 모드 기록이 없다(로그 relay·heal 확인, 30분 안에 $($failures.Count) 번째)"
    }
    $st.last_switch = [ordered]@{ at_ms = $now; action = $plan.Action; ok = [bool]$switchOk; reason = $plan.Reason }
  }

  # 로그: 판단이 바뀔 때만(15초마다 쌓지 않는다)
  $logKey = "$($plan.Reason)|$($own.Owner)|$modeAfter|$([string]$obs.Session.session_id)|$($settings.Enabled)"
  if (($plan.Action -eq 'none') -and ($logKey -ne $st.last_log_key)) {
    Write-AutoLog "판단: $($plan.Reason)$(if ($plan.Detail) { " ($($plan.Detail))" }) · 모드 $(if ($modeAfter) { $modeAfter } else { '꺼짐' }) · 주인 $($own.Owner)"
  }
  $st.last_log_key = $logKey

  # EC2 에 알림: 판단이 바뀌었거나 전환했거나 5분이 지났을 때(실패해도 다음에 다시)
  $report = ConvertTo-AutoReport -Plan $plan -Obs $obs -Enabled $settings.Enabled -Owner $own.Owner -ModeAfter $modeAfter -SwitchOk $switchOk -NowMs $now
  $reportKey = "$($report.reason_code)|$($report.owner)|$($report.mode)|$($report.session_id)|$($report.auto_enabled)"
  if (($plan.Action -ne 'none') -or ($reportKey -ne $st.last_report_key) -or (($now - $st.last_reported_at_ms) -ge $AutoReportSeconds * 1000)) {
    $sent = Invoke-DeviceRequest 'POST' '/api/devices/gpu-session/gateway-state' ($report | ConvertTo-Json -Depth 4 -Compress)
    if ($sent.Status -eq 204) { $st.last_reported_at_ms = $now; $st.last_report_key = $reportKey }
  }

  $st.last_good_at_ms = $plan.LastGoodAtMs
  $st.failures = @($failures)
  $sess = $obs.Session
  $st.last = [ordered]@{
    at_ms = $now; enabled = $settings.Enabled; disabled_by = $settings.DisabledBy; grace_seconds = $settings.GraceSeconds
    owner = $own.Owner; owner_command = $own.Command; mode = $modeAfter; action = $plan.Action; reason = $plan.Reason; detail = [string]$plan.Detail
    session_id = $(if ($sess) { [string]$sess.session_id } else { '' }); session_state = $(if ($sess) { [string]$sess.state } else { '' })
    stage = $(if ($sess) { [string]$sess.stage } else { '' }); tier0_link = $(if ($sess) { [string]$sess.tier0_link } else { '' })
    target = $(if ($plan.Target) { "$($plan.Target.Host):$($plan.Target.ControlPort)/$($plan.Target.VideoPort)" } else { '' })
  }
  Write-AutoState $st
  return $plan
}

function Show-AutoStatus {
  $settings = Get-AutoSettings
  $own = Get-ModeOwner
  $taskState = Get-AutoTaskState
  $onLabel = if ($settings.Enabled) { '켬' } elseif ($settings.DisabledBy -eq 'env') { '끔(.env SIMAI_GATEWAY_AUTO_SWITCH)' } else { '끔(auto off, 켜기: auto on)' }
  $ownLabel = if ($own.Owner) { "$($own.Owner)($($own.Command), $([DateTimeOffset]::FromUnixTimeMilliseconds($own.AtMs).LocalDateTime.ToString('MM-dd HH:mm')))" } else { '기록 없음' }
  Say "자동 전환: $onLabel, 모드 주인 $ownLabel, 작업자=$taskState, 유예 $($settings.GraceSeconds)초 (로그 $AutoLog)"
  $st = Read-AutoState
  if ($st.last) {
    $l = $st.last
    $when = [DateTimeOffset]::FromUnixTimeMilliseconds([long]$l.at_ms).LocalDateTime.ToString('MM-dd HH:mm:ss')
    $sid = [string]$l.session_id
    $sess = if ($sid) { "세션 $($sid.Substring(0, [math]::Min(8, $sid.Length))) $($l.session_state)/$($l.stage) Tier0 $($l.tier0_link)" } else { '세션 없음' }
    $line = "  마지막 판단 $when : $($l.reason)$(if ($l.detail) { " ($($l.detail))" }), $sess$(if ($l.target) { ", 대상 $($l.target)" })"
    if ($l.reason -in @('PIN_MISMATCH', 'AUTH_FAILED', 'BACKOFF', 'PORTS_UNSUPPORTED', 'ENDPOINT_PORTS_MISMATCH', 'INVALID_RESPONSE')) { Warn $line.Trim() } else { Say $line }
  }
  if ($st.last_switch) {
    $w = [DateTimeOffset]::FromUnixTimeMilliseconds([long]$st.last_switch.at_ms).LocalDateTime.ToString('MM-dd HH:mm:ss')
    Say "  마지막 전환 $w : $($st.last_switch.action) $(if ($st.last_switch.ok) { '성공' } else { '실패' }) ($($st.last_switch.reason))"
  }
}

function Do-AutoWorker {
  $m = [Threading.Mutex]::new($false, 'Local\SimaiGatewayAuto')
  try { $acquired = $m.WaitOne(0) } catch [Threading.AbandonedMutexException] { $acquired = $true }
  if (-not $acquired) { $m.Dispose(); return }
  try {
    Write-AutoLog "자동 전환 작업자 시작($AutoPollSeconds 초마다 조회)"
    while ($true) {
      try { [void](Invoke-AutoSwitchCycle) } catch { Write-AutoLog "판단 중 오류: $($_.Exception.Message)" }
      Start-Sleep -Seconds $AutoPollSeconds
    }
  } finally { $m.ReleaseMutex(); $m.Dispose() }
}

function Do-Auto {
  $sub = if ($LogKind) { $LogKind.Trim().ToLower() } else { 'status' }
  switch ($sub) {
    'status' { Show-AutoStatus }
    'on' {
      Remove-Item $AutoOffFile -Force -ErrorAction SilentlyContinue
      Set-ModeOwner 'auto' 'auto on'
      Write-AutoLog 'auto on: 자동 전환 켬, 수동 우선 해제'
      Say '자동 전환을 켰다(수동 우선도 풀었다). 다음 조회(15초 안)부터 GPU 세션을 따라간다.'
      if ((Get-AutoSettings).DisabledBy -eq 'env') { Warn '.env 의 SIMAI_GATEWAY_AUTO_SWITCH=off 가 남아 있어 아직 꺼져 있다' }
      Start-AutoTask
    }
    'off' {
      New-Item -ItemType Directory -Force $LocalDir | Out-Null
      [IO.File]::WriteAllText($AutoOffFile, ("auto off {0:yyyy-MM-dd HH:mm:ss}" -f (Get-Date)), $Utf8)
      Write-AutoLog 'auto off: 자동 전환 끔(지금 모드는 그대로)'
      Say '자동 전환을 껐다. 지금 모드는 그대로 두고, 다시 켜려면 auto on.'
    }
    'once' { $p = Invoke-AutoSwitchCycle; Say "이번 판단: $($p.Action) $($p.Reason)"; Show-AutoStatus }
    'worker' { Do-AutoWorker }
    default { Fail 'auto 하위 명령은 status | on | off | once | worker 중 하나다(예: simai-gateway.cmd auto off)' }
  }
}

# ---------- 메뉴 ----------
function Show-Menu {
  while ($true) {
    Write-Host ''
    Write-Host '=== simai 게이트웨이 ===' -ForegroundColor Green
    Write-Host ' 1  설치·갱신 (저장소 pull·의존성·비밀 묶음·SSH·시계·heal 예약)'
    Write-Host ' 2  점검 (EC2·카메라·경보 소리)'
    Write-Host ' 3  관제 시작 (GPU Pod 가 떠 있을 때: 터널 → 경보 단말 → 영상 중계)'
    Write-Host ' 4  끄기 (관제·미리보기)'
    Write-Host ' 5  카메라 미리보기만 (GPU 없이, 전송 없음)'
    Write-Host ' 6  원격 열기 설정 (게이트웨이, 관리자, 처음 1회)'
    Write-Host ' 7  [개발 노트북] 원격 준비 (처음 1회)'
    Write-Host ' 8  [개발 노트북] 게이트웨이 원격 데스크톱'
    Write-Host ' 9  지금 상태(모드·창·sshd·역터널·카메라·디스크·절전·heal)'
    Write-Host ' h  heal (지금 점검·복구: sshd·역터널·절전·미리보기)'
    Write-Host ' a  GPU 세션 자동 전환 상태 (켜기·끄기: simai-gateway.cmd auto on|off)'
    Write-Host ' q  나가기'
    $c = Read-Host '선택'
    try {
      switch ($c) {
        '1' { Do-Setup } '2' { Do-Check } '3' { Set-OwnerForCommand 'run(메뉴)'; Do-Run } '4' { Set-OwnerForCommand 'stop(메뉴)'; Do-Stop }
        '5' { Set-OwnerForCommand 'preview(메뉴)'; Do-Preview } 'a' { Show-AutoStatus }
        '6' { Do-RemoteSetup } '7' { Do-DevRemote } '8' { Do-Rdp } '9' { Do-Status }
        'h' { Do-Heal } 'heal' { Do-Heal } 'q' { return } default { }
      }
    } catch { Write-Host "[gateway] 멈춤: $($_.Exception.Message)" -ForegroundColor Red }
  }
}

if ($MyInvocation.InvocationName -ne '.') {
  switch ($Command) {
    'setup' { Do-Setup } 'check' { Do-Check }
    # 사람이 친 run·stop·preview 는 모드 주인을 manual 로 남겨 자동 전환이 덮어쓰지 않게 한다(-Auto 는 자동 전환, -KeepOwner 는 heal·갱신 복원, S15P31A101-701)
    'run' { Set-OwnerForCommand 'run'; Do-Run } 'stop' { Set-OwnerForCommand 'stop'; Do-Stop } 'preview' { Set-OwnerForCommand 'preview'; Do-Preview }
    'auto' { Do-Auto }
    'remote-setup' { Do-RemoteSetup } 'run-remote' { Do-RunRemote } 'preview-remote' { Do-PreviewRemote } 'status' { Do-Status }
    'update' { Do-Update } 'update-remote' { Do-UpdateRemote }
    'heal' { Do-Heal } 'logs' { Do-Logs } 'restart-tunnel' { Do-RestartTunnel }
    'upload-recordings' { Start-RecordingUpload } 'upload-worker' { Do-UploadWorker }
    'dev-remote' { Do-DevRemote } 'rdp' { Do-Rdp }
    default { Show-Menu }
  }
}
