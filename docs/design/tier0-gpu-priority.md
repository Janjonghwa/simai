# Tier 0 GPU 우선 보호 (S15P31A101-60)

Tier 0 와 분석 워커가 GPU Pod 하나를 나눠 쓸 때 Tier 0 를 먼저 지키는 구조다. 근거: 명세 3.2(GPU 공유 규칙·메모리 예산표),
6.6(지연 예산 capture_to_risk p95 600ms), 20.5(안전 경로 격리), 15.1 G0(VLM·분석 워커 동시 실행 시 capture_to_risk·메모리 부족),
부록 D D-G0-08(분석 워커 강제 종료 시 판정 지속 100%).

| 규칙(명세 3.2) | 구현 |
|---|---|
| Tier 0 가 GPU 메모리를 먼저 확보 | `simai_ai.gpu.reserve.GpuReservation`: Tier 0 기동 첫 단계에서 torch CUDA 캐시에 `gpu.reserve_mb`(기본 8192, `SIMAI_TIER0_GPU_RESERVE_MB`)를 잡아 둔다 |
| 분석 워커는 상한 안에서 | 분석 워커 시작 때 `torch.cuda.set_per_process_memory_fraction`(예산 기본 0.70, 동시 실행 시 워커 몫 0.30/0.40·최소 보장 0.10), VLM 은 vLLM `gpu_memory_utilization`(기본 0.30, FP8 가중치 10.05 GiB)·`max_num_seqs`(4) — `simai_ai.gpu.budget` |
| 지연이 목표를 넘으면 새 작업을 멈춤 | Tier 0 가 표시 파일에 지연 표본을 싣고, 분석 워커가 대여 요청 전·작업 시작 전에 읽는다(`AdmissionGate`) |
| LIVE 중 EXP 금지 | GPU 실험 큐(`gpu_queue.py` LIVE 보호)와 분석 워커(LIVE 중 `static_reconstruct`·`hmr_*` 를 받지 않음·도는 것은 멈춤) |
| 분석 워커가 죽어도 Tier 0 계속 | 별도 프로세스, 신호는 파일 하나뿐(Tier 0 는 분석 워커를 기다리지 않는다) |

## 신호 경로: 표시 파일

같은 Pod·같은 볼륨이라 Tier 0 → 분석 워커 신호는 기존 표시 파일 `$SIMAI_ROOT/state/tier0.live` 에 싣는다. EC2·I5 계약을 바꾸지 않는다.
표시 파일은 원자적 교체로 5초마다 갱신되고(정리 주기), 정상 종료하면 지운다.

```jsonc
"gpu_priority": {
  "version": 1,
  "phase": "starting | running | start_failed | shutdown_failed",
  "pid": 1234, "written_at": 1760342400000,
  "reserve": {"state": "OK | PARTIAL | LOST | SKIPPED | DISABLED | FAILED | PENDING", "target_mb": 8192, "reserved_mb": 8192.0, "device": 0, "reason": null},
  "load": {"samples": 5, "unknown": 0, "lag_ms_max": 212.0, "lag_ms_p95": 212.0, "lag_ms": [180.0, ...],
           "over_budget": 0, "budget_ms": 600, "queue_wait_exceeded": null}
}
```

- `phase=starting`: Tier 0 `start()` 첫 단계(선확보 전)에 쓴다. GPU 실험 큐는 표시 파일이 있으면 LIVE 로 보므로 Tier 0 기동 중에도 EXP 를
  시작하지 않는다. 기동이 실패하면 지운다(이전 실행이 비정상 종료로 남긴 표시 파일이었으면 `start_failed` 로 남긴다).
- `load`: 정리 주기(1초)마다 보정 T_lag(`HealthReport.lag_ms` = now − t_latest_global, 촬영 → 판정 구간)를 넣고 표시 파일을 쓸 때 꺼낸다.
  시계 차이를 모르거나 프레임이 없어 lag 를 모르면 `unknown` 으로 센다. `queue_wait_exceeded` 는 LIVE 입력(-182 `I0Ingest`)이
  추론 대기 상한(150ms)을 넘겨 버린 프레임(`SKIPPED_BY_POLICY` 중 `QUEUE_WAIT_EXCEEDED`, 카메라 합)의 직전 표본 뒤 증가분이다(`SUPERSEDED` 밀림은 넣지 않음).
  누계를 읽지 못하면 그 표본의 lag 도 unknown 으로 센다. SIM 입력은 대기열이 없어 null 이다. 대기 상한을 넘긴 프레임은 버려지므로 그 정체는
  t_latest_global 정체 → lag 증가로도 보인다.

## 분석 워커 접수 규칙 (AdmissionGate)

앞에서 걸리면 그 이유로 받지 않는다. **모르면 받지 않는다.**

