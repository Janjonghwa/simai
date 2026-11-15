# S15P31A101-740 GPU 호스트 CPU 확인: 느린 서버를 화면에서 알린다

관련 명세: 3.2(Tier 0 우선·지연 감시), 3.12(GPU 실행 사이트: 호스트를 고를 수 없음). 앞 티켓 -699(세션 보고)·-700(부팅)·-702(관리자 GPU 패널).
관련 규칙: AGENTS 1(Tier 0 값은 건드리지 않는다: 이 티켓은 측정·표시만), 6(덮어쓰지 않음: 진행 사건 행에 추가만), 7(관측과 추정 구분: 못 잰 칸은 null).

## 문제

18차(2026-11-15): 같은 H100 SXM AP-JP-1 인데 1번째 Pod 은 Tier 0 판정 p95 29~45 ms(2~4명), 2번째 Pod(Intel Xeon Platinum 8460Y+, 160 논리 코어 호스트)은 같은 계산의 스레드 CPU 시간이 2배(교차 시점 평균 13 → 26 ms)라 p95 100~140 ms 가 되어 아바타 관문이 막혔다. RunPod 은 호스트를 고를 수 없고, 1번째 Pod 의 CPU 모델은 기록이 없다. 그래서 켠 뒤에 호스트 CPU 를 재서 느린 서버면 사람이 알고(끄고 다시 켜면 다른 호스트에 배정될 수 있음) 판단하게 한다. 자동으로 끄지는 않는다(조작 없음).

## 구현

| 위치 | 내용 |
|---|---|
| `deploy/scripts/simai_gpu_boot.py` | `cpu_model`(/proc/cpuinfo 첫 model name), `os.cpu_count`, `cpu_quota`(cgroup v2 `cpu.max`, 없으면 v1 `cfs_quota_us/period`, 제한 없음 null), `single_thread_score`. 등록 직후 데몬 스레드(`Boot.measure_cpu`)가 재서 `Reporter.set_host_cpu` 로 넘기고 다음 보고부터 `host_cpu` 를 싣는다(백그라운드라 WEIGHTS 단계를 늦추지 않는다). 실패는 로그만 남기고 계속한다. 보고 봉투 `schema_version` 은 1.1.0 |
| `contracts/messages/gpu_session_report.schema.json` | 선택 `host_cpu {model, logical_cpus, cpu_quota, single_thread_score, measured_at}`, 닫힌 객체, 못 잰 칸 null. 묶음 v7.61.0, 예시 2·반례 2, Python 생성 모델 갱신(TS 대상 아님) |
| `server` V80, `GpuSessionService` | `gpu_session_event.host_cpu jsonb`(추가만). 지금 값은 host_cpu 가 있는 가장 최근 PROGRESS 행. 관리자 `GET /api/gpu/sessions/current` 의 `session.host_cpu`(계약 밖, 없으면 null). 게이트웨이용 `gpu_session_status` 에는 싣지 않는다. 시험 `GpuSessionIntegrationTest` |
| `front/apps/admin/.../gpu` | `hostCpuWarning`: 열린 세션의 점수가 `HOST_CPU_SLOW_SCORE` 미만이면 GPU 정보 줄(상단 점 정보창·켜기·끄기 패널 공통)에 「CPU 가 느린 서버 — 사람이 많을 때 판정 지연이 늘 수 있음. 끄고 다시 켜면 다른 서버에 배정될 수 있어요」. 점수를 모르면 경고하지 않는다 |
| 시험 | `ai/tests/deploy/test_simai_gpu_boot.py`(파일 읽기·점수·보고에 실림·실패 무시), 서버 통합, front `gpuStatus.test.ts` |

## 측정 방법

점수 = 단일 스레드에서 초당 반복 수(클수록 빠름). 한 반복은 Tier 0 교차 시점과 비슷한 일이다: numpy 4개 점 × (6×4 행렬 SVD + lstsq 삼각측량 풀이) + 순수 Python 루프 2,000 걸음(부동소수·딕셔너리). 예열 1회 뒤 약 1.5 초(`BENCH_BUDGET_S`, 최소 3 반복, 너무 느리면 4.5 초에서 끊음) 돈다. 합쳐 2 초 이내(노트북 확인: 모델명·코어·몫 포함 전체 약 1.6 초). numpy 가 없으면 척도가 달라 점수를 내지 않는다(null). Tier 0 이미지는 ai venv(numpy 있음)로 이 스크립트를 돌린다. 측정 중 다른 스레드(캐시 받기)가 CPU 를 쓰면 점수가 낮게 나올 수 있어 한 번만 재고, 경고는 점수 하나가 아니라 사람이 보는 참고다.

## 기준값 (초기값 1,000, 조정 예정)

근거가 약하다는 점을 숨기지 않는다.
- 18차에서 확인된 것은 상대 차이뿐이다: 느린 Pod 의 같은 계산 CPU 시간이 빠른 Pod 의 2배. 빠른 Pod 의 CPU 모델·점수 기록은 없다.
- 이 측정기의 절대 값은 아직 어느 Pod 에서도 재지 않았다. 노트북(Windows, 22 논리 코어, numpy 2.5)에서 3번 재니 약 2,250~3,200(흔들림 큼)이었다. 서버 코어는 보통 노트북 터보보다 느리니 정상 서버는 이보다 낮을 것이다.
- 그래서 초기값은 노트북 점수의 0.4배쯤인 1,000 이다. 정상 서버가 이 아래로 나오면 거짓 경고(경고만 뜨고 아무것도 안 막힘), 느린 서버가 이 위로 나오면 놓친다.
- **첫 라이브 실측 뒤 조정한다**: 다음 GPU 세션마다 로그 `호스트 CPU: ... 점수 N` 과 관리자 응답 `session.host_cpu` 를 기록해 빠른 서버 값과 느린 서버(p95 가 크게 나온 서버) 값을 모으고, 둘 사이(빠른 쪽 약 0.7배)로 `HOST_CPU_SLOW_SCORE`(`front/apps/admin/src/features/gpu/gpuStatus.ts`)를 바꾼다.

## 버린 대안

- 서버가 기준을 판정해 `host_cpu.slow` 불리언을 싣기: 기준 조정마다 서버·계약을 건드려야 한다. 점수만 싣고 기준은 화면 상수로 둔다.
- 점수가 낮으면 자동으로 끄고 다시 켜기: 돈·재고 영향이 있고 오탐 위험이 크다. 이 티켓은 알림만(사용자 지시: 조작 없음).
- Tier 0 처리 지연으로 느린 서버 판정: 사람이 적을 때는 차이가 안 보여 켠 직후 알릴 수 없다. 이 측정은 부하 없이도 나온다.
- 측정 점수를 gpu_session 열에 갱신: 추가만 규칙(AGENTS 6)에 어긋난다. 진행 사건 행에 싣는다.

## 잔여 위험·넘긴 일

- 기준값은 초기 추정이다(위). 첫 라이브 실측 뒤 조정.
- 같은 호스트라도 이웃 Pod 부하에 따라 점수가 흔들릴 수 있다(한 번만 잰다). 반복 측정은 하지 않는다.
- 측정은 부팅 직후(WEIGHTS 시작 시점)라 같은 시간 캐시 받기·sha256 과 CPU 를 나눠 쓴다. 필요하면 측정 시점을 READY 뒤로 미룬다(그때는 Tier 0 가 CPU 를 쓰고 있어 더 어렵다).
- -741(`simai_bootstrap.py` Tier 0 재시작 명령)과 파일이 겹치지 않는다.
