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

  It 'run(관제) 모드에서 창이 죽었고 Pod 주소 기록이 없으면 되살리지 않고 notify-run-dead 만 돌려준다' {
    $plan = Get-HealPlan -SavedMode 'run' -RelayAlive $false -Now $now
    ($plan -join ',') | Should Be 'notify-run-dead'
  }

  It 'run(관제) 모드에서 창이 죽었고 Pod 주소 기록이 있으면 이전 모드로 restart-run (S15P31A101-652)' {
    $plan = Get-HealPlan -SavedMode 'run' -RelayAlive $false -RunArgsKnown $true -Now $now
    ($plan -join ',') | Should Be 'restart-run'
  }

  It '최근 30분에 3번 되살렸으면 더 되살리지 않고 notify-mode-flapping (S15P31A101-652)' {
    (Get-HealPlan -SavedMode 'preview' -RelayAlive $false -RecentModeRestarts 3 -Now $now) -join ',' | Should Be 'notify-mode-flapping'
    (Get-HealPlan -SavedMode 'run' -RelayAlive $false -RunArgsKnown $true -RecentModeRestarts 3 -Now $now) -join ',' | Should Be 'notify-mode-flapping'
    (Get-HealPlan -SavedMode 'preview' -RelayAlive $false -RecentModeRestarts 2 -Now $now) -join ',' | Should Be 'restart-preview'
  }
}

Describe 'Test-ModeTreeAlive 창과 안의 프로그램 생존 (S15P31A101-652)' {
  $t0 = [datetime]::ParseExact('2026-11-09 09:00:00', 'yyyy-MM-dd HH:mm:ss', $null)
  function P($id, $name, $parent, $created) { [pscustomobject]@{ Id = $id; Name = $name; ParentId = $parent; Created = $created } }

  It '미리보기 창이 열려 있어도 안의 중계(uv)가 죽고 conhost 만 남았으면 죽은 것으로 본다(11-09)' {
    $procs = @((P 100 'powershell.exe' 1 $t0), (P 101 'conhost.exe' 100 $t0))
    (Test-ModeTreeAlive -Pids @(100) -Processes $procs -PidFileTime $t0 -ChildRequiredPids @(100)) | Should Be $false
  }

  It '창과 uv 자식이 있으면 살아 있다' {
    $procs = @((P 100 'powershell.exe' 1 $t0), (P 101 'conhost.exe' 100 $t0), (P 102 'uv.exe' 100 $t0.AddSeconds(1)))
    (Test-ModeTreeAlive -Pids @(100) -Processes $procs -PidFileTime $t0 -ChildRequiredPids @(100)) | Should Be $true
  }

  It '창 PID 가 없거나 PID 파일 뒤에 시작한 다른 powershell(PID 재사용)이면 죽은 것으로 본다' {
    (Test-ModeTreeAlive -Pids @(100) -Processes @() -PidFileTime $t0) | Should Be $false
    $reused = @((P 100 'powershell.exe' 1 $t0.AddHours(2)), (P 102 'uv.exe' 100 $t0.AddHours(2)))
    (Test-ModeTreeAlive -Pids @(100) -Processes $reused -PidFileTime $t0 -ChildRequiredPids @(100)) | Should Be $false
    $other = @((P 100 'notepad.exe' 1 $t0))
    (Test-ModeTreeAlive -Pids @(100) -Processes $other -PidFileTime $t0) | Should Be $false
  }

  It '관제는 터널 창 자식을 보지 않고(재연결 대기) 경보 단말·중계 창 자식은 본다' {
    $procs = @((P 10 'powershell.exe' 1 $t0), (P 20 'powershell.exe' 1 $t0), (P 30 'powershell.exe' 1 $t0),
      (P 21 'uv.exe' 20 $t0), (P 31 'uv.exe' 30 $t0))
    (Test-ModeTreeAlive -Pids @(10, 20, 30) -Processes $procs -PidFileTime $t0 -ChildRequiredPids @(20, 30)) | Should Be $true
    $alertDead = @($procs | Where-Object { $_.Id -ne 21 })
    (Test-ModeTreeAlive -Pids @(10, 20, 30) -Processes $alertDead -PidFileTime $t0 -ChildRequiredPids @(20, 30)) | Should Be $false
  }
}