1. 표시 파일 없음 → 기본은 모름 → 받지 않는다. 파일이 없다는 것만으로는 '관제 없음'과 'Tier 0 가 표시 파일을 못 씀(볼륨 오류)·엔진 준비 중'을
   구별할 수 없다. 관제가 없는 개발 시간에는 운영자가 `SIMAI_WORKER_DEV_NO_LIVE=1` 로 명시하고, 그때만 LIVE 아님 → 모든 유형을 받는다
   (그 모드에서도 LIVE 를 본 뒤 `live_linger_s`(60초) 안이면 재시작 틈으로 보고 받지 않는다). Tier 0 는 표시 파일 쓰기 실패를 판정을 멈추지 않고
   로그(바뀔 때만)와 `status.marker_error` 로 알리고, 같은 상태를 health `tier0_warnings` 의 `LIVE_MARKER_WRITE_FAILED`(계약 health 2.1.0, S15P31A101-197)로
  싣는다(다음 쓰기가 성공하면 빠짐, 정보 경고라 판정·알림은 그대로).
   S15P31A101-403: Tier 0 가 정상 종료(모든 종료 단계 성공)하면 표시 파일을 지운 뒤 옆에 `tier0.live.stopped`(정상 종료 기록)를 남기고, 접수 문은
   표시 파일이 없고 이 기록이 읽히면 '정상 종료로 끝남' → LIVE 아님으로 본다(기록 없음·깨짐은 지금처럼 모름). Tier 0 시작 경로가 GPU 를 잡기 전에 지운다.
   관제 뒤 배경 복원(`simai-bootstrap after-live`)이 이것으로 개발 플래그 없이 돈다(설계 `worker-static-reconstruct.md` -403 절).
2. 확인·읽기 실패, `max_marker_age_s`(15초, 갱신 주기의 3배)보다 오래됨, `gpu_priority` 없음·형식 틀림 → 모름 → 받지 않는다(LIVE 로 본다).
3. `phase != running`, 선확보가 OK·SKIPPED·DISABLED 가 아님 → 받지 않는다.
4. 표본 0, unknown ≥ 1, `lag_ms_max` > 예산(600ms) → 받지 않는다(창 최댓값: p95 보다 엄격, 한 창·한 표본이라도 즉시).
5. `queue_wait_exceeded` 가 창당 `max_queue_wait_exceeded`(2) 이하면 통과로 넘기고(안정 시계를 지우지 않음), 넘으면 받지 않는다(S15P31A101-412).
6. 멈춘 뒤에는 통과 상태가 `resume_after_s`(10초) 이어져야 받는다(깜빡임 방지).

분석 워커는 위 규칙을 쓴다. 상시 아바타는 아래 S15P31A101-498 절의 처리 부하 기준을 쓴다. 판정 사유 코드(`Admission.code`)와 계측(`AdmissionGate.stats()`)은
아래 S15P31A101-412 절에 있다.

워커 동작(`services/worker/runner.py`):

- 대여 요청마다(네트워크 오류 뒤 다시 보낼 때도) 접수 문을 본다. 받지 않으면 대여를 요청하지 않는다(EC2 는 작업을 대기열에 둔다).
  LIVE 중에는 대여 요청 목록에서 EXP 유형을 빼고 long-poll 을 5초로 줄인다.
- long-poll 사이에 상황이 바뀌면: 받은 EXP 작업은 바로, 지연 때문이면 `hold_max_s`(60초, 실행 상한의 절반 이하) 동안 하트비트로 대여를 지키다가
  반납한다(`LIVE_EXP_BLOCKED`·`TIER0_PRIORITY`). 시작하지 않았으므로 `started_at` 은 null.
- 반납은 retryable FAILED + `error.consume_attempt: false`(계약 job_callback 2.1.0, S15P31A101-197)다. EC2 는 시도 횟수를 쓰지 않고 같은 attempt 로
  다시 내주므로 마지막 시도(attempt == max_attempts)도 다른 시도와 같이 반납한다(최종 FAILED 가 되지 않는다). 처리기 실패·시간 초과는 반납이 아니다.
  이 의미(attempt = 반납을 세지 않는 시도 번호)는 `job_request` 3.0.0(메이저)이다. 워커는 3.x 가 아닌 `job_request` 를 대여 요청 오류(`RequestRejected`)로 보고 멈춘다(2.x EC2 와 섞어 돌리지 않음).
- 도는 EXP 작업은 별도 감시 스레드가 `live_poll_s`(2초)마다 본다(하트비트 HTTP 대기와 무관). LIVE 가 시작되면 바로 취소를 알리고 `LIVE_PREEMPTED` 로
  반납한다(마지막 시도여도). 지연 초과는 새 작업만 멈춘다(도는 VLM 은 끝까지).
- 접수 중지가 이어지면 5분마다 경고를 다시 남기고(`Tier 0 우선 접수 중지 N분째`), `Worker.paused_for_s` 로 이어진 시간을 읽을 수 있다.

## 아바타 전용 처리 부하 접수 (S15P31A101-498)

2026-11-05 사용자 승인으로 아바타 접수를 촬영→판정 끝단 지연에서 Tier 0 처리 부하로 바꿨다. 끝단 지연에는 카메라·업링크·Pod 전송 지연이 섞여 있으므로 GPU/처리 여유를 판별하기 어렵다. 위치·위험 판정과 제품 끝단 지연 예산은 그대로이며, 분석 워커/VLM의 접수 기준도 그대로다. 명세 §3.2 문구는 별도 정리가 필요하다.

표시 파일에 다음 로컬 진단 필드를 추가한다. 외부 I1 health나 계약 스키마는 바꾸지 않는다.

```jsonc
"processing": {
  "judge": {"samples": 50, "unknown": 0, "lag_ms_max": 38.2},
  "detect": {"samples": 70, "unknown": 0, "lag_ms_max": 61.4},
  "detect_required": true
}
```

