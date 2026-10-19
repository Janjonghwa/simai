# Tier 0 GPU 우선 보호 (S15P31A101-60)

Tier 0 와 분석 워커가 GPU Pod 하나를 나눠 쓸 때 Tier 0 를 먼저 지키는 구조다. 근거: 명세 3.2(GPU 공유 규칙·메모리 예산표),
6.6(지연 예산 capture_to_risk p95 600ms), 20.5(안전 경로 격리), 15.1 G0(VLM·분석 워커 동시 실행 시 capture_to_risk·메모리 부족),
부록 D D-G0-08(분석 워커 강제 종료 시 판정 지속 100%).

| 규칙(명세 3.2) | 구현 |
|---|---|
| Tier 0 가 GPU 메모리를 먼저 확보 | `simai_ai.gpu.reserve.GpuReservation`: Tier 0 기동 첫 단계에서 torch CUDA 캐시에 `gpu.reserve_mb`(기본 8192, `SIMAI_TIER0_GPU_RESERVE_MB`)를 잡아 둔다 |
| 분석 워커는 상한 안에서 | 분석 워커 시작 때 `torch.cuda.set_per_process_memory_fraction`(기본 0.70), VLM 은 vLLM `gpu_memory_utilization`(기본 0.55)·`max_num_seqs`(4) — `simai_ai.gpu.budget` |
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
2. 확인·읽기 실패, `max_marker_age_s`(15초, 갱신 주기의 3배)보다 오래됨, `gpu_priority` 없음·형식 틀림 → 모름 → 받지 않는다(LIVE 로 본다).
3. `phase != running`, 선확보가 OK·SKIPPED·DISABLED 가 아님 → 받지 않는다.
4. 표본 0, unknown ≥ 1, `lag_ms_max` > 예산(600ms), `queue_wait_exceeded` ≥ 1 → 받지 않는다(창 최댓값: p95 보다 엄격).
5. 위를 모두 통과한 상태가 `resume_after_s`(30초) 이어져야 받는다(깜빡임 방지).

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

## GPU 메모리 예산 (초기값, G0 실측으로 고친다)

H100 80GB 기준. 비율은 GPU 전체 메모리 대비.

| 프로세스 | 상한 | 설정 |
|---|---|---|
| Tier 0 | 0.20 (16GB): 엔진 + 디코더 4개 + torch 선확보 8GB | `SIMAI_TIER0_GPU_RESERVE_MB`, 검사용 `SIMAI_GPU_TIER0_FRACTION` |
| 분석 워커 전체(torch) | 0.70 | `SIMAI_WORKER_GPU_MEMORY_FRACTION` |
| VLM(vLLM) | 0.55, 동시 요청 4 | `SIMAI_WORKER_VLLM_GPU_MEMORY_UTILIZATION`, `SIMAI_WORKER_VLLM_MAX_NUM_SEQS` |

불변식(시작 때 검사, 틀리면 워커가 시작하지 않음): Tier 0 + 분석 워커 ≤ 0.95, vLLM ≤ 분석 워커.

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
