# S15P31A101-599 SIM 입력의 실제 알림 채널 격리

관련 명세·규칙: AGENTS.md 규칙 5(LIVE 만 실제 알림), 명세 2장 원칙 10, 3.10 "점검·리허설"(`docs/spec/03-system.md`, 장면 연습은 녹화 뒤 REPLAY), 7장·부록 E(출처). 앞 설계 `docs/design/tier0-recording-replay.md`(file 입력 = REPLAY, 채널 닫음), `docs/design/g3-report.md`(SIM 입력 + 로컬 EC2 흉내·노트북 시험 상대).

## 문제

- `ai/services/tier0/service.py` `INPUT_ORIGINS = {"file": Origin.REPLAY}` 이고 나머지는 `Origin.LIVE` 다. 그래서 SIM(합성) 입력의 전이도 LIVE 출처다.
- `ai/services/tier0/settings.py` 는 file 입력만 I1·노트북 제어 채널을 끄고, SIM 은 환경 변수가 있으면 둘 다 연다.
- 운영 .env(EC2 I1 주소, 제어 포트, 듣는 주소 기본 `0.0.0.0`)가 남은 채 SIM 을 띄우면 합성 이벤트가 노트북 현장 경보·EC2·워치로 실제 LIVE 로 나간다(2026-11-11 감사 탐침: `mode sim control True origin LIVE`). `risk_event` 에 `input_provenance` 가 없어 하류에서 거를 수도 없다.

## 고른 안: SIM 은 루프백 채널만 연다(설정 단계에서 거부)

`load_settings` 가 SIM 입력일 때 I1 주소 host 와 제어 채널 듣는 주소가 모두 루프백(`127.0.0.0/8`, `::1`, `localhost`)인지 본다. 하나라도 아니면 `Tier0ConfigError` 로 시작하지 않는다(진입점 종료 코드 2, 이유와 어느 값인지 이름만 적고 비밀 값은 싣지 않음). 채널 값이 없으면(오프라인 SIM) 그대로 돈다.

근거:
- SIM + LIVE 출처는 의도된 시험 경로다. `ai/tests/services/tier0/test_tier0_e2e.py` 는 SIM 입력의 `local_alert.source == "LIVE"` 를 확인하고, G3 계측(`deploy/scripts/crash_recovery_g3.py`)도 SIM + 루프백 EC2 흉내·노트북 시험 상대로 LIVE 경로(I1 전달, 현장 경보 재동기화)를 잰다. 이 시험 상대는 모두 `127.0.0.1` 이다(`tier0_site.tier0_env` 의 제어 채널, `test_i1_tls.Ec2Server` 의 I1 주소).
- 위험은 "운영 주소로 나가는 것"이지 "LIVE 출처" 자체가 아니다. 주소를 루프백으로 묶으면 실제 노트북·EC2·워치에 닿을 수 없다.
- 계약(I1 LIVE 계약, `origin` enum)을 바꾸지 않는다. 코드 변경은 설정 검사 한 곳이다.
- 제어 채널은 기본 `0.0.0.0` 에서 듣는다. 운영 .env 에 듣는 주소를 따로 적지 않았으면 SIM 은 그것만으로 거부된다(가장 흔한 실수 모양을 잡는다).

## 버린 대안

- (a) SIM 출처를 REPLAY 로 바꾸기: REPLAY 는 현장 경보·I1 로 아무것도 보내지 않으므로(`test_tier0_file_replay.py`) SIM e2e·G3 계측이 재려는 LIVE 경로(현장 경보, I1 전달, 재동기화)를 더는 시험할 수 없다. 계약 enum 에 SIM 전용 출처를 더하는 것은 I1 LIVE 계약 변경(Lead 승인)이라 이번 범위 밖이다.
- 거부 대신 경고만 하고 채널을 닫기: 시험자가 경고를 놓치면 "EC2 연결이 안 된다"로 오해한다. 사용법 오류로 멈추는 편이 원인이 바로 보인다.
- 명시적 시험 허용 목록 환경 변수(비루프백 시험 EC2 허용): 그 변수가 운영 .env 에 같이 남으면 같은 사고가 난다. 원격 시험 상대가 필요하면 SSH 로컬 포워드로 루프백에 붙인다(아래 잔여 위험).

## 시험

`ai/tests/services/tier0/test_sim_channel_isolation.py`:
- 루프백 판정 단위(127.x, ::1, `[::1]`, localhost 는 참 / `0.0.0.0`, `::`, 사설 IP, 도메인, `localhost.<도메인>` 은 거짓)
- SIM + 운영형 env → `Tier0ConfigError`, 오류에 토큰 없음. I1 만·제어만·한쪽만 루프백인 조합도 거부
- SIM + 루프백(IPv4·localhost·IPv6) → I1·제어 설정이 그대로(기존 e2e 경로)
- SIM 채널 없음 → 오프라인 그대로 / file + 운영형 env → 두 채널 없음 / live + 운영형 env → 두 채널 그대로
- 진입점 `python -m services.tier0` SIM + 운영형 env → 종료 코드 2

`test_tier0_entry.py::test_i1_path_is_configurable_and_gateway_id_is_shared` 는 SIM 설정에 `ec2.example.com` 을 넣어 gateway_id 공유만 봤다. 이번 규칙에 맞게 그 확인만 루프백 주소로 바꿨다(I1Settings 경로 확인은 그대로).

## 잔여 위험

- SIM 이벤트는 여전히 LIVE 출처로 저널·상태 디렉터리에 남는다. 같은 `state_dir` 를 운영 LIVE 와 공유하면 복구 때 섞일 수 있다(file 입력처럼 `replay/` 로 나누지 않았다). 운영 Pod 에서 SIM 을 돌리지 않는 운영 절차로 막는다.
- SSH 로컬 포워드(`-L 127.0.0.1:…:<운영 EC2>`)로 운영 EC2 를 루프백에 붙이면 검사를 통과한다. 의도적 우회는 막지 않는다.
- `g3-report.md` D-G3-07(Pod 교체) 항목의 "시험 EC2" 를 비루프백 주소로 SIM 과 쓰려면 이제 거부된다. 그 항목을 실행할 때 EC2 흉내를 같은 Pod 루프백에 띄우거나 포워드를 쓴다.
- `localhost` 는 이름 해석에 맡긴다(hosts 파일을 바꾼 환경은 고려하지 않음).

## 넘긴 일

- 명세 3.10 "점검·리허설" 또는 7장 출처 규칙에 "SIM(합성) 입력은 시험 전용이며 실제 알림 채널(I1·노트북 제어)은 루프백 시험 상대에만 연다"를 적는 일은 명세 정리 MR 로 넘긴다.
- `risk_event` 에 입력 출처(SIM 여부)를 실어 하류가 거를 수 있게 하는 것은 계약 변경이라 별도 논의.