실제 표본은 `LoadWindow`와 같은 요약 형식이다. 여기의 `lag_ms_*`는 호환된 창 자료구조의 키 이름이며 **처리 소요 시간**을 뜻한다. 주입된 단조 시계로 판정 `judge.tick`과 검출·로컬 추적·Re-ID 묶음 `feed.detect_once`를 각각 잰다. GPU 사용률을 추정한 값이 아니라 CPU·GPU 경합이 처리 시간에 미친 결과를 보는 대리 신호다. 입력 대기열에서 기다린 시간·촬영 시각·네트워크 전송 시간은 포함하지 않는다. 판정 lock 대기와 처리 중 I/O는 포함하므로 바쁜 Tier 0를 보수적으로 보호한다.

- `AvatarAdmissionGate`는 판정 창 최댓값 **100ms**(기본 판정 주기 10Hz), 검출 창 최댓값 **150ms**(기존 추론 대기 상한과 같은 처리 지체 상한)를 쓴다. 실측 튜닝 전의 명시적 초기값이며, 전송 지연 600ms를 처리 시간 예산으로 재사용하지 않는다. 사람 수/Re-ID로 실제 처리 시간이 늘면 계속 물러난다.
- 별도 검출기가 있는 LIVE/파일 입력은 두 창을 모두 요구한다. SIM 입력은 `detect_required=false`이고 판정 창만 요구한다. 표본 없음·unknown·잘못된 형식·구버전 표시 파일은 아바타를 멈춘다. 창을 읽는 시점에 진행 중 작업의 경과도 넣어 멈춘 작업을 정상으로 보지 않는다.
- 기존 파일 신선도(15초), 실행 phase, GPU 선확보 상태, 10초 안정 재개, I5 LIVE EXP 차단은 유지한다. 기존 `queue_wait_exceeded`는 카메라 입력 폭주·전송 묶임에도 증가하므로 아바타에는 직접 적용하지 않고 측정한 검출 처리 시간으로 보호한다. 분석 워커는 기존 큐 대기 규칙을 계속 쓴다.
- 표시 파일은 기존 5초 주기에 갱신한다. 부하 초과가 파일에 반영되면 다음 게이트 검사에서 즉시 중지하지만, 발생부터 관측까지 최대 한 갱신 주기 지연이 있다. 오래된 표시 파일은 15초 뒤 모름으로 중지한다. 뷰어는 새 포즈가 없으면 기존 0.5초 기준으로 캡슐로 돌아간다.
- 포즈 스레드와 외형 작업자가 같은 게이트를 호출하므로 `check`·`stats`를 직렬화해 안정 대기·계측 상태를 보호한다. 외형 계산과 업로드에는 게이트 잠금을 잡고 있지 않는다.

시험: `tests/gpu/test_avatar_admission.py`(끝단 5초·낮은 처리 부하 통과, 처리 초과·모름·예약 실패·표시 파일 만료 차단, 진행 중/실패한 처리 표본), `tests/services/tier0/test_tier0_gpu_priority.py`(실제 Tier 0 표시 파일 조립), 기존 분석 워커 우선 보호 회귀 시험. 실제 GPU에서 1/4인 동시 HMR·외형을 켜고 처리 창·포즈 주기·Tier 0 p95를 재측정해야 한다.

## 아바타 게이트 p95 지속 초과와 히스테리시스 (S15P31A101-547)

### 배경 (2026-11-08 H100 녹화 재생 실측)

11-08 H100 세션(`.agy/s13r-tier0-status.json`, `.agy/s13r-judge-max-per-window.txt`)에서 `stage_timing.judge_tick`은 인원수별로 다음과 같이 측정되었다.

- **0명(2,811회):** 평균 `0.3 ms`, p95 `0.3 ms`, 최대 `1.4 ms`
- **3명(499회):** 평균 `19.9 ms`, **p95 `97.4 ms`**, **최대 `262.3 ms`** (`cross_view_2d_triangulation` 최대 `103.8 ms`, `world_track` 최대 `170.3 ms`)
- **4명(2,254회):** 평균 `135.9 ms`, **p95 `233.4 ms`**, 최대 `555.4 ms`
- **5명(10,702회):** 평균 `189.3 ms`, **p95 `333.0 ms`**, 최대 `514.0 ms`
- **6명(78회):** 평균 `203.1 ms`, **p95 `421.7 ms`**, 최대 `496.0 ms`

3명 구간에서는 `judge_tick` p95가 `97.4 ms ≤ 100 ms`로 10Hz 판정 주기를 지키고 있었으나, 5초 창(약 50개 tick) 안에 단 1~2회 Re-ID·삼각측량 단발 튐(최대 `262.3 ms`)이 섞이면서 창 최댓값(`lag_ms_max`)은 매 창 `100 ms`를 넘었다. 그 결과 아바타 서비스 로그의 1분 주기 창 최댓값 45개(`205.9 ~ 566.0 ms`, 중앙값 `336.2 ms`)가 모두 `100 ms`를 초과해 45분 내내 차단되었고 포즈 송신이 `0`건이었다.

### 변경 규칙과 표시 파일 형식

```jsonc
"processing": {
  "judge": {"samples": 50, "unknown": 0, "lag_ms_p95": 31.4, "lag_ms_max": 182.0},
  "detect": {"samples": 70, "unknown": 0, "lag_ms_p95": 54.2, "lag_ms_max": 141.6},
  "detect_required": true
}
```

