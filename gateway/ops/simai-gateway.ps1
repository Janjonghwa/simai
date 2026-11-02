# simai 로비 게이트웨이 노트북 도우미 (Windows PowerShell 5.1 이상, S15P31A101-393). 설명은 같은 폴더 README.md.
# 더블클릭: simai-gateway.cmd  →  메뉴(1 설치·갱신 · 2 점검 · 3 관제 시작 · 4 관제 끄기 · 5 미리보기만 · 6~8 원격)
# 명령줄: simai-gateway.cmd run -PodIp 1.2.3.4 -ControlPort 40001 -VideoPort 40002   (CCTV 원격 시청 업로드를 끄려면 -NoRemoteCctv)
# 원격(S15P31A101-394): 게이트웨이 remote-setup(관리자 1회) · 개발 노트북 dev-remote(1회)·rdp · ssh simai-gw 로 run-remote/preview-remote/update-remote/stop/status
# 비밀 값(.env·토큰·키)은 화면에 찍지 않는다. 키 이름만 확인한다.
param(
  [ValidateSet('menu', 'setup', 'check', 'run', 'stop', 'preview', 'remote-setup', 'run-remote', 'preview-remote', 'update', 'update-remote', 'status', 'dev-remote', 'rdp')][string]$Command = 'menu',
  [string]$PodIp = '',
  [int]$ControlPort = 0,
  [int]$VideoPort = 0,
  [string]$Root = '',          # 비우면: 이 스크립트가 저장소 안이면 그 저장소, 아니면 C:\simai\S15P31A101
  [string]$RelayConfig = '',   # 비우면 <저장소>\gateway\relay\configs\lobby.toml
  [string]$Bundle = '',        # 비우면 스크립트 옆 bundle\ 또는 C:\simai-gateway\bundle\
  [string]$LocalDir = 'C:\simai-local',
  # 관제(run)·미리보기(preview)는 기본으로 CCTV 원격 시청 업로드(중계 --remote-cctv, 보는 사람이 있을 때만 EC2 로)를 켠다.
  # 관제 중 Tier 0 지연이 늘면 이 스위치로 끈다(S15P31A101-400). 로컬 미리보기(127.0.0.1)는 그대로다
  [switch]$NoRemoteCctv
)
$ErrorActionPreference = 'Stop'
$RemoteCctvArg = if ($NoRemoteCctv) { '' } else { ' --remote-cctv' }
$RemoteCctvLabel = if ($NoRemoteCctv) { '원격 시청 끔' } else { '원격 시청 켬' }
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
$SshDir = Join-Path $env:USERPROFILE '.ssh'
$Pem = Join-Path $SshDir 'K15A101T.pem'
$Utf8 = New-Object System.Text.UTF8Encoding $false
$RelayDir = Join-Path $Root 'gateway\relay'
$AlertDir = Join-Path $Root 'gateway\site_alert'

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
  $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
  if (-not $admin) { Warn '관리자 권한이 아니라 시계 동기화를 건너뛴다. 관리자 PowerShell 에서: w32tm /resync'; return }
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
  New-Item -ItemType Directory -Force (Split-Path $Root), $LocalDir, $SshDir | Out-Null
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
  foreach ($d in $RelayDir, $AlertDir) {
    Say "의존성 설치: $d"
    Push-Location $d
    try { uv sync; if ($LASTEXITCODE) { Fail "uv sync ($d)" } } finally { Pop-Location }
  }
  Install-Bundle
  Ensure-SshConfig
  Check-Env
  Sync-Clock
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

