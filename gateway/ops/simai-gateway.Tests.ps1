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


Describe '종료 확인 없는 녹화 보호 (S15P31A101-693)' {
  It 'PID 기록이 없으면 녹화 파일을 종료된 것으로 확정하지 않는다' {
    $PidFile = Join-Path $TestDrive 'missing-pids.json'
    $ModeFile = Join-Path $TestDrive 'missing-mode.txt'
    Mock Stop-CameraHelper { }
    Mock Finalize-RecordingManifest { }
    Do-Stop -Quiet -NoUpload
    Assert-MockCalled Finalize-RecordingManifest -Times 0 -Exactly
  }
}



Describe 'GPU 세션 자동 전환 순수 판정 (S15P31A101-701)' {
  $pin = 'sha256:' + ('a' * 64)
  $other = 'sha256:' + ('b' * 64)
  $now = [long]1794657675000
  function Sess([string]$State = 'ACTIVE', [string]$Link = 'CONNECTED', [long]$RegAt = 1794657000000, [string]$PodHost = '203.0.113.24', [int]$Ctrl = 40123, [int]$Video = 40124) {
    [pscustomobject]@{ session_id = '6f1c2d4e-8a9b-4c0d-9e1f-2a3b4c5d6e7f'; state = $State; stage = 'READY'; public_host = $PodHost
      ports = @([pscustomobject]@{ role = 'CONTROL'; public_port = $Ctrl }, [pscustomobject]@{ role = 'VIDEO'; public_port = $Video })
      tls_public_key_sha256 = $null; registered_at_ms = $RegAt; tier0_link = $Link }
  }
  $tA = @{ Host = '203.0.113.24'; ControlPort = 40123; VideoPort = 40124 }
  $tB = @{ Host = '198.51.100.7'; ControlPort = 41001; VideoPort = 41002 }

  It '모드 파일에서 관제 대상을 읽는다(Do-Run 이 쓴 글)' {
    $mi = Get-ModeInfo '관제 Pod 203.0.113.24 제어 40123 영상 40124, 원격 시청 켬, 녹화 C:\simai-local\recordings\live-1 (11-14 09:00)'
    $mi.Mode | Should Be 'run'
    (Test-SameTarget $mi.Target $tA) | Should Be $true
    (Get-ModeInfo '미리보기(전송 없음), 원격 시청 켬 (11-14 09:00)').Mode | Should Be 'preview'
    (Get-ModeInfo '').Mode | Should Be ''
  }

  It '세션 주소·포트를 터널 대상으로 바꾸고 SSH 는 무시, 명령줄에 넣지 못할 값은 거부한다' {
    $t = ConvertTo-GpuTarget (Sess)
    $t.Ok | Should Be $true
    (Test-SameTarget $t.Target $tA) | Should Be $true
    $s = Sess; $s.ports += [pscustomobject]@{ role = 'SSH'; public_port = 22113 }
    (ConvertTo-GpuTarget $s).Ok | Should Be $true
    (ConvertTo-GpuTarget (Sess -PodHost '1.2.3.4; calc')).Error | Should Be 'PORTS_UNSUPPORTED'
    $two = Sess; $two.ports += [pscustomobject]@{ role = 'VIDEO'; public_port = 40125 }
    (ConvertTo-GpuTarget $two).Ok | Should Be $false
    $str = Sess; $str.ports[0].public_port = '40123 -NoRecord'
    (ConvertTo-GpuTarget $str).Ok | Should Be $false
  }

  It '엔드포인트 지문이 고정 목록에 없으면 PIN_MISMATCH, 루프백 포트가 터널과 다르면 ENDPOINT_PORTS_MISMATCH' {
    function Ep([string]$Fp, [string]$Ip = '127.0.0.1', [int]$Ctrl = 17000, [int]$Video = 17001) {
      [pscustomobject]@{ msg_type = 'tier0_endpoint'; payload = [pscustomobject]@{ public_ip = $Ip; tls_public_key_sha256 = $Fp
        ports = @([pscustomobject]@{ role = 'CONTROL'; public_port = $Ctrl }, [pscustomobject]@{ role = 'VIDEO'; camera_id = 'cam_A'; public_port = $Video },
          [pscustomobject]@{ role = 'VIDEO'; camera_id = 'cam_B'; public_port = $Video }) } }
    }
    (Get-EndpointCheck (Ep $pin) @($pin)).Ok | Should Be $true
    $bad = Get-EndpointCheck (Ep $other) @($pin)
    $bad.Reason | Should Be 'PIN_MISMATCH'
    $bad.Fingerprint | Should Be $other
    (Get-EndpointCheck (Ep $pin) @()).Reason | Should Be 'PIN_MISMATCH'
    (Get-EndpointCheck (Ep $pin) @($pin) $other).Reason | Should Be 'PIN_MISMATCH'
    (Get-EndpointCheck (Ep $pin -Ctrl 17002) @($pin)).Reason | Should Be 'ENDPOINT_PORTS_MISMATCH'
    (Get-EndpointCheck (Ep $pin -Ip '203.0.113.24' -Ctrl 40123 -Video 40124) @($pin)).Ok | Should Be $true
    (Get-PinnedFingerprints " $pin , sha256:short,$($other.ToUpper())").Count | Should Be 2
  }

  It '미리보기에서 세션이 준비되면 관제로, Tier 0 가 아직이면 기다린다' {
    $p = Get-AutoSwitchPlan -Mode 'preview' -Session (Sess) -Target $tA -NowMs $now
    $p.Action | Should Be 'run'
    $p.Reason | Should Be 'READY'
    (Test-SameTarget $p.Target $tA) | Should Be $true
    $w = Get-AutoSwitchPlan -Mode 'preview' -Session (Sess -Link 'DISCONNECTED') -Target $tA -NowMs $now
    $w.Action | Should Be 'none'
    $w.Reason | Should Be 'WAITING_TIER0'
  }

  It '같은 대상으로 이미 관제 중이면 다시 전환하지 않는다(새 세션이 같은 주소로 다시 등록해도)' {
    $p = Get-AutoSwitchPlan -Mode 'run' -CurrentTarget $tA -Session (Sess) -Target $tA -NowMs $now -LastGoodAtMs ($now - 600000)
    $p.Action | Should Be 'none'
    $p.Reason | Should Be 'RUNNING'
    $p.LastGoodAtMs | Should Be $now
  }

  It '짧은 끊김(Tier 0 재시작·EC2 재배포 2분)에는 되돌리지 않고 유예가 지나면 미리보기로' {
    $g = Get-AutoSwitchPlan -Mode 'run' -CurrentTarget $tA -Session (Sess -Link 'DISCONNECTED') -Target $tA -NowMs $now -LastGoodAtMs ($now - 120000)
    $g.Action | Should Be 'none'
    $g.Reason | Should Be 'GRACE'
    $l = Get-AutoSwitchPlan -Mode 'run' -CurrentTarget $tA -Session (Sess -Link 'DISCONNECTED') -Target $tA -NowMs $now -LastGoodAtMs ($now - 301000)
    $l.Action | Should Be 'preview'
    $l.Reason | Should Be 'TIER0_LOST'
    $e = Get-AutoSwitchPlan -Mode 'run' -CurrentTarget $tA -Session (Sess -State 'ENDED' -Link 'DISCONNECTED') -NowMs $now -LastGoodAtMs ($now - 120000)
    $e.Reason | Should Be 'GRACE'
    $e2 = Get-AutoSwitchPlan -Mode 'run' -CurrentTarget $tA -Session (Sess -State 'ENDED' -Link 'DISCONNECTED') -NowMs $now -LastGoodAtMs ($now - 400000)
    $e2.Action | Should Be 'preview'
    $e2.Reason | Should Be 'SESSION_ENDED'
    (Get-AutoSwitchPlan -Mode 'run' -CurrentTarget $tA -Session $null -NowMs $now -LastGoodAtMs ($now - 400000)).Reason | Should Be 'NO_SESSION'
  }

  It 'EC2 조회가 실패하면(모름) 유예가 지나도 모드를 그대로 둔다' {
    $p = Get-AutoSwitchPlan -Mode 'run' -CurrentTarget $tA -FetchError 'EC2_UNREACHABLE' -NowMs $now -LastGoodAtMs ($now - 3600000)
    $p.Action | Should Be 'none'
    $p.Reason | Should Be 'EC2_UNREACHABLE'
    $p.LastGoodAtMs | Should Be ($now - 3600000)
  }

  It '다른 세션이 준비되면 바로 새 대상으로 관제를 옮긴다' {
    $p = Get-AutoSwitchPlan -Mode 'run' -CurrentTarget $tB -Session (Sess) -Target $tA -NowMs $now -LastGoodAtMs ($now - 1000)
    $p.Action | Should Be 'run'
    (Test-SameTarget $p.Target $tA) | Should Be $true
  }

  It '수동 명령이 우선한다: 그 뒤 등록된 세션이 나와야 자동 전환이 다시 움직인다' {
    $hold = Get-AutoSwitchPlan -Owner 'manual' -ManualAtMs ($now - 1000) -Mode 'run' -CurrentTarget $tB -Session (Sess -RegAt ($now - 60000)) -Target $tA -NowMs $now
    $hold.Action | Should Be 'none'
    $hold.Reason | Should Be 'MANUAL_HOLD'
    $offHold = Get-AutoSwitchPlan -Owner 'manual' -ManualAtMs ($now - 1000) -Mode '' -Session $null -NowMs $now
    $offHold.Reason | Should Be 'MANUAL_HOLD'
    $rel = Get-AutoSwitchPlan -Owner 'manual' -ManualAtMs ($now - 120000) -Mode 'preview' -Session (Sess -RegAt ($now - 60000)) -Target $tA -NowMs $now
    $rel.ReleaseManual | Should Be $true
    $rel.Action | Should Be 'run'
  }

  It '지문이 맞지 않으면 전환하지 않고 까닭을 남긴다' {
    $p = Get-AutoSwitchPlan -Mode 'preview' -Session (Sess) -Target $tA -EndpointReason 'PIN_MISMATCH' -EndpointDetail '엔드포인트 지문이 고정 목록에 없다' -NowMs $now
    $p.Action | Should Be 'none'
    $p.Reason | Should Be 'PIN_MISMATCH'
    $p.Detail | Should Be '엔드포인트 지문이 고정 목록에 없다'
  }

  It '꺼짐·바쁨·전환 실패 반복은 움직이지 않고, 자동 주인인데 꺼져 있으면 미리보기를 띄운다' {
    (Get-AutoSwitchPlan -Enabled $false -Mode 'preview' -Session (Sess) -Target $tA -NowMs $now).Reason | Should Be 'DISABLED'
    (Get-AutoSwitchPlan -Busy $true -Mode 'preview' -Session (Sess) -Target $tA -NowMs $now).Reason | Should Be 'BUSY'
    (Get-AutoSwitchPlan -Mode 'preview' -Session (Sess) -Target $tA -NowMs $now -RecentFailures 3).Reason | Should Be 'BACKOFF'
    $off = Get-AutoSwitchPlan -Mode '' -Session $null -NowMs $now
    $off.Action | Should Be 'preview'
    $off.Reason | Should Be 'MODE_OFF'
  }

  It 'heal 은 자동 전환 작업자가 없으면 등록하고 멈춰 있으면 시작한다' {
    (Get-HealPlan -AutoTaskState 'Missing') -join ',' | Should Be 'register-auto-task'
    (Get-HealPlan -AutoTaskState 'Ready') -join ',' | Should Be 'start-auto-task'
    (Get-HealPlan -AutoTaskState 'Running').Count | Should Be 0
  }

  It 'EC2 에 보내는 전환 상태는 계약 모양이고 PIN_MISMATCH 때만 지문을 싣는다' {
    $plan = @{ Action = 'none'; Reason = 'PIN_MISMATCH'; Target = $null }
    $obs = @{ Session = (Sess); Fingerprint = $other }
    $r = ConvertTo-AutoReport -Plan $plan -Obs $obs -Enabled $true -Owner 'auto' -ModeAfter 'preview' -SwitchOk $null -NowMs $now
    $r.decision | Should Be 'NONE'
    ($null -eq $r.switch_ok) | Should Be $true
    $r.mode | Should Be 'PREVIEW'
    $r.tls_public_key_sha256 | Should Be $other
    $r2 = ConvertTo-AutoReport -Plan @{ Action = 'run'; Reason = 'READY'; Target = $tA } -Obs $obs -Enabled $true -Owner 'auto' -ModeAfter 'run' -SwitchOk $true -NowMs $now
    $r2.decision | Should Be 'SWITCH_RUN'
    $r2.switch_ok | Should Be $true
    $r2.target.control_port | Should Be 40123
    $r2.Contains('tls_public_key_sha256') | Should Be $false
  }
}