1. **처리 창(`ProcessingWindow`) p95·표본 수 유지:** `ProcessingWindow`는 `LoadWindow` 요약(`samples`, `unknown`, `lag_ms_p95`, `lag_ms_max`, `lag_ms`, `over_budget`, `budget_ms`)을 그대로 내보내며, 5초 창에서 15~30Hz 검출 표본(75~150개)이 잘리지 않도록 창 보관 상한을 `DEFAULT_PROCESSING_MAX_VALUES = 256`(`LoadWindow` 기본 64에서 확대)으로 둔다. 진행 중 경과 표본 추가 규칙과 옛 아바타 서비스 호환용 `samples`·`unknown`·`lag_ms_max` 필드는 그대로 유지한다.
2. **구버전 표시 파일 호환:** `AvatarAdmissionGate`가 `lag_ms_p95` 키가 없는 옛 표시 파일을 읽으면 `lag_ms_max`(임의 분포에서 $\text{p95} \le \max$이므로 보수적 상한)로 폴백하여 같은 히스테리시스·극단값 규칙을 적용한다. 반대로 `lag_ms_p95` 키가 있는데 값이 `null`·비정상 숫자·음수이면 형식 오류(`lag_unknown`)로 즉시 차단한다.
3. **히스테리시스 상태 기계(게이트 객체 보유):**
   - **열림(기본·재개 후):** `judge`(및 `detect_required`면 `detect`)의 `lag_ms_p95`가 예산을 넘는 상태가 **연속 `T_block`초(`over_budget_hold_s`)** 이어질 때만 차단(`over_budget`) 상태로 전이한다. `T_block` 미만의 단발 창 초과는 통과시킨다.
   - **차단 후 재개:** 일단 차단되면 모든 필수 단계의 `lag_ms_p95`가 예산 이하이고 정상인 상태가 **연속 `T_resume`초(`policy.resume_after_s`)** 이어져야 열림 상태로 재개한다(`stabilizing` → `ok`). 안정 대기 중 한 번이라도 `p95 > 예산`이 오면 재개 시계를 초기화한다.
   - **즉시 차단:** 표시 파일 없음/만료/깨짐, `phase != running`, GPU 선확보 실패, `samples == 0`, `unknown > 0`, 그리고 **창 최댓값 `lag_ms_max >= AVATAR_EXTREME_LAG_MS`** 인 극단적 멈춤은 `T_block`을 기다리지 않고 즉시 차단한다.
   - 분석 워커·VLM의 `AdmissionGate`(`gpu_priority.load` 창 최댓값 600ms 즉시 차단)와 위험 판정 경로는 바꾸지 않는다.

### 정한 수치와 근거

| 항목 | 상수 / 설정 | 값 | 근거 |
|---|---|---|---|
| 판정·검출 p95 예산 | `AVATAR_JUDGE_BUDGET_MS` / `AVATAR_DETECT_BUDGET_MS` | **100 ms** / **150 ms** (유지) | 판정 100ms는 Tier 0 10Hz 판정 주기(`tick_ms=100`)와 일치한다. 11-08 실측에서 3명 p95는 `97.4 ms ≤ 100 ms`(평균 `19.9 ms`로 10Hz 유지), 4~5명 p95는 `233.4 ~ 333.0 ms`(평균 `135.9 ~ 189.3 ms`로 10Hz 주기 초과)여서 100ms가 정상(≤3명)과 지속 과부하(4~5명)를 가르는 물리적 경계다. 검출 150ms는 `queue_wait_ms`(명세 5.1) 상한과 같다 |
| 연속 초과 차단 시간 `T_block` | `AVATAR_OVER_BUDGET_HOLD_S` | **10.0 s** | 표시 파일 갱신 주기가 **5초**(`MARKER_EVERY_S=5.0`)이므로, 단일 5초 창 하나가 디스크에 5초 동안 유지된다. 3명 수준(모집단 p95 `97.4 ms`)에서도 50개 표본의 유한 표본 변동으로 단일 창의 표본 p95(48번째 값)가 100ms를 넘을 확률이 약 28%다. `T_block = 10.0s`(연속 2개 창, 약 100 tick 지속 초과)로 두면 단일 창 요동은 흡수하고, 4~5명 지속 과부하에서는 2주기(10초) 만에 확실히 차단한다 |
| 연속 정상 재개 시간 `T_resume` | `AdmissionPolicy.resume_after_s` | **10.0 s** (유지) | 차단 뒤 5초 표시 파일 두 창(10초) 동안 연속으로 `p95 ≤ 예산`을 확인한 뒤 재개하여 경계 부근 열림/닫힘 진동(뷰어 캡슐 깜빡임)을 막는다(-412와 동일) |
| 극단적 멈춤 즉시 차단 상한 | `AVATAR_EXTREME_LAG_MS` | **1000 ms** (유지) | 5초 창(28~50 표본) 끝에서 판정·검출이 1~4초 동안 멈추면(또는 진행 중 작업이 정체되면) 나쁜 표본이 1개뿐이라 창 `lag_ms_p95`는 여전히 20~30ms로 낮게 나올 수 있다. 11-08 H100 실측에서 3~6명 전 구간의 `judge_tick` 최댓값은 `555.4 ms`(45개 창 로그 최댓값 `566.0 ms`), `detect_trt` 최댓값은 `387.9 ms`였다. 따라서 `lag_ms_max >= 1000 ms`(10Hz 주기의 10배이자 제품 `capture_to_risk` 예산 600ms 초과)는 다인 추론 변동이 아니라 1초 이상 멈춤(hang/stall)이므로 즉시 차단한다 |