function Do-Stop([switch]$Quiet) {
  if (-not (Test-Path $PidFile)) { if (-not $Quiet) { Say '켜 둔 관제 창이 없다' }; return }
  foreach ($p in (Get-Content $PidFile -Raw | ConvertFrom-Json)) {
    if (Get-Process -Id $p -ErrorAction SilentlyContinue) { taskkill /PID $p /T /F | Out-Null }
  }
  Remove-Item $PidFile -Force
  Remove-Item $ModeFile -Force -ErrorAction SilentlyContinue
  if (-not $Quiet) { Say '관제·미리보기 창을 껐다' }
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
  ($s | ConvertTo-Json) | Set-Content $StateFile -Encoding ascii

  Do-Stop -Quiet
  if ((Test-Tcp '127.0.0.1' 17000 300) -or (Test-Tcp '127.0.0.1' 17001 300)) { Fail '127.0.0.1:17000/17001 을 이미 다른 프로그램이 쓰고 있다(예전 터널 창을 닫는다)' }

  # EC2 까지만 SSH 하나, EC2→Pod 는 연결마다 따로 TCP(카메라별 독립 연결, 명세 12.2). TLS 는 Tier 0 가 끝낸다.
  $fw = "-L 127.0.0.1:17000:$($s.PodIp):$($s.ControlPort) -L 127.0.0.1:17001:$($s.PodIp):$($s.VideoPort)"
  $tunnel = "`$Host.UI.RawUI.WindowTitle='simai 터널(EC2 경유)'; while (`$true) { Write-Host ('[터널] 연결 ' + (Get-Date -Format HH:mm:ss)); ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 simai-ec2 $fw; Write-Host '[터널] 끊김, 3초 뒤 다시'; Start-Sleep 3 }"
  $p1 = Start-Process powershell -PassThru -ArgumentList '-NoExit', '-Command', $tunnel
  $ok = $false
  for ($i = 0; $i -lt 20; $i++) { Start-Sleep -Milliseconds 500; if (Test-Tcp '127.0.0.1' 17000 300) { $ok = $true; break } }
  if (-not $ok) { Warn '터널이 10초 안에 열리지 않았다. 터널 창의 오류를 본다(계속 다시 시도한다).' } else { Say '터널 열림' }

  $p2 = Start-Process powershell -PassThru -WorkingDirectory $AlertDir -ArgumentList '-NoExit', '-Command', "`$Host.UI.RawUI.WindowTitle='simai 현장 경보 단말'; uv run simai-site-alert run"
  Start-Sleep 3
  $p3 = Start-Process powershell -PassThru -WorkingDirectory $RelayDir -ArgumentList '-NoExit', '-Command', "`$Host.UI.RawUI.WindowTitle='simai 영상 중계'; uv run simai-relay run --config '$RelayCfg' --send --preview-port 47032$RemoteCctvArg"
  (@($p1.Id, $p2.Id, $p3.Id) | ConvertTo-Json) | Set-Content $PidFile -Encoding ascii
  "관제 Pod $($s.PodIp) 제어 $($s.ControlPort) 영상 $($s.VideoPort), $RemoteCctvLabel ($(Get-Date -Format 'MM-dd HH:mm'))" | Set-Content $ModeFile -Encoding utf8
  Say "관제 시작: 창 3개(터널·경보 단말·영상 중계), CCTV $RemoteCctvLabel. 끌 때는 메뉴 4"
}

function Do-Preview {
  # GPU Pod 없이 카메라 → 관제 앱 CCTV 미리보기만(Tier 0 로 보내지 않음). 같은 노트북 브라우저는 로컬(127.0.0.1)로,
  # 노트북 밖 관리자는 원격 시청(EC2 경유, 보는 사람이 있을 때만 올림)으로 simai.live 관제 화면에서 본다.
  Need uv
  New-Item -ItemType Directory -Force $LocalDir | Out-Null
  Do-Stop -Quiet
  $p = Start-Process powershell -PassThru -WorkingDirectory $RelayDir -ArgumentList '-NoExit', '-Command', "`$Host.UI.RawUI.WindowTitle='simai 카메라 미리보기(전송 없음)'; uv run simai-relay run --config '$RelayCfg' --preview-port 47032$RemoteCctvArg"
  (@($p.Id) | ConvertTo-Json) | Set-Content $PidFile -Encoding ascii
  "미리보기(전송 없음), $RemoteCctvLabel ($(Get-Date -Format 'MM-dd HH:mm'))" | Set-Content $ModeFile -Encoding utf8
  Say "미리보기 시작(전송 없음, CCTV $RemoteCctvLabel). Pod 가 뜨면 3(관제 시작)으로 바꾼다. 끌 때는 4"
}

