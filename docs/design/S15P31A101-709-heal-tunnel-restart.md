# S15P31A101-709 heal 이 멀쩡한 역터널을 5분마다 재시작하던 결함

관련 명세: 3.12(노트북↔EC2 경로). 앞 티켓: -394(역터널), -424(heal·터널 로그 해석), -701(heal 예약 작업 첫 등록 경로).

## 증상(16차 GPU 세션, 11-14)

`gw.sh status`: 원격 역터널 작업=Running, ssh=살아 있음, 마지막 연결=기록 없음. heal 로그 23:12·23:17·23:22 「역터널 재시작(마지막 성공 오래됨(기록 없음))」.
heal 예약 작업(`simai-gateway-heal`)이 11-14 21:37 에 이 노트북에 처음 등록되면서 드러났다. 재시작마다 EC2 경유 SSH·scp(대용량 업로드)가 끊겼다.

## 원인

1. **기록을 쓰는 쪽**: 역터널 스크립트(`C:\simai-local\gateway-remote-tunnel.ps1`)가 `ssh -R` 직전에 `yyyy-MM-dd HH:mm:ss 연결`, 끝나면 `끊김(코드 n)` 을 쓴다.
   이 파일은 6(원격 열기 설정, `Do-RemoteSetup`)만 다시 쓴다. -394 판은 `Add-Content` 에 `-Encoding` 이 없어 PS 5.1 기본인 시스템 ANSI(CP949)로 썼고,
   -424 판부터 UTF-8 로 쓴다. `gw.sh update`(git pull)는 이 파일을 바꾸지 않는다. heal 이 21:37 에 처음 생긴 것으로 보아 6 은 -424 전(11-02)에 마지막으로 돌았다고 추정한다(6 은 -424 부터 heal 을 등록한다).
2. **기록을 읽는 쪽**: `Get-TunnelState` 가 로그를 `Get-Content -Encoding UTF8` 로 읽어 CP949 `연결` 이 깨지고, `Get-TunnelLogInfo` 는 연결 줄을 못 찾아
   마지막 연결 '기록 없음', 마지막 성공은 로그 첫 줄 시각(오래됨)이 된다. 로그가 아예 없으면 `Get-TunnelState` 가 null 을 `[datetime]::MinValue` 로 바꿨다.
3. **판정**: `Get-HealPlan` 이 `ssh 죽음 OR 마지막 성공 300초 넘음` 이면 재시작해, 모르는 기록을 오래된 기록으로 보고 ssh 가 살아 있어도 재시작했다.
   재시작 뒤에도 옛 스크립트가 다시 CP949 로 쓰므로 5분마다 되풀이됐다. 노트북에서 -394 와 같은 `Add-Content` 로 재현했다(옛 읽기: 연결 없음·첫 줄 시각, 새 읽기: 연결 유지 중).

## 바꾼 것

- `Get-HealPlan`: 재시작은 ① ssh 프로세스가 없을 때, ② ssh 는 살아 있지만 기록이 최근(300초)이 아니고 EC2 가 `127.0.0.1:2222` 리슨이 **없다고 답했을 때**만.
  기록 없음·오래됨만으로는, EC2 에 못 붙어 모를 때는 재시작하지 않는다(`-TunnelEc2Listening` null). ssh 가 살아 있으면 ServerAliveInterval 15·CountMax 3 이라 끊긴 연결은 45초 안에 ssh 가 끝나고 루프가 다시 붙는다.
- `Get-TunnelState`: 기록 없음을 null(모름)으로 둔다(MinValue 변환 제거).
- `ConvertFrom-TunnelLogBytes`·`Read-TunnelLogLines`: 줄마다 UTF-8(엄격)로 읽고 아니면 시스템 ANSI 로 읽는다. 쓰는 중에도 읽게 공유 모드로 연다. 중간에 낀 BOM 도 벗긴다.
  그래서 옛 판 스크립트 그대로여도 status 의 마지막 연결이 바로 보이고 기록이 최근으로 잡혀 EC2 확인도 거의 돌지 않는다.
- `Test-TunnelListeningOnEc2`(ssh 는 살아 있고 기록이 최근이 아닐 때만, heal 1회당 ssh 1번), `Get-GatewayEc2SshArgs`: SYSTEM 이 게이트웨이 사용자의 설정·키·`known_hosts` 로 EC2 에 붙는다.
  `Invoke-FreePortsOnEc2` 도 같은 인자를 쓴다(전에는 SYSTEM 의 known_hosts 에 EC2 가 없어 BatchMode 에서 조용히 실패했을 수 있다).

## 버린 대안

- **역터널 스크립트가 주기적으로 생존을 기록**: 게이트웨이의 스크립트 파일은 6 을 다시 돌려야 바뀌어 지금 노트북에 바로 듣지 않는다. 읽는 쪽에서 옛 판 줄을 읽는 편이 update 만으로 고쳐진다.
- **heal 이 스크립트 파일을 새 판으로 덮어쓰기**: SYSTEM 이 사용자 작업의 스크립트를 바꾸는 경로가 하나 더 생긴다. 읽기 보정으로 충분해 하지 않았다.
- **기록이 오래되면 무조건 재시작(예전)**: 이번 결함 그 자체.

## 운영 적용

- 개발 노트북에서 `gw.sh update` 만 하면 된다. heal 예약 작업은 저장소의 `gateway\ops\simai-gateway.ps1` 경로를 실행하므로 **다시 등록할 필요 없다**. 다음 heal(5분 안)부터 새 판정이다.
- 확인: `gw.sh status` 의 마지막 연결이 `… (연결 유지 중)` 으로 보이고, `gw.sh logs heal` 에 5분마다 `역터널 재시작` 이 더 쌓이지 않는다.
- 선택: 현장에서 6 을 다시 돌리면 역터널 스크립트가 UTF-8 판으로 바뀐다(역터널이 한 번 끊긴다). 급하지 않다.

## 남은 위험

- ssh 는 살아 있는데 터널이 실제로는 안 되는 경우(EC2 쪽 포워딩만 죽음)는 EC2 확인이 `false` 로 답해야 잡힌다. EC2 에 못 붙으면(모름) 재시작하지 않으므로 그동안은 ssh 의 ServerAlive(45초)에 기댄다.
- ssh 가 재접속 사이 5초 쉬는 순간에 heal 이 보면 재시작한다(예전과 같음, 터널이 어차피 끊긴 순간이라 해는 작다).
- 게이트웨이 노트북 실기에서는 돌려 보지 않았다(SYSTEM 으로 EC2 확인이 붙는지 포함). 붙지 않으면 결과는 '모름'이라 재시작하지 않는 쪽으로 안전하다.

## 코드·시험 위치

`gateway/ops/simai-gateway.ps1`(`Get-HealPlan`, `Test-TunnelRecordFresh`, `ConvertFrom-TunnelLogBytes`, `Read-TunnelLogLines`, `Get-TunnelState`, `Test-TunnelListeningOnEc2`, `Get-GatewayEc2SshArgs`, `Do-Heal`),
`gateway/ops/simai-gateway.Tests.ps1`(Get-HealPlan·Get-TunnelLogInfo 의 -709 시험), `gateway/ops/README.md`(heal 3번 항목).