### 11-08 분포 기반 시뮬레이션 (45분 = 5초 창 540개, 1초 주기 게이트 검사)

11-08 `.agy/s13r-tier0-status.json`의 인원별 `judge_tick` 분포(평균·p95·최대 일치 모형, 시드 고정 `20261109`)와 `.agy/s13r-judge-max-per-window.txt`(45개 창 최댓값)에 대해 기존 게이트(`lag_ms_max > 100ms` 즉시 차단)와 변경 게이트(`lag_ms_p95 > 100ms` 연속 `T_block=10s` 차단, `T_resume=10s` 재개, `lag_ms_max >= 1000ms` 즉시 차단)를 비교한 결과다.

| 시나리오 (11-08 실측 모수) | 창당 표본 수 | 창 `max > 100ms` 비율 | 창 `p95 > 100ms` 비율 | 기존 게이트 통과율 (`max > 100` 즉시 차단) | **변경 게이트 통과율 (`p95`, `T_block=10s`, `T_resume=10s`)** | 정상 상태 판정 |
|---|---:|---:|---:|---:|---:|---|
| **0명** (`mean 0.3, p95 0.3, max 1.4 ms`) | 50 | 0.0% | 0.0% | 99.6% (기동 후 100%) | **100.0%** | 전 구간 통과 |
| **3명** (`mean 19.9, p95 97.4, max 262.3 ms`) | 50 | 85.7% | 28.3% | **0.4%** (사실상 전면 차단) | **92.2%** | **대부분 통과** (단발 최댓값·단일 창 요동 흡수) |
| **4명** (`mean 135.9, p95 233.4, max 555.4 ms`) | 35 | 100.0% | 100.0% | 0.0% | **0.4%** (초기 `T_block` 10초 뒤 **100% 차단**) | **지속 과부하 차단** |
| **5명** (`mean 189.3, p95 333.0, max 514.0 ms`) | 28 | 100.0% | 100.0% | 0.0% | **0.4%** (초기 `T_block` 10초 뒤 **100% 차단**) | **지속 과부하 차단** |
| **6명** (`mean 203.1, p95 421.7, max 496.0 ms`) | 25 | 100.0% | 100.0% | 0.0% | **0.4%** (초기 `T_block` 10초 뒤 **100% 차단**) | **지속 과부하 차단** |
| **11-08 로그 45개 창** (`s13r-judge-max-per-window.txt`, `205.9 ~ 566.0 ms`, 구버전 `max` 폴백 재생) | 45개 창 | 100.0% (45/45) | — (로그에 p95 없음, `max` 폴백 시 100%) | 0.0% (0/45) | **4.4%** (첫 10초 2개 창 후 **43/45 차단**) | 구버전 표시 파일 폴백 시에도 지속 과부하 차단 |

#### 가용 자료의 한계

1. `.agy/s13r-judge-max-per-window.txt`는 아바타 서비스가 60초마다 남긴 `아바타 시작 대기` 로그에서 추출한 45개 창의 `judge.lag_ms_max` 값뿐이다. 당시 로그 형식은 `lag_ms_p95`나 창 내부 개별 tick 배열(`lag_ms`), 같은 시각의 `detect` 처리 창, 창별 활성 인원수를 남기지 않았다.
2. `.agy/s13r-tier0-status.json`은 세션 종료 시점의 마지막 5초 창 원시 배열(`judge.lag_ms` 28개: `p95=218.2ms, max=273.9ms`, `detect.lag_ms` 30개: `p95=220.0ms, max=226.5ms`)과 인원수별 마지막 256개 표본 요약(`stage_timing.judge_tick`)만 담고 있으며, 45분 전체(540개 창)의 창별 `lag_ms_p95` 시계열은 보존되어 있지 않다. 또한 11-08 녹화 재생에서는 4~5명 구간이 전체 tick의 79.3%(12,956 / 16,346)를 차지하고 3명 구간은 3.1%(499 tick)였다.
3. 따라서 위 시뮬레이션은 `status.stage_timing.judge_tick`의 인원수별 실측 모수(`mean`, `p95`, `max`)를 재현하는 분포에서 5초 창 표본을 추출해 검증했으며, 실제 GPU에서 다인 동시 HMR 추론이 켜진 상태의 창별 `lag_ms_p95` 시계열은 다음 GPU 세션에서 재확인해야 한다.


## GPU 메모리 예산 (G0·11-06 FP8 PoC 반영, S15P31A101-525)

H100 80GB 기준. 비율은 GPU 전체 메모리 대비.

| 프로세스 | 상한 | 설정 |
|---|---|---|
| Tier 0 | 0.20 (16GB): 엔진 + 디코더 4개 + torch 선확보 8GB | `SIMAI_TIER0_GPU_RESERVE_MB`, 검사용 `SIMAI_GPU_TIER0_FRACTION` |
| 분석 워커 전체 예산 | 0.70 (vLLM + 아바타 + 워커 합산 상한) | `SIMAI_WORKER_GPU_MEMORY_FRACTION` |
| VLM(vLLM, FP8) | 0.30 (24GB: FP8 가중치 10.05 GiB + CUDA graph·활성화 여유 + KV 약 13 GiB), 동시 요청 4 | `SIMAI_WORKER_VLLM_GPU_MEMORY_UTILIZATION`, `SIMAI_VLLM_QUANTIZATION`(`fp8`), `SIMAI_WORKER_VLLM_MAX_NUM_SEQS` |
| 상시 아바타 | 0.10 (8GB, 실사용 4.0~5.1GB) | `SIMAI_AVATAR_GPU_MEMORY_FRACTION` |
| 분석 워커 프로세스(torch) | 0.30 (아바타 켬, 24GB) / 0.40 (아바타 끔, 32GB), **최소 보장 0.10 (8GB, MapAnything 피크 6.4GB OOM 방지)** | `SIMAI_WORKER_TORCH_MEMORY_FRACTION` (`MIN_WORKER_TORCH_FRACTION=0.10`) |