Describe 'Get-RecentModeRestartCount 최근 창 되살리기 횟수 (S15P31A101-652)' {
  It '30분 안의 미리보기·관제 되살리기만 센다' {
    $now = [datetime]::ParseExact('2026-11-09 10:00:00', 'yyyy-MM-dd HH:mm:ss', $null)
    $lines = @(
      '2026-11-09 09:00:00 미리보기 창이 죽어 다시 시작(preview)',
      '2026-11-09 09:40:00 미리보기 창이 죽어 다시 시작(preview)',
      '2026-11-09 09:45:00 역터널 재시작(ssh 프로세스 없음)',
      '2026-11-09 09:55:00 관제 창이 죽어 다시 시작(run -PodIp 1.2.3.4 -ControlPort 1 -VideoPort 2)'
    )
    (Get-RecentModeRestartCount -Lines $lines -Now $now) | Should Be 2
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

Describe 'Build-RelayCommand 및 Get-RecordingStatusInfo 관제 동시 녹화 (S15P31A101-554)' {
  It 'Build-RelayCommand 는 recordDir 가 있으면 --record-dir 를 붙이고 preview(빈 문자열)면 붙이지 않는다' {
    $RelayLog = Join-Path $TestDrive 'gateway-relay.log'
    $cmdWithRec = Build-RelayCommand 'simai 영상 중계' ' --send' 'C:\simai-local\recordings\live-20261109-140000'
    ($cmdWithRec -like "*--send*--record-dir 'C:\simai-local\recordings\live-20261109-140000'*") | Should Be $true

    $cmdPreview = Build-RelayCommand 'simai 카메라 미리보기(전송 없음)' ''
    ($cmdPreview -like '*--record-dir*') | Should Be $false
  }

  It 'Get-RecordingStatusInfo 는 녹화 폴더 용량과 버린 프레임 수를 집계한다' {
    $recRoot = Join-Path $TestDrive 'recordings'
    $liveDir = Join-Path $recRoot 'live-20261109-140000'
    $camDir = Join-Path $liveDir 'cam_A'
    New-Item -ItemType Directory -Force $camDir | Out-Null
    [IO.File]::WriteAllBytes((Join-Path $camDir 'epoch_0000.h264'), (New-Object byte[] 2097152))
    [IO.File]::WriteAllText((Join-Path $liveDir 'manifest.json'), '{}', $Utf8)
    $logLines = @(
      '2026-11-09 14:00:05 INFO 녹화 상태 {"record_dir": "' + ($liveDir -replace '\\', '\\') + '", "recorded_frames": 250, "dropped_frames": 3}'
    )

    $info = Get-RecordingStatusInfo -ModeText "관제 Pod 1.2.3.4 제어 40001 영상 40002, 원격 시청 켬, 녹화 $liveDir" -RelayLogLines $logLines
    $info.Enabled | Should Be $true
    $info.RecordDir | Should Be $liveDir
    $info.TotalBytes | Should Be 2097154
    $info.DroppedFrames | Should Be 3
    $info.RecordedFrames | Should Be 250
    $info.HasManifest | Should Be $true
    ($info.Summary -like "*폴더 $liveDir*용량 2.0 MB*버린 프레임 3*") | Should Be $true
  }

  It 'Get-RecordingStatusInfo 는 -NoRecord(녹화 끔)일 때 Enabled=false 를 돌려준다' {
    $info = Get-RecordingStatusInfo -ModeText '관제 Pod 1.2.3.4 제어 40001 영상 40002, 원격 시청 켬, 녹화 끔'
    $info.Enabled | Should Be $false
    $info.Summary | Should Be '끔 (-NoRecord)'
  }
}


Describe '녹화 작업 구간 업로드 (S15P31A101-668)' {
  It '관제 모드를 끌 때만 업로드를 띄운다(미리보기·꺼짐·-NoRecordingUpload 는 아님)' {
    Test-UploadAfterStop -ModeText '관제 Pod 1.2.3.4 제어 40001 영상 40002, 원격 시청 켬, 녹화 C:\simai-local\recordings\live-1 (11-13 09:15)' | Should Be $true
    Test-UploadAfterStop -ModeText '미리보기(전송 없음), 원격 시청 켬 (11-13 09:15)' | Should Be $false
    Test-UploadAfterStop -ModeText '' | Should Be $false
    Test-UploadAfterStop -ModeText '관제 Pod 1.2.3.4' -NoUpload | Should Be $false
  }

  It '업로드는 중계 venv 에서 녹화 루트·모드 파일·.env 를 넘겨 simai_recorder upload 를 부른다' {
    $a = Get-RecordingUploadArgs
    ($a[0..4] -join ' ') | Should Be 'run python -m simai_recorder upload'
    $a[$a.IndexOf('--root') + 1] | Should Be $RecordingsRoot
    $a[$a.IndexOf('--mode-file') + 1] | Should Be $ModeFile
    ($a[$a.IndexOf('--env-file') + 1]).EndsWith('.env') | Should Be $true
  }
}
