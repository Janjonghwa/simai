# 개발 노트북에서 실행: 게이트웨이 노트북 첫 설치용 폴더를 만든다(도우미 스크립트 + bundle\ 비밀 묶음, S15P31A101-393).
# 첫 설치 뒤에는 도우미가 저장소 안의 판(gateway/ops)을 쓰므로, 다시 만들 일은 비밀 값이 바뀔 때뿐이다.
# 사용: powershell -ExecutionPolicy Bypass -File gateway\ops\make-gateway-usb.ps1 -Out E:\simai-gateway
#       -ListOnly 면 옮길 .env 키 이름만 보여 주고 아무것도 만들지 않는다.
# 값은 화면에 찍지 않는다. 게이트웨이에 필요한 키만 골라 옮긴다(EC2·Tier 0 서버 토큰 등은 옮기지 않는다).
param(
  [string]$Out = '',
  [switch]$ListOnly,
  [string]$RepoEnv = '',
  [string]$Pem = "$env:USERPROFILE\.ssh\K15A101T.pem"
)
$ErrorActionPreference = 'Stop'
if (-not $RepoEnv) { $RepoEnv = Join-Path $PSScriptRoot '..\..\.env' }
$RepoEnv = (Resolve-Path $RepoEnv).Path
$pattern = '^\s*(RTSP_[A-Za-z0-9_]+|SIMAI_EC2_BASE_URL|SIMAI_DEVICE_TOKEN|SIMAI_TIER0_PINNED_SPKI_SHA256|SIMAI_TIER0_CONTROL_PATH|SIMAI_LAPTOP_ID|SIMAI_RELAY_STATUS_PORT|SIMAI_GATEWAY_AUTO_SWITCH|SIMAI_GATEWAY_AUTO_REVERT_SECONDS)\s*='
$lines = @()
foreach ($line in [IO.File]::ReadAllLines($RepoEnv)) { if ($line -match $pattern) { $lines += $line } }
$names = $lines | ForEach-Object { ($_ -split '=', 2)[0].Trim() }
Write-Host "옮길 .env 키 $($names.Count)개: $($names -join ', ')"
if ([IO.File]::ReadAllText($RepoEnv) -match '(?m)^\s*SIMAI_TIER0_CA_FILE\s*=\s*\S') { Write-Host '주의: SIMAI_TIER0_CA_FILE 은 경로라 옮기지 않았다(필요하면 게이트웨이에서 직접).' -ForegroundColor Yellow }
if ($ListOnly) { return }
if (-not $Out) { throw '-Out 폴더를 준다(예: E:\simai-gateway)' }

$b = Join-Path $Out 'bundle'
New-Item -ItemType Directory -Force $b | Out-Null
[IO.File]::WriteAllLines((Join-Path $b 'gateway.env'), [string[]]$lines, (New-Object System.Text.UTF8Encoding $false))
Copy-Item $Pem (Join-Path $b 'K15A101T.pem') -Force
Copy-Item (Join-Path $PSScriptRoot 'simai-gateway.ps1'), (Join-Path $PSScriptRoot 'simai-gateway.cmd') $Out -Force
Write-Host "만듦: $Out (simai-gateway.cmd, bundle\gateway.env·K15A101T.pem)"
Write-Host '게이트웨이에서 폴더째 C:\simai-gateway 로 복사 → simai-gateway.cmd 를 관리자 권한으로 실행 → 1. 설치 중에 묶음을 지운다. USB 사본도 지운다.' -ForegroundColor Yellow