불변식(시작 때 검사, 틀리면 기동 전 거부): Tier 0 + 분석 워커 전체 ≤ 0.95 (PPE 켜면 + PPE 0.05 ≤ 0.95), vLLM + 아바타 + 워커 프로세스 ≤ 분석 워커 전체, 워커 프로세스 ≥ 0.10.
11-06 GPU PoC(H200, vLLM 0.21 + vllm-cosmos3, `--quantization fp8`)에서 가중치가 `16.79 → 10.05 GiB`, 첫 응답이 `1.73 → 1.57 s`(입력 약 2,900 토큰/요청, 한국어 판정·근거 품질 동등)로 확인되어 VLM 기본 비율을 `0.55`/`0.50` 에서 `0.30` 으로 낮추고 12차 GPU 세션의 MapAnything OOM(`worker_fraction=0.05` ≈ 4GB < 피크 6.4GB)을 막았다.

## 운영 순서와 한계

- Pod 에서 Tier 0 를 먼저 띄우고(표시 파일 `phase=running`·`reserve.state=OK` 확인) 분석 워커를 띄운다. 분석 워커가 먼저 메모리를 잡았으면
  선확보가 PARTIAL 이 되고 분석 워커는 새 작업을 받지 않는다(Tier 0 는 판정을 계속한다). 이때는 분석 워커를 내리고 Tier 0 를 다시 띄운다.
- 선확보는 torch 캐싱 할당기에 기댄다. Tier 0 코드에서 `torch.cuda.empty_cache()` 를 부르면 블록이 돌아가고 `reserve.state=LOST` 가 된다.
  TensorRT 실행 컨텍스트 메모리는 torch 캐시와 따로 잡힌다(엔진 로드 때 확보).
- 엔진 준비(`deploy/tier0/entrypoint.sh` 의 engine_cache) 동안은 아직 표시 파일이 없다. GPU 실험 큐는 진입점 명령줄(`/simai-tier0`)로 LIVE 를
  잡고, 분석 워커는 표시 파일이 없으면 모름으로 보고 받지 않는다(개발 모드 `SIMAI_WORKER_DEV_NO_LIVE=1` 을 관제 Pod 에 두지 않는다).
- GPU 실험 큐는 표시 파일이 있으면 LIVE 이고, 없을 때는 Linux 에서 Tier 0 명령줄(`-m services.tier0`, `/simai-tier0`)도 본다. 표시 파일 쓰기가 실패한
  Tier 0 도 명령줄로 잡힌다. 확인 명령(`check_command`)은 인자 목록만 받고, 0 = LIVE, `not_live_exit_codes`(기본 1) = LIVE 아님, 그 밖은 모름 → LIVE.
- 반납은 시도 횟수를 쓰지 않으므로 횟수로는 끝나지 않는다. 다음 대여 요청 전에 접수 문을 다시 보므로(닫혀 있으면 요청하지 않음) 바로 되풀이되지는 않지만,
  접수 문이 열고 닫히기를 반복하면 같은 작업이 여러 번 반납될 수 있다. EC2 쪽 반납 횟수 상한은 EC2 구현에서 정한다.

## 시험과 Pod 측정

노트북(GPU 없음): `tests/gpu/`(가짜 torch 선확보·접수 문 반례), `tests/services/worker/test_runner_tier0_priority.py`(가짜 EC2),
`tests/services/tier0/test_tier0_gpu_priority.py`(SIM Tier 0 + 더미 분석 워커 프로세스 강제 종료, 측정 도구),
`tests/experiments/gpu_queue/`(VRAM 대기 중 LIVE 시작, 확인 명령 실패).

Pod 측정은 `ai/experiments/gpu_queue/tier0_priority_probe.py`(`load` 더미 부하, `watch` 표시 파일 판정)로 -183 GPU 세션 큐에서 한다.
명령과 기대 결과는 MR 본문에 있다.

## S15P31A101-412: 사유별 접수 규칙과 차단 사유 계측

관련 명세는 3.2(GPU 공유·Tier 0 우선, v1.3 수정 48), 5.1(`queue_wait_ms` 150ms), 6.6(`capture_to_risk` p95 600ms), 13.2(포즈 0.5 s 없으면 캡슐),
20.5(안전 경로 격리)다. 변경 위치는 `ai/src/simai_ai/gpu/{admission,budget}.py`, `ai/src/simai_ai/hmr/stream.py`, `ai/services/avatar/transport.py`,
`deploy/compose.yaml`·`deploy/README.md`(기본값)이고, 시험은 `ai/tests/gpu/test_admission.py`, `ai/tests/hmr/test_avatar_loop.py`,
`ai/tests/services/tier0/test_tier0_gpu_priority.py`다. 계약 변경은 없다(표시 파일 형식 그대로).

