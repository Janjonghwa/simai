$here = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $here 'simai-gateway.ps1')

Describe 'Get-HealPlan 순수 판정 함수' {
  $now = [datetime]::ParseExact('2026-11-03 10:30:00', 'yyyy-MM-dd HH:mm:ss', $null)

  It '모두 정상이면 빈 배열을 돌려준다' {
    $plan = Get-HealPlan -SshdStatus 'Running' -SshdStartType 'Automatic' -SshdFailureConfigured $true `
      -TunnelTaskState 'Running' -TunnelProcessAlive $true -TunnelLastSuccessAt $now.AddSeconds(-30) -Now $now `
      -PowerCfgOk $true -SavedMode 'preview' -RelayAlive $true
    $plan.Count | Should Be 0
  }

  It 'sshd 가 Stopped(signal 8 종료 등)이면 start-sshd 를 돌려준다' {
    $plan = Get-HealPlan -SshdStatus 'Stopped' -SshdStartType 'Automatic' -SshdFailureConfigured $true -Now $now
    ($plan -join ',') | Should Be 'start-sshd'
  }

  It 'sshd 시작 유형·실패 재시작 옵션이 빠졌으면 함께 복구한다' {
    $plan = Get-HealPlan -SshdStatus 'Stopped' -SshdStartType 'Manual' -SshdFailureConfigured $false -Now $now
    ($plan -join ',') | Should Be 'set-sshd-automatic,start-sshd,set-sshd-failure'
  }

  It '역터널 예약 작업이 Running 이 아니면 start-tunnel-task 를 포함한다' {
    $plan = Get-HealPlan -TunnelTaskState 'Ready' -TunnelProcessAlive $true -TunnelLastSuccessAt $now -Now $now
    ($plan -join ',') | Should Be 'start-tunnel-task'
  }

  It '역터널 프로세스가 없거나 마지막 성공이 300초를 넘었으면 restart-tunnel 을 포함한다' {
    $p1 = Get-HealPlan -TunnelTaskState 'Running' -TunnelProcessAlive $false -TunnelLastSuccessAt $now -Now $now
    ($p1 -join ',') | Should Be 'restart-tunnel'

    $p2 = Get-HealPlan -TunnelTaskState 'Running' -TunnelProcessAlive $true -TunnelLastSuccessAt $now.AddMinutes(-10) -Now $now
    ($p2 -join ',') | Should Be 'restart-tunnel'
  }

  It '절전 설정이 바뀌었으면 fix-powercfg 를 포함한다' {
    $plan = Get-HealPlan -PowerCfgOk $false -Now $now
    ($plan -join ',') | Should Be 'fix-powercfg'
  }

  It 'preview 모드에서 창이 죽었으면 restart-preview 를 돌려준다' {
    $plan = Get-HealPlan -SavedMode 'preview' -RelayAlive $false -Now $now
    ($plan -join ',') | Should Be 'restart-preview'
  }

  It 'run(관제) 모드에서 창이 죽었으면 되살리지 않고 notify-run-dead 만 돌려준다' {
    $plan = Get-HealPlan -SavedMode 'run' -RelayAlive $false -Now $now
    ($plan -join ',') | Should Be 'notify-run-dead'
  }
}

Describe 'Get-TunnelLogInfo 역터널 로그 해석' {
  $now = [datetime]::ParseExact('2026-11-03 10:30:00', 'yyyy-MM-dd HH:mm:ss', $null)

  It '마지막 줄이 연결이고 ssh 프로세스가 살아 있으면 현재 시각을 마지막 성공으로 본다' {
    $lines = @(
      '2026-11-03 08:00:00 연결'
    )
    $info = Get-TunnelLogInfo -Lines $lines -ProcessAlive $true -Now $now
    $info.LastEvent | Should Be 'connect'
    $info.LastSuccessAt | Should Be $now
  }

  It '장시간 연결 뒤 짧은 끊김 반복이면 마지막 정상 세션 종료 시각을 마지막 성공으로 본다' {
    $lines = @(
      '2026-11-03 09:00:00 연결',
      '2026-11-03 10:20:00 끊김(코드 255)',
      '2026-11-03 10:20:05 연결',
      '2026-11-03 10:20:08 끊김(코드 255)'
    )
    $info = Get-TunnelLogInfo -Lines $lines -ProcessAlive $false -Now $now
    $info.LastEvent | Should Be 'disconnect'
    $info.LastSuccessAt.ToString('yyyy-MM-dd HH:mm:ss') | Should Be '2026-11-03 10:20:00'
  }
}

Describe 'Test-SshdFailureConfigured 실패 복구 옵션 판정' {
  It '86400 및 RESTART 3개가 있으면 true 를 돌려준다' {
    $sample = @"
SERVICE_NAME: sshd
        RESET_PERIOD (in seconds)    : 86400
        FAILURE_ACTIONS              : RESTART -- Delay = 5000 milliseconds.
                                       RESTART -- Delay = 5000 milliseconds.
                                       RESTART -- Delay = 60000 milliseconds.
"@
    (Test-SshdFailureConfigured $sample) | Should Be $true
    (Test-SshdFailureConfigured 'SERVICE_NAME: sshd') | Should Be $false
  }
}

Describe 'Rotate-LogIfNeeded 로그 1 MB 순환' {
  It '1 MB 를 넘으면 .old 로 한 번 순환한다' {
    $tmpLog = Join-Path $TestDrive 'gateway-heal.log'
    [IO.File]::WriteAllText($tmpLog, ('a' * 1050000), $Utf8)
    Rotate-LogIfNeeded $tmpLog 1048576
    (Test-Path "$tmpLog.old") | Should Be $true
    (Test-Path $tmpLog) | Should Be $false
  }
}
