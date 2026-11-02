# simai 로비 게이트웨이 노트북 도우미 (Windows PowerShell 5.1 이상, S15P31A101-393). 설명은 같은 폴더 README.md.
# 더블클릭: simai-gateway.cmd  →  메뉴(1 설치·갱신 · 2 점검 · 3 관제 시작 · 4 관제 끄기)
# 명령줄: simai-gateway.cmd run -PodIp 1.2.3.4 -ControlPort 40001 -VideoPort 40002
# 비밀 값(.env·토큰·키)은 화면에 찍지 않는다. 키 이름만 확인한다.
param(
  [ValidateSet('menu', 'setup', 'check', 'run', 'stop')][string]$Command = 'menu',
  [string]$PodIp = '',
  [int]$ControlPort = 0,
  [int]$VideoPort = 0,
  [string]$Root = '',          # 비우면: 이 스크립트가 저장소 안이면 그 저장소, 아니면 C:\simai\S15P31A101
  [string]$RelayConfig = '',   # 비우면 <저장소>\gateway\relay\configs\lobby.toml
  [string]$Bundle = '',        # 비우면 스크립트 옆 bundle\ 또는 C:\simai-gateway\bundle\
  [string]$LocalDir = 'C:\simai-local'
)
$ErrorActionPreference = 'Stop'
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
  if (-not $Quiet) { Say '터널·경보 단말·영상 중계를 껐다' }
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
  $p3 = Start-Process powershell -PassThru -WorkingDirectory $RelayDir -ArgumentList '-NoExit', '-Command', "`$Host.UI.RawUI.WindowTitle='simai 영상 중계'; uv run simai-relay run --config '$RelayCfg' --send --preview-port 47032"
  (@($p1.Id, $p2.Id, $p3.Id) | ConvertTo-Json) | Set-Content $PidFile -Encoding ascii
  Say '관제 시작: 창 3개(터널·경보 단말·영상 중계). 끌 때는 메뉴 4'
}

# ---------- 메뉴 ----------
function Show-Menu {
  while ($true) {
    Write-Host ''
    Write-Host '=== simai 게이트웨이 ===' -ForegroundColor Green
    Write-Host ' 1  설치·갱신 (저장소 pull·의존성·비밀 묶음·SSH·시계)'
    Write-Host ' 2  점검 (EC2·카메라·경보 소리)'
    Write-Host ' 3  관제 시작 (터널 → 경보 단말 → 영상 중계)'
    Write-Host ' 4  관제 끄기'
    Write-Host ' q  나가기'
    $c = Read-Host '선택'
    try {
      switch ($c) { '1' { Do-Setup } '2' { Do-Check } '3' { Do-Run } '4' { Do-Stop } 'q' { return } default { } }
    } catch { Write-Host "[gateway] 멈춤: $($_.Exception.Message)" -ForegroundColor Red }
  }
}

switch ($Command) {
  'setup' { Do-Setup } 'check' { Do-Check } 'run' { Do-Run } 'stop' { Do-Stop } default { Show-Menu }
}