### 10차 GPU 세션(2026-11-02) 근거

- 43분 동안 Tier 0 지연(촬영 → 판정) p95 301 ms. 5초 창 표시 파일 합계로 `over_budget` 4, `unknown` 4, `queue_wait_exceeded` 18 이었다
  (pod.jsonl 기준 600 ms 초과 4회).
- 그런데 아바타 루프는 거의 늘 막혔다(`gate_skipped` 6,165, 로그 `AdmissionGate 차단: Tier 0 지연 266ms ≤ 예산 600ms, 안정 대기 15/30s`).
  -408 조사로 VLM 빠른 경로(`vlm_rationale`)도 같은 게이트 때문에 대여가 13.8~134 s 늦은 건이 6건(일부 마감 초과)이었다. 게이트가 열려 있고
  워커가 놀면 작업 생성 → 시작은 49~74 ms 였다.
- 코드 원인: 이전 규칙은 큐 대기 초과 1건·모름·표본 없음·노후를 모두 즉시 닫고 30초(`resume_after_s`)를 처음부터 셌다. 단발 허용
  (`max_over_budget_samples=1`, -376)은 지연 초과에만 있었다. 추정(Argon): 게이트가 열리면 아바타가 같은 GPU 에서 YOLO26x-seg(1280, 4대)와
  SAM 3D Body 를 돌려 Tier 0 검출 대기 150 ms 를 몇 프레임 넘기고, 다음 표시 파일의 `queue_wait_exceeded ≥ 1` 로 다시 35초 닫히는 자가 진동.
  요약 수치만으로 6,165 를 다 설명했다고 보지 않고(Codex), 이번에 사유 계측을 넣어 11차에서 확인한다.

### 정한 수치 (설정값, 명세에 없는 값)

| 설정 | 기본 | 근거 |
|---|---|---|
| `max_queue_wait_exceeded` (`SIMAI_WORKER_MAX_QUEUE_WAIT_EXCEEDED`) | 2 | 큐 대기 초과로 버린 프레임은 그 카메라 갱신을 한 번 건너뛴 것이고(latest-frame-wins, 5.1) 판정 지연은 같은 창의 lag 표본 최댓값이 따로 잰다. 10차에서 p95 301 ms 인데도 큐 대기 초과 창이 18개였고 지연 초과 창은 4개뿐이었다. 한 창 3건 이상은 여러 카메라·연속 버림, 곧 정체의 시작으로 보고 멈춘다 |
| `resume_after_s` (`SIMAI_WORKER_RESUME_AFTER_S`) | 10 | 표시 파일 5초 주기의 통과 창 두 개(표본 약 10개)를 이어서 봐야 연다(깜빡임 방지의 최소). 뷰어는 포즈가 0.5 s 없으면 캡슐이므로 닫힌 시간이 곧 캡슐 시간이다. 30초에서는 사유 하나가 약 35초 캡슐을 만들었다. 10차 모양(43분, 지연 초과 창 4·큐 대기 초과 창 18)을 넣은 시험에서 열림 비율 97% 초과 |

- 이전 구현의 '안정 대기 중 지연 초과 단발 1표본 허용'(`max_over_budget_samples`)은 없앴다. 명세 3.2 의 기준(창 최댓값이 목표를 넘으면 멈춤)과
  어긋났고(Codex), 재개가 10초로 줄어 단발 초과 한 번의 비용이 약 35초 → 10~15초가 되어 허용할 이유가 없다. 진짜 지연 초과는 한 창·한 표본이라도 즉시 멈춘다.
- 큐 대기 초과 2건 이하 창은 '통과'로 본다. 안정 대기 중에 와도 시계를 지우지 않고, 열려 있으면 그대로 둔다. 지연 초과·모름과 함께 오면 그쪽 사유로 멈춘다.

### 아바타·분석 워커 정책: 하나로 둔다

결정: 같은 `AdmissionPolicy`(같은 환경 변수)를 분석 워커와 상시 아바타 루프에 모두 적용한다. 정책 객체를 나누지 않는다.

1. LIVE 중 배치 금지는 지연 규칙과 따로 걸린다. `Admission.allowed_types`·`blocks` 는 LIVE 면 `live_blocked_types`
   (`static_reconstruct`·`hmr_refine`·`hmr_appearance_update`)를 늘 빼고, 도는 배치 작업은 `_watch_live` 가 LIVE 시작 때 선점한다. 표시 파일이 있으면
   (지연과 무관하게) LIVE 다. 따라서 지연 규칙을 사유별로 나눠도 LIVE 중 배치 EXP 는 돌지 않는다.
2. VLM 빠른 경로(`vlm_rationale`, `VLM_FAST` 5000 ms)는 같은 게이트의 30초 대기로 막혔다. 우선순위 Tier 0 > VLM 빠른 경로 > 그 밖이 실제로 지켜지려면
   VLM 도 새 규칙(진짜 지연 초과·모름만 즉시 멈춤, 큐 대기 초과 소수는 넘김, 재개 10초)을 받아야 한다(Lead 지시 2026-11-02).
3. 지연 초과 때 분석 워커는 새 작업만 멈추고 도는 VLM 은 끝까지 둔다(그대로). 아바타는 추론 전마다 보므로 바로 멈춘다(그대로).
4. 정책을 나누면 같은 표시 파일을 두 해석으로 읽어 운영 판독(사유 로그)이 갈라진다. 다르게 둘 필요가 생기면 프로세스별 환경 변수로 준다
   (아바타도 `WorkerGpuSettings.from_env` 로 같은 값을 읽는다).