Describe 'GPU 세션 자동 전환 한 번 돌리기(가짜 EC2 응답, S15P31A101-701)' {
  $pin = 'sha256:' + ('a' * 64)
  $LocalDir = Join-Path $TestDrive 'local'
  New-Item -ItemType Directory -Force $LocalDir | Out-Null
  $ModeFile = Join-Path $LocalDir 'gateway-mode.txt'
  $AutoStateFile = Join-Path $LocalDir 'gateway-auto.json'
  $AutoOffFile = Join-Path $LocalDir 'gateway-auto-off.txt'
  $OwnerFile = Join-Path $LocalDir 'gateway-mode-owner.json'
  $AutoLog = Join-Path $LocalDir 'gateway-auto.log'
  $runText = '관제 Pod 203.0.113.24 제어 40123 영상 40124, 원격 시청 켬, 녹화 C:\simai-local\recordings\live-1 (11-14 09:00)'
  $previewText = '미리보기(전송 없음), 원격 시청 켬 (11-14 09:00)'
  $Bom = New-Object System.Text.UTF8Encoding $true
  function Reset([string]$Mode, [string]$Owner = 'auto', [long]$OwnerAt = 1) {
    Remove-Item (Join-Path $LocalDir '*') -Force -ErrorAction SilentlyContinue
    if ($Mode) { [IO.File]::WriteAllText($ModeFile, $Mode, $Bom) }
    [IO.File]::WriteAllText($OwnerFile, ('{"owner":"' + $Owner + '","at_ms":' + $OwnerAt + ',"command":"test"}'), $Utf8)
  }
  function SessionJson([string]$State = 'ACTIVE', [string]$Link = 'CONNECTED', [long]$RegAt = 1794657000000) {
    $ended = if ($State -eq 'ENDED') { '1794661200000' } else { 'null' }
    $reason = if ($State -eq 'ENDED') { '"END_REPORTED"' } else { 'null' }
    ('{"schema_version":"1.0.0","server_time_ms":1794657660000,"session":{"session_id":"6f1c2d4e-8a9b-4c0d-9e1f-2a3b4c5d6e7f","state":"' + $State +
      '","stage":"READY","public_host":"203.0.113.24","ports":[{"role":"CONTROL","public_port":40123,"requested_port":17000},{"role":"VIDEO","public_port":40124}],' +
      '"tls_public_key_sha256":null,"registered_at_ms":' + $RegAt + ',"last_report_at_ms":1794657600000,"ended_at_ms":' + $ended + ',"end_reason":' + $reason +
      ',"tier0_link":"' + $Link + '"}}') | ConvertFrom-Json
  }
  function EndpointJson([string]$Fp) {
    ('{"schema_version":"1.0.0","msg_type":"tier0_endpoint","msg_id":"0190c3f0-0000-7000-8000-000000000001","sent_at":1,"source_id":"tier0-lobby-s9",' +
      '"payload":{"gateway_id":"tier0-lobby-s9","public_ip":"127.0.0.1","ports":[{"role":"CONTROL","requested_port":70000,"public_port":17000},' +
      '{"role":"VIDEO","camera_id":"cam_A","requested_port":70001,"public_port":17001}],"tls_public_key_sha256":"' + $Fp + '","registered_at":1}}') | ConvertFrom-Json
  }

  Mock Get-EnvValue { if ($Name -eq 'SIMAI_TIER0_PINNED_SPKI_SHA256') { return $pin } return '' }
  Mock Test-SwitchBusy { $false }
  Mock Write-Host { }

  Context '세션이 준비되면 관제로 전환하고 상태·EC2 에 남긴다' {
    It '세션이 준비되면 관제로 전환하고 상태·EC2 에 남긴다' {
      Reset $previewText
      Mock Invoke-DeviceRequest { @{ Status = 200; Json = (SessionJson); Error = '' } } -ParameterFilter { $Path -eq '/api/devices/gpu-session' }
      Mock Invoke-DeviceRequest { @{ Status = 200; Json = (EndpointJson $pin); Error = '' } } -ParameterFilter { $Path -eq '/api/endpoints/tier0' }
      Mock Invoke-DeviceRequest { @{ Status = 204; Json = $null; Error = '' } } -ParameterFilter { $Method -eq 'POST' }
      Mock Start-AutoMode { [IO.File]::WriteAllText($ModeFile, $runText, $Bom) }
      $p = Invoke-AutoSwitchCycle
      $p.Reason | Should Be 'READY'
      Assert-MockCalled Start-AutoMode -Times 1 -Exactly -ParameterFilter { $ModeArgs -eq 'run -PodIp 203.0.113.24 -ControlPort 40123 -VideoPort 40124 -Auto' }
      Assert-MockCalled Invoke-DeviceRequest -Times 1 -Exactly -ParameterFilter { ($Method -eq 'POST') -and ($Body -match '"decision":"SWITCH_RUN"') -and ($Body -match '"switch_ok":true') }
      $st = Get-Content $AutoStateFile -Raw | ConvertFrom-Json
      $st.last.reason | Should Be 'READY'
      $st.last_switch.ok | Should Be $true
      ($st.last_good_at_ms -gt 0) | Should Be $true

      # 다음 판단: 같은 대상으로 관제 중이니 다시 전환하지 않는다
      $p2 = Invoke-AutoSwitchCycle
      $p2.Reason | Should Be 'RUNNING'
      Assert-MockCalled Start-AutoMode -Times 1 -Exactly
    }
  }

  Context '세션이 끝나도 유예 안에서는 그대로, 유예가 지나면 미리보기로 되돌린다' {
    It '세션이 끝나도 유예 안에서는 그대로, 유예가 지나면 미리보기로 되돌린다' {
      Reset $runText
      Mock Invoke-DeviceRequest { @{ Status = 200; Json = (SessionJson -State 'ENDED' -Link 'DISCONNECTED'); Error = '' } } -ParameterFilter { $Path -eq '/api/devices/gpu-session' }
      Mock Invoke-DeviceRequest { @{ Status = 204; Json = $null; Error = '' } } -ParameterFilter { $Method -eq 'POST' }
      Mock Start-AutoMode { [IO.File]::WriteAllText($ModeFile, $previewText, $Bom) }
      [IO.File]::WriteAllText($AutoStateFile, ('{"last_good_at_ms":' + ((Get-NowMs) - 60000) + ',"failures":[]}'), $Utf8)
      (Invoke-AutoSwitchCycle).Reason | Should Be 'GRACE'
      Assert-MockCalled Start-AutoMode -Times 0 -Exactly
      [IO.File]::WriteAllText($AutoStateFile, ('{"last_good_at_ms":' + ((Get-NowMs) - 301000) + ',"failures":[]}'), $Utf8)
      (Invoke-AutoSwitchCycle).Reason | Should Be 'SESSION_ENDED'
      Assert-MockCalled Start-AutoMode -Times 1 -Exactly -ParameterFilter { $ModeArgs -eq 'preview -Auto' }
    }
  }

  Context 'EC2 가 응답하지 않으면(재배포) 오래 관제 대상을 못 봤어도 그대로 둔다' {
    It 'EC2 가 응답하지 않으면(재배포) 오래 관제 대상을 못 봤어도 그대로 둔다' {
      Reset $runText
      Mock Invoke-DeviceRequest { @{ Status = 0; Json = $null; Error = 'WebException' } }
      Mock Start-AutoMode { }
      [IO.File]::WriteAllText($AutoStateFile, ('{"last_good_at_ms":' + ((Get-NowMs) - 3600000) + ',"failures":[]}'), $Utf8)
      (Invoke-AutoSwitchCycle).Reason | Should Be 'EC2_UNREACHABLE'
      Assert-MockCalled Start-AutoMode -Times 0 -Exactly
    }
  }

  Context '지문이 맞지 않으면 전환하지 않고 상태에 사유를 남긴다' {
    It '지문이 맞지 않으면 전환하지 않고 상태에 사유를 남긴다' {
      Reset $previewText
      Mock Invoke-DeviceRequest { @{ Status = 200; Json = (SessionJson); Error = '' } } -ParameterFilter { $Path -eq '/api/devices/gpu-session' }
      Mock Invoke-DeviceRequest { @{ Status = 200; Json = (EndpointJson ('sha256:' + ('c' * 64))); Error = '' } } -ParameterFilter { $Path -eq '/api/endpoints/tier0' }
      Mock Invoke-DeviceRequest { @{ Status = 204; Json = $null; Error = '' } } -ParameterFilter { $Method -eq 'POST' }
      Mock Start-AutoMode { }
      (Invoke-AutoSwitchCycle).Reason | Should Be 'PIN_MISMATCH'
      Assert-MockCalled Start-AutoMode -Times 0 -Exactly
      Assert-MockCalled Invoke-DeviceRequest -Times 1 -Exactly -ParameterFilter { ($Method -eq 'POST') -and ($Body -match '"reason_code":"PIN_MISMATCH"') -and ($Body -match 'cccccccc') }
      (Get-Content $AutoStateFile -Raw -Encoding UTF8 | ConvertFrom-Json).last.detail | Should Match '고정 목록에 없다'
    }
  }

  Context '수동으로 켠 관제는 새 세션이 그 전에 등록된 것이면 덮어쓰지 않는다' {
    It '수동으로 켠 관제는 새 세션이 그 전에 등록된 것이면 덮어쓰지 않는다' {
      Reset ('관제 Pod 198.51.100.7 제어 41001 영상 41002, 원격 시청 켬, 녹화 C:\x (11-14 09:00)') 'manual' (Get-NowMs)
      Mock Invoke-DeviceRequest { @{ Status = 200; Json = (SessionJson -RegAt ((Get-NowMs) - 60000)); Error = '' } } -ParameterFilter { $Path -eq '/api/devices/gpu-session' }
      Mock Invoke-DeviceRequest { @{ Status = 200; Json = (EndpointJson $pin); Error = '' } } -ParameterFilter { $Path -eq '/api/endpoints/tier0' }
      Mock Invoke-DeviceRequest { @{ Status = 204; Json = $null; Error = '' } } -ParameterFilter { $Method -eq 'POST' }
      Mock Start-AutoMode { }
      (Invoke-AutoSwitchCycle).Reason | Should Be 'MANUAL_HOLD'
      Assert-MockCalled Start-AutoMode -Times 0 -Exactly
      (Get-ModeOwner).Owner | Should Be 'manual'
    }
  }

  Context '전환이 시작 기록을 남기지 못하면 실패를 세고 30분에 3번이면 멈춘다' {
    It '전환이 시작 기록을 남기지 못하면 실패를 세고 30분에 3번이면 멈춘다' {
      Reset $previewText
      Mock Invoke-DeviceRequest { @{ Status = 200; Json = (SessionJson); Error = '' } } -ParameterFilter { $Path -eq '/api/devices/gpu-session' }
      Mock Invoke-DeviceRequest { @{ Status = 200; Json = (EndpointJson $pin); Error = '' } } -ParameterFilter { $Path -eq '/api/endpoints/tier0' }
      Mock Invoke-DeviceRequest { @{ Status = 204; Json = $null; Error = '' } } -ParameterFilter { $Method -eq 'POST' }
      Mock Start-AutoMode { }
      Mock Wait-ModeApplied { $false }
      1..3 | ForEach-Object { (Invoke-AutoSwitchCycle).Reason | Should Be 'READY' }
      (Invoke-AutoSwitchCycle).Reason | Should Be 'BACKOFF'
      Assert-MockCalled Start-AutoMode -Times 3 -Exactly
      @((Get-Content $AutoStateFile -Raw | ConvertFrom-Json).failures).Count | Should Be 3
    }
  }

  Context '사람이 친 run·preview·stop 은 주인을 manual 로, -Auto 는 auto 로, -KeepOwner 는 그대로 둔다' {
    It '사람이 친 run·preview·stop 은 주인을 manual 로, -Auto 는 auto 로, -KeepOwner 는 그대로 둔다' {
      Reset $previewText 'auto' 1
      Set-OwnerForCommand 'stop'
      (Get-ModeOwner).Owner | Should Be 'manual'
      $Auto = $true
      Set-OwnerForCommand 'run'
      (Get-ModeOwner).Owner | Should Be 'auto'
      $Auto = $false; $KeepOwner = $true
      Set-OwnerForCommand 'run'
      (Get-ModeOwner).Owner | Should Be 'auto'
    }
  }

  Context '이 기능 전부터 켜 둔 관제는 수동으로, 미리보기는 자동으로 넘겨받는다' {
    It '이 기능 전부터 켜 둔 관제는 수동으로, 미리보기는 자동으로 넘겨받는다' {
      Remove-Item (Join-Path $LocalDir '*') -Force -ErrorAction SilentlyContinue
      [IO.File]::WriteAllText($ModeFile, $runText, $Bom)
      Mock Invoke-DeviceRequest { @{ Status = 0; Json = $null; Error = 'x' } }
      [void](Invoke-AutoSwitchCycle)
      (Get-ModeOwner).Owner | Should Be 'manual'
      Remove-Item (Join-Path $LocalDir '*') -Force -ErrorAction SilentlyContinue
      [IO.File]::WriteAllText($ModeFile, $previewText, $Bom)
      [void](Invoke-AutoSwitchCycle)
      (Get-ModeOwner).Owner | Should Be 'auto'
    }
  }

  Context 'heal·갱신이 되살리는 명령은 모드 주인을 바꾸지 않는다(-KeepOwner)' {
    It 'heal·갱신이 되살리는 명령은 모드 주인을 바꾸지 않는다(-KeepOwner)' {
      $StateFile = Join-Path $LocalDir 'gateway-pod.json'
      [IO.File]::WriteAllText($StateFile, '{"PodIp":"203.0.113.24","ControlPort":40123,"VideoPort":40124}', $Utf8)
      [IO.File]::WriteAllText($ModeFile, $runText, $Bom)
      (Get-ResumeMode).Args | Should Be 'run -PodIp 203.0.113.24 -ControlPort 40123 -VideoPort 40124 -KeepOwner'
      [IO.File]::WriteAllText($ModeFile, $previewText, $Bom)
      (Get-ResumeMode).Args | Should Be 'preview -KeepOwner'
    }
  }
}