# ---------- 원격 (S15P31A101-394) ----------
# 게이트웨이가 EC2 로 역터널을 걸어 둔다: EC2 의 127.0.0.1:2222 → 게이트웨이 sshd, 127.0.0.1:13389 → 게이트웨이 원격 데스크톱.
# EC2 sshd 는 GatewayPorts 기본값(no)이라 EC2 루프백에만 열린다. 개발 노트북은 EC2 를 거쳐(ProxyJump·로컬 포워드) 들어온다.
$RemoteDir = '~/simai-gateway'           # EC2 ubuntu 홈 아래. 공개키·게이트웨이 사용자 이름·포트 정리 스크립트만 둔다(비밀 아님)
$TunnelScript = Join-Path $LocalDir 'gateway-remote-tunnel.ps1'
$TunnelTask = 'simai-gateway-remote'
$RunTask = 'simai-gateway-run'

function Test-Admin {
  return ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
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

function Do-RemoteSetup {
  if (-not (Test-Admin)) { Fail '관리자 권한으로 연다(simai-gateway.cmd 우클릭 → 관리자 권한으로 실행)' }
  Need ssh
  # 1) OpenSSH 서버: 127.0.0.1 에서만 듣고 키 인증만
  #    Windows 기능(Add-WindowsCapability)은 SSAFY 관리 PC 에서 20분 넘게 멈춘다(S15P31A101-398).
  #    sshd 서비스가 없을 때만 공식 Win32-OpenSSH MSI(버전·해시 고정)의 서버 기능만 설치한다.
  if (-not (Get-Service sshd -ErrorAction SilentlyContinue)) { Install-OpenSshServer }
  Set-Service sshd -StartupType Automatic
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
  Say 'OpenSSH 서버: 127.0.0.1 전용, 키 인증만'

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
  Ec2 "mkdir -p $RemoteDir; echo $(To-B64 $env:USERNAME) | base64 -d > $RemoteDir/gateway_user" | Out-Null

  # 3) 원격 데스크톱 켜기(역터널로만 들어오므로 방화벽 규칙은 손대지 않는다)
  Set-ItemProperty 'HKLM:\System\CurrentControlSet\Control\Terminal Server' -Name fDenyTSConnections -Value 0
  if ((Get-ItemProperty 'HKLM:\System\CurrentControlSet\Control\Terminal Server').fDenyTSConnections -ne 0) { Warn '원격 데스크톱이 정책으로 막혀 있다(SSH 는 된다)' } else { Say '원격 데스크톱 켬' }

  # 4) EC2 에 옛 역터널 포트 정리 스크립트(끊긴 세션이 포트를 쥐고 있으면 새 역터널이 실패한다)
  #    다른 sshd 세션(같은 ubuntu 라도 privsep 자식)의 PID 는 sudo 없이 ss -p 에 안 보여 옛 세션이 2222 를 쥔 채 남았다(10차 세션) → sudo -n(EC2 ubuntu 는 비밀번호 없는 sudo, S15P31A101-414)
  $free = "#!/bin/sh`n# simai gateway reverse tunnel: kill stale sshd holding 127.0.0.1:2222/13389 (S15P31A101-394, sudo -n S15P31A101-414)`nfor p in 2222 13389; do`n  for pid in `$(sudo -n ss -ltnpH `"sport = :`$p`" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u); do sudo -n kill `"`$pid`" 2>/dev/null; done`ndone`nexit 0`n"
  Ec2 "echo $(To-B64 $free) | base64 -d > $RemoteDir/free-ports.sh; chmod +x $RemoteDir/free-ports.sh" | Out-Null

  # 5) 역터널: 로그인 때 숨김 창으로 뜨고 끊기면 다시 붙는다
  $tunnel = @'
# simai 게이트웨이 원격 역터널 (S15P31A101-394). 예약 작업 simai-gateway-remote 가 로그인 때 띄운다. 도우미 6 이 다시 쓴다.
$log = Join-Path $PSScriptRoot 'gateway-remote-tunnel.log'
while ($true) {
  Add-Content $log ("{0:yyyy-MM-dd HH:mm:ss} 연결" -f (Get-Date))
  $t0 = Get-Date
  ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -R 127.0.0.1:2222:127.0.0.1:22 -R 127.0.0.1:13389:127.0.0.1:3389 simai-ec2
  Add-Content $log ("{0:yyyy-MM-dd HH:mm:ss} 끊김(코드 {1})" -f (Get-Date), $LASTEXITCODE)
  if (((Get-Date) - $t0).TotalSeconds -lt 20) { ssh -o BatchMode=yes -o ConnectTimeout=10 simai-ec2 '~/simai-gateway/free-ports.sh' }
  Start-Sleep 5
}
'@
  [IO.File]::WriteAllText($TunnelScript, $tunnel, (New-Object System.Text.UTF8Encoding $true))
  $act = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$TunnelScript`""
  $trg = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
  $pri = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
  $set = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
  Register-ScheduledTask -TaskName $TunnelTask -Action $act -Trigger $trg -Principal $pri -Settings $set -Force | Out-Null
  Stop-ScheduledTask -TaskName $TunnelTask -ErrorAction SilentlyContinue
  Start-ScheduledTask -TaskName $TunnelTask
  Start-Sleep 5
  $on = Ec2 "ss -ltnH 'sport = :2222' | wc -l"
  if ([int]($on | Select-Object -Last 1) -ge 1) { Say '역터널 열림(EC2 127.0.0.1:2222·13389). 개발 노트북에서 ssh simai-gw / 8(원격 데스크톱)' }
  else { Warn "역터널이 아직 안 열렸다. 로그: $(Join-Path $LocalDir 'gateway-remote-tunnel.log')" }
  Warn '게이트웨이는 전원 연결·절전 해제·Windows 로그인 상태로 둔다(역터널은 로그인 때 뜬다).'
}

function Start-InSession([string]$modeArgs, [string]$label) {
  # SSH 세션에서 띄운 창은 게이트웨이 화면에 안 보이고 SSH 를 끊으면 함께 죽는다 → 로그인한 사용자 세션의 예약 작업으로 띄운다.
  $arg = "-NoProfile -WindowStyle Minimized -ExecutionPolicy Bypass -File `"$PSCommandPath`" $modeArgs"
  $act = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg
  $pri = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
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
  $extra = if ($NoRemoteCctv) { ' -NoRemoteCctv' } else { '' }
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
$UpdateLog = Join-Path $LocalDir 'gateway-update.log'

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
        @("uv sync ($AlertDir)", { Push-Location $AlertDir; try { uv sync } finally { Pop-Location } }))
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
  if ($m.StartsWith('관제')) {
    $s = Load-State
    if ($s.PodIp -and $s.ControlPort -and $s.VideoPort) {
      return @{ Args = "run -PodIp $($s.PodIp) -ControlPort $($s.ControlPort) -VideoPort $($s.VideoPort)$extra"; Label = '관제(창 3개)' }
    }
    return $null
  }
  if ($m.StartsWith('미리보기')) { return @{ Args = "preview$extra"; Label = '카메라 미리보기' } }
  return $null
}