버린 대안: 아바타 전용 완화 정책(워커는 30초 유지) — VLM 대여 지연이 그대로 남는다. GPU 사용률 기반 게이트 — 표시 파일 계약·측정이 새로 필요하고
Tier 0 지연(정본 기준)과 직접 맞지 않는다.

### 계측 (판정 결과 불변)

- `Admission.code`: `ok`·`not_live`·`stabilizing`·`live_linger`·`marker_absent`·`marker_stale`·`marker_unreadable`·`not_running`·`reserve_not_ok`·
  `no_samples`·`lag_unknown`·`over_budget`·`queue_wait_exceeded`.
- `AdmissionGate.stats()`: `blocked`(받지 않은 check 수, 사유별), `resets`(열림·안정 대기 → 닫힘 전이 수, 사유별), `opened`, `last_reset_ms`·
  `last_reset_code`·`last_reset_reason`, `queue_wait_tolerated_windows`·`_frames`(넘긴 창·프레임, 같은 표시 파일은 한 창).
- 아바타 60초 요약(`아바타 입력 상태:`): `gate_skipped` 와 그 사유별 분해 `skip_reasons`, `pose_enqueued`(송신 대기열에 넣은 배치),
  `pose_sent`(송신기가 실제로 소켓에 쓴 배치 누계), `uploads`(GLB·PNG 업로드 성공), `assets_confirmed`, `gate_resets`, `last_reset`, `queue_wait_tolerated`.
  송신기 1분 집계(`아바타 1분 송신 집계:`)에 `pose_batches_total`·`asset_msgs_total` 을 더했다. 요약 줄 맨 앞 상태(예: `포즈 대기열 전달`)는
  그 순간 회차 하나의 상태이고 송신 횟수가 아니다.
- 상태 파일은 만들지 않았다(로그로 충분, 11차에서 부족하면 후속). 분석 워커 러너 로그는 그대로(받음/받지 않음 전이 때 사유 문자열).

### 남은 위험

- 자가 진동이 남을 수 있다: 아바타가 열린 뒤 분할·HMR 이 한 창에 큐 대기 초과 3건 이상을 만들면 여전히 10초 단위로 닫혔다 열린다.
  근본 대책(아바타가 Tier 0 검출을 재사용해 자체 분할을 줄임)은 후속 티켓이다. 트랙 ID 안정화는 -413, VLM 은 -408.
- 창당 2건 허용은 Tier 0 가 그 카메라 프레임을 버리는 것을 받아들이는 것이다. 판정 지연 상한(창 최댓값 600 ms)은 그대로 즉시 멈춤이라
  Tier 0 우선 원칙은 지킨다. 이 판단은 G0·11차 실측으로 다시 본다.
- 재개 10초는 열림·닫힘 전이를 늘릴 수 있다(`resets` 로 본다). 분석 워커는 전이가 잦으면 같은 작업 반납이 늘 수 있다(위 '운영 순서와 한계').

### 11차 GPU 세션에서 볼 지표

- 아바타 60초 요약: `skip_reasons` 분포(무엇이 막는지), `gate_resets` 사유·횟수, `last_reset`, `queue_wait_tolerated`, 분당 `pose_sent`, 캡슐로 돌아간 시간.
- 아바타를 켠 상태의 Tier 0 `capture_to_risk` p95·p99 가 600 ms 안인지(Tier 0 우선 검증).
- 표시 파일 창별 `queue_wait_exceeded` 분포(1·2 대 3 이상)와 아바타가 열린 직후 늘어나는지(자가 진동 확인).
- `vlm_rationale` 생성 → 시작 지연과 마감 초과 건수(-408 과 함께).


## S15P31A101-493 로비 배분과 재생성

기동기 기본은 전체 분석 예산 0.70 = vLLM 0.50 + 아바타 0.10 + 워커 0.10이다. H100 80GB에서 워커 상한 약 8GB는 12차 MapAnything peak alloc 6.4GB보다 약 1.6GB 크다. 이는 예약이 아니고 다른 CUDA 할당·입력에 따른 피크 증가를 보장하지 않는다. 아바타 실측 4.0~5.1GB여서 0.10을 유지한다. vLLM은 12차 38.3GB, 3차 가중치 16.79GiB + KV 23.99GiB였고 다운로드 31.5GB는 VRAM 크기가 아니다. 0.55에서 0.50으로 줄이면 KV와 동시성 여유가 줄며 32768 토큰·4 동시 요청의 기동과 처리량은 미검증이다. 80GB 미만은 경고하며 48GB급 미만은 기존대로 Tier 0만 기동한다. 명시 워커 상한은 env 파일 > 프로세스 환경 > 계산값, 합계 초과 거부.

명세 5.3·19.1은 LIVE 중 재생성 예외를 실측 뒤 결정하도록 남겨 두었다. 작업이 없어도 상시 안전 감시이므로 작업 종료를 LIVE 종료로 취급하지 않는다. 기본적으로 LIVE 재생성을 차단하고 로그에 접수 제외 이유를 남긴다. 관제 정상 종료 뒤 `simai-bootstrap after-live`로 워커를 띄워 처리한다. 서버·화면의 대기 이유 및 관제 종료 안내는 후속이며 대기 요청의 시간 초과를 피하려면 종료 후 요청해야 한다.