function Do-UpdateRemote {
  $resume = Get-ResumeMode
  if (Test-Path $PidFile) { Say "갱신하려고 지금 모드를 멈춘다(끝나면 되돌림: $(if ($resume) { $resume.Label } else { '없음' }))"; Do-Stop -Quiet }
  New-Item -ItemType Directory -Force $LocalDir | Out-Null
  Remove-Item $UpdateLog -Force -ErrorAction SilentlyContinue
  if (Test-Path $UpdateLog) { Fail "지난 갱신 로그를 지우지 못했다(다른 갱신이 도는 중일 수 있다): $UpdateLog" }
  $arg = "-NoProfile -WindowStyle Minimized -ExecutionPolicy Bypass -File `"$PSCommandPath`" update"
  $act = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg
  $pri = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
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
  if ($resume) { Start-InSession $resume.Args $resume.Label } else { Say '갱신 전 꺼져 있어 그대로 둔다(평상시는 preview-remote)' }
}

function Do-Status {
  $mode = '꺼짐'
  if (Test-Path $ModeFile) { $mode = (Get-Content $ModeFile -Raw).Trim() }
  Say "모드: $mode"
  if (Test-Path $PidFile) {
    foreach ($p in (Get-Content $PidFile -Raw | ConvertFrom-Json)) {
      $alive = [bool](Get-Process -Id $p -ErrorAction SilentlyContinue)
      Say "  창 PID $p : $(if ($alive) { '살아 있음' } else { '죽음' })"
    }
  }
  $t = Get-ScheduledTask -TaskName $TunnelTask -ErrorAction SilentlyContinue
  if ($t) { Say "원격 역터널 작업: $($t.State)" } else { Say '원격 역터널 작업: 없음(6 을 안 함)' }
  if (Test-Path $RelayCfg) {
    $hosts = Select-String -Path $RelayCfg -Pattern '^\s*host\s*=\s*"([^"]+)"' | ForEach-Object { $_.Matches[0].Groups[1].Value }
    $up = @($hosts | Where-Object { Test-Tcp $_ 554 800 }).Count
    Say "카메라 RTSP 응답: $up / $($hosts.Count)"
  }
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
  Say '확인: ssh simai-gw hostname  /  원격 데스크톱: 8'
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

# ---------- 메뉴 ----------
function Show-Menu {
  while ($true) {
    Write-Host ''
    Write-Host '=== simai 게이트웨이 ===' -ForegroundColor Green
    Write-Host ' 1  설치·갱신 (저장소 pull·의존성·비밀 묶음·SSH·시계)'
    Write-Host ' 2  점검 (EC2·카메라·경보 소리)'
    Write-Host ' 3  관제 시작 (GPU Pod 가 떠 있을 때: 터널 → 경보 단말 → 영상 중계)'
    Write-Host ' 4  끄기 (관제·미리보기)'
    Write-Host ' 5  카메라 미리보기만 (GPU 없이, 전송 없음)'
    Write-Host ' 6  원격 열기 설정 (게이트웨이, 관리자, 처음 1회)'
    Write-Host ' 7  [개발 노트북] 원격 준비 (처음 1회)'
    Write-Host ' 8  [개발 노트북] 게이트웨이 원격 데스크톱'
    Write-Host ' 9  지금 상태(모드·창·역터널·카메라)'
    Write-Host ' q  나가기'
    $c = Read-Host '선택'
    try {
      switch ($c) {
        '1' { Do-Setup } '2' { Do-Check } '3' { Do-Run } '4' { Do-Stop } '5' { Do-Preview }
        '6' { Do-RemoteSetup } '7' { Do-DevRemote } '8' { Do-Rdp } '9' { Do-Status } 'q' { return } default { }
      }
    } catch { Write-Host "[gateway] 멈춤: $($_.Exception.Message)" -ForegroundColor Red }
  }
}

switch ($Command) {
  'setup' { Do-Setup } 'check' { Do-Check } 'run' { Do-Run } 'stop' { Do-Stop } 'preview' { Do-Preview }
  'remote-setup' { Do-RemoteSetup } 'run-remote' { Do-RunRemote } 'preview-remote' { Do-PreviewRemote } 'status' { Do-Status }
  'update' { Do-Update } 'update-remote' { Do-UpdateRemote }
  'dev-remote' { Do-DevRemote } 'rdp' { Do-Rdp }
  default { Show-Menu }
}
