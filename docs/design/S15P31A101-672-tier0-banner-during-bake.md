# S15P31A101-672 정밀 맵 굽기 중 'Tier 0 응답 없음' 배너 일시 점멸 원인 및 해결 설계

## 1. 관련 명세 절과 규칙

- **기획서 명세**:
  - `docs/spec/03-architecture.md` (3.2 GPU 공유·우선순위·이중 감시, 3.10 현장 경보 재동기화, 3.12 GPU 실행 사이트 사양)
  - `docs/spec/05-processing.md` (5.2 Tier 0 실시간 판정 루프, 5.3 현장 세팅 및 정밀 맵 굽기)
  - `docs/spec/06-operations.md` (6.1 시간 동기화·지연 판정, 6.2 System Health `NOMINAL`/`DEGRADED`/`UNSAFE_UNKNOWN`, 6.5 장애 격리)
  - `docs/spec/12-interfaces.md` (12.2 I0 제어 채널, 12.3 I1 `health`·`viewer_health` 1Hz 계약)
- **지켜야 할 불변 규칙 (`AGENTS.md`)**:
  - **규칙 1 (Tier 0 정본)**·**규칙 2 (모르면 비운다)**·**명세 3.2 이중 감시**: Tier 0 가 실제로 멈추거나 연결이 끊겼을 때는 절대 숨기지 않고 `UNSAFE_UNKNOWN` / `CONTROL_HALTED` 로 정직하게 보고한다. 고치는 대상은 **정밀 맵 굽기(`BAKING_TEXTURE`) 중 CPU 스레드 과다 생성과 Tier 0 내부 `judge_lock` 경합 때문에 실제로 살아 있는 Tier 0 의 1Hz `health` 송신이 밀려 EC2 가 3초 타임아웃으로 오판하는 문제**다.

---

## 2. 14차 GPU 세션(`2026-11-12`) 원인 확정 (시각·로그·줄 번호 근거)

### 2.1 14차 세션 타임라인 및 Pod 로그·상태 파일 근거

1. **1차 기동 (`lobby-s14-0446`) 및 현장 세팅 정밀 맵 굽기 구간**:
   - `04:46:58 UTC` (`13:46:58 KST`): Pod 기동 (`bootstrap/20261112T044658Z-844.json`), 초기 보정 `cal-lobby-init-f0ab1c6b-jba2-2f6cc097` (`04:55:52.807Z`).
   - 현장 세팅(`setup_session_id = e719bbc1-b272-4cfc-a246-3f7a6cc39fb5`)이 배경 묶음(`04:58:17.096Z`)으로 새 보정 `cal-lobby-init-c85f2a97-jba2-59e38a25` 를 생성하고, 빠른 미리보기 뒤 약 5분간 정밀 맵 굽기(`site_setup` `BAKING_TEXTURE`: `texture_mesh` TSDF 융합·단순화·`texrecon` + `mesh_clean` + `final_map_bake`)를 수행.
   - `05:32:08.086Z` (`14:32:08.086 KST`): 세팅 완료 및 `cfg-66` 구성 묶음 발행.
   - `05:32:09.056Z` (`14:32:09.056 KST`): Tier 0 가 새 세팅 보정 채택 (`lobby-s14-0446/state/calibrations/active.json` 및 `0962a377...report.json`).
2. **2차 기동 (`lobby-s14-0549`, `.agy/s14/lobby-s14.log`, 총 28,545줄)**:
   - `start-lobby.sh` 가 `> /srv/simai/runs/lobby-s14.log` 덮어쓰기 리다이렉트로 `05:49:29 UTC` (`14:49:29 KST`)에 재기동.
   - `.agy/s14/lobby-s14.log:4` (`05:49:31,148`):
     `[bootstrap] CPU 격리: affinity 224개, cgroup 상한 23.8 vCPU (/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_quota_us) → vLLM·워커 212-223 (12개), Tier 0 묶지 않음, Tier 0 수치 연산 스레드 1개`
   - `.agy/s14/lobby-s14.log:33` (`05:49:33,458`):
     `I1 연결됨 (1 번째)` — 이후 `06:22:29` 종료 시까지 `I1 연결 끊김` 0건(WebSocket TCP 연결 자체는 끊기지 않음).
   - `.agy/s14/lobby-s14.log:12417` (`06:00:28,570`):
     `Tier 0 우선 접수 문: 받지 않음 (Tier 0 지연 2347ms > 예산 600ms)` — 부하 중 Tier 0 지연이 `2347 ms` 까지 치솟은 기록 확인.

### 2.2 EC2 및 프런트엔드의 `'Tier 0 응답 없음'` 판정 경로

1. **EC2 (`server/src/main/java/com/ssafy/simai/domain/health/ViewerHealthService.java`)**:
   - `ViewerHealthService.java:71`: `simai.health.link-timeout-ms:3000` (**3.0초**, `HealthPublisher.java` 가 1초 주기로 `evaluate()` 호출).
   - `ViewerHealthService.java:94-115`: Tier 0 로부터 `health` 또는 `world_state` 를 받을 때 `t_latest_global` 이 단조 증가하면 `progress` 단조 시각(`nanos`)을 갱신한다.
   - `ViewerHealthService.java:208-225`:
     - `tier0Up`: 마지막 Tier 0 `health` 수신 후 경과가 `3,000 ms` 이내인지 (`nowNanos - tier0.receivedNanos() <= linkTimeoutNanos`).
     - `processing`: `t_latest_global` 마지막 단조 증가 후 경과가 `3,000 ms` 이내인지 (`nowNanos - p.nanos() <= linkTimeoutNanos`).
     - `remote_state`: `tier0Up && processing && laptopOk` 이면 `"NORMAL"`. 그렇지 않을 때 `!tier0Up && laptopSaysTier0Ok` 이면 `"REMOTE_LINK_LOST"`, 그 외(`tier0Up && !processing` 또는 노트북 하트비트 갱신 전 `!tier0Up`)에는 `"CONTROL_HALTED"` 로 전이하며 `effectiveSystemHealth` 를 `"UNSAFE_UNKNOWN"` 으로 강제한다.
2. **프런트엔드 (`front/apps/admin`)**:
   - 14차 세션 이미지(`unified-7558a7a333d7`) 당시 `FailureBand.tsx`(이후 `-647` 커밋 `26f12fc3` 에서 상단 `TopBar` 및 CCTV 셀 배지로 통합·제거됨)는 `remote_state === 'CONTROL_HALTED'` 이고 `laptop.link === 'CONNECTED'` 일 때 `'관제 정지 · 판정 서버(Tier 0) 응답 없음'` 배너를 화면 상단에 표시했다.
   - 현재 프런트(`front/apps/admin/src/store/realtime.store.ts:153, 156`, `front/apps/admin/src/features/layout/TopBar.tsx:74`, `front/apps/admin/src/features/layout/status.ts:45-68`)는 `HEALTH_TIMEOUT_MS = 3000`(3.0초), `WORLD_STALE_MS = 1500`(1.5초) 기준으로 `viewer_health` 를 소비한다.

### 2.3 왜 정밀 맵 굽기(`BAKING_TEXTURE`) 동안 Tier 0 `health`·진행이 3초 넘게 밀렸는가?

원인은 **굽기 자식 프로세스의 CPU 스레드 폭발(원인 A)** 과 **Tier 0 `i1-publisher` 의 `judge_lock` 경합 블로킹(원인 B)** 이 겹친 것이다.

#### 원인 A — 정밀 맵 굽기 자식 프로세스의 224 스레드 폭발 및 컨테이너 CFS 쿼터 잠식
1. `S15P31A101-566` 에 의해 현장 세팅(`site_setup`)은 관제 전 초기 단계이므로 Tier 0 지연 접수 문(`admission.py`)의 예외로 실행된다.
2. `ai/src/simai_ai/scene/reconstruct_tasks.py:654` (`run_final_map_pipeline`) 및 `reconstruct_tasks.py:835` (`bake`)에서 `num_threads` 기본값은 `0` 이다.
3. `ai/src/simai_ai/scene/texture_mesh.py:76-87` 의 `resolve_num_threads(0)` 는 프로세스 CPU 친화성(`os.sched_getaffinity(0)`)이나 컨테이너 cgroup 상한(`23.8 vCPU`)을 보지 않고 호스트 전체 논리 코어 수인 `os.cpu_count()`(**224개**)를 반환했다.
4. 그 결과 `configure_cpu_threads(0)` 가 `os.environ["OMP_NUM_THREADS"] = "224"` 로 덮어쓰고, `Texrecon.run` (`texture_mesh.py:477`)이 `texrecon --num_threads=224` 로 실행되어 Open3D TSDF 통합·메시 단순화·Embree 레이캐스팅과 `texrecon` 이 **224개 OpenMP 스레드**를 띄웠다.
5. 또한 `reconstruct_tasks.py:1092` 의 `main()` 은 별도 자식 프로세스(`python -m simai_ai.scene.reconstruct_tasks`) 진입점인데 `-662` 에서 `limit_cv2_threads(os.environ)` 호출이 빠져 있어 `cv2` 역시 224개 스레드를 만들었고, `tm.main(args)` 가 높여 놓은 `OMP_NUM_THREADS=224` 가 같은 프로세스 안의 후속 `mesh_clean`·`final_map_bake` 단계까지 그대로 남았다.
6. 14차 세션(`lobby-s14.log:4`)에서는 Tier 0 코어 고정조차 꺼져 있었고(`Tier 0 묶지 않음`), `-662` 에서 Tier 0(`0-11`)와 사이드카(`212-223`) 코어를 분리한 뒤에도 모든 프로세스는 컨테이너의 `23.8 vCPU` Linux CFS 대역폭 쿼터(`cpu.cfs_quota_us`)를 공유한다. 굽기 프로세스가 224개 스레드로 사이드카 12코어를 5분 내내 100% 포화시키면 컨테이너 전체 CFS 예산이 고갈되어 커널 CFS 스로틀링이 Tier 0 스레드까지 멈추게 만든다.

#### 원인 B — `i1-publisher` 의 1Hz `health` 및 100Hz 보정 이력 조회가 `judge_lock` 에 블로킹됨
1. `ai/services/tier0/service.py:530, 534` 에서 `HealthReporter` 와 `SiteAlertResync` 에 모두 `lock=self.judge_lock` 이 주입되어 있었다.
2. `tier0-judge` 스레드는 매 판정 틱 전체(`ai/services/tier0/judge.py:221`: `_begin_calibration_tick` → `config_sync.begin_tick` → `perception.step` → `_judge`) 동안 `self.judge_lock` 을 쥐고 있다.
3. 한편 `i1-publisher` 스레드(`ai/services/tier0/i1_publisher.py:202`)는 **10ms(100Hz)** 마다 `tick()` 을 돌면서:
   - 먼저 `_drain_sources()` 에서 `self._calibration_messages()` (`service.py:1376`)를 호출해 **매 10ms 마다 `with self.judge_lock:` 을 잡고 `self.calibration.transitions` 전체를 `json.loads(json.dumps(...))` 로 깊은 복사**했다.
   - 이어서 1초마다 `_emit_health()` 에서 `self.health.payload()` (`health.py:219`), `self.re_setup_field()` (`service.py:985`), `self._site_fields()` → `self.judge.active_states()` (`judge.py:194`)를 호출하며 다시 `self.judge_lock` 을 중복 획득했다.
   - 그뿐 아니라 `tier0-housekeeping` 스레드도 매 1초마다 `_sample_load()` → `self.health.evaluate()` (`service.py:1740`)와 `clips.poll()` (`service.py:1597`), 매 5초마다 `_write_marker()` (`service.py:1625`)에서 `self.judge_lock` 을 잡았다.
4. 따라서 정밀 맵 굽기 중 CPU CFS 스로틀링이나 다중 인원 추적 부하로 `tier0-judge` 틱이 수백 ms~2.3초(`lobby-s14.log:12417`) 지연되면, `i1-publisher` 가 `judge_lock` 대기열에 갇혀 1Hz `health` 송신 자체가 3.0초(`link-timeout-ms=3000`) 이상 지연되고, EC2 `ViewerHealthService` 가 `tier0Up = false` / `CONTROL_HALTED` 로 판정해 관제 화면에 `'Tier 0 응답 없음'` 배너가 잠깐씩 떴다.

---

## 3. 해결 설계 및 코드·시험 위치

| 구분 | 파일 및 위치 | 변경 내용 |
|---|---|---|
| **원인 A 해결 (굽기 CPU 스레드 상한)** | `ai/src/simai_ai/scene/texture_mesh.py:76-132` | `DEFAULT_BAKE_NUM_THREADS = 4` 도입. `num_threads <= 0` 일 때 `os.cpu_count()`(224) 대신 `SIMAI_BAKE_NUM_THREADS` 환경변수(양수면 그대로) 또는 `min(_usable_cpu_count(), DEFAULT_BAKE_NUM_THREADS)`(프로세스 affinity 와 cgroup v1/v2 CPU 쿼터 반영, 최대 4스레드)를 반환하도록 수정. |
| **원인 A 해결 (자식 프로세스 스레드 제한)** | `ai/src/simai_ai/scene/reconstruct_process.py:16-24`<br>`ai/src/simai_ai/scene/reconstruct_tasks.py:686-695, 855-864, 1106-1110` | `ProcessStages` 기본 환경변수에 `OMP_NUM_THREADS`, `MKL_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, `NUMEXPR_NUM_THREADS`, `SIMAI_CV2_THREADS` 기본값 `"1"` 설정. `reconstruct_tasks.main()` 진입 시 `limit_cv2_threads(os.environ)` 호출. `tm.main(args)` 실행 전후로 `OMP_NUM_THREADS` 를 복원해 후속 `mesh_clean`·`final_map_bake` 단계에 높은 OpenMP 스레드 수가 전파되지 않게 차단. |
| **원인 B 해결 (`i1-publisher` 의 `judge_lock` 비블로킹화)** | `ai/src/simai_ai/calibration/runtime.py:324-334`<br>`ai/services/tier0/judge.py:194-200`<br>`ai/services/tier0/health.py:197-200`<br>`ai/services/tier0/service.py:517-536, 985-994, 1379-1392` | 1) `RuntimeCalibrationStore.transitions_since(cursor)`: `_head` 의 copy-on-write 불변성을 이용해 새 전환 이력이 없을 때(99.99% 틱) `judge_lock` 획득과 JSON 깊은 복사를 생략.<br>2) `JudgmentLoop.active_states_snapshot()`: 매 틱·ACK·종료·상향 끝마다 원자적으로 교체되는 불변 `MappingProxyType`(`_view_states`)을 `judge_lock` 없이 읽도록 제공.<br>3) `HealthReporter`: `lock=self.judge_lock` 대신 자체 `RLock` 을 사용(`Perception.camera_clocks` 는 이미 자체 `Perception._lock`, `ConfigSync` 는 자체 `ConfigSync._lock` 사용)하고 `re_setup_field()` 도 불변 항목 스냅샷을 읽어 `i1-publisher` 의 1Hz `health` 발행이 긴 `judge.tick()` 에 블로킹되지 않게 분리. |
| **단위 시험** | `ai/tests/services/tier0/test_resource_isolation.py:119-202`<br>`ai/tests/scene/test_reconstruct_tasks.py:664-720` | 1) 다른 스레드가 `judge_lock` 을 잡고 있는 동안에도 `health.payload()`, `_site_fields()`, `re_setup_field()`, `_calibration_messages()` 가 `< 50ms` 안에 즉시 반환되고 계약 스키마(`health` 2.3.0)를 만족하며, 실제 진행(`t_latest_global`)이 `lag_unsafe_ms` 넘게 멈추면 `UNSAFE_UNKNOWN`(`LAG_UNSAFE`)을 숨기지 않고 보고하는지 검증.<br>2) 224코어·24 vCPU cgroup 환경에서 `resolve_num_threads(0)` 가 `4` 로 제한되고 `OMP_NUM_THREADS` 가 복원되는지 검증. |

---

## 4. 명세에 없어 정한 수치와 근거

- **`DEFAULT_BAKE_NUM_THREADS = 4` (`texture_mesh.py:76`)**:
  - **근거**: RunPod 최소 사양 컨테이너(`23.8 vCPU` cgroup CFS 상한, `simai_bootstrap.py` 기준 Tier 0 에 12코어·사이드카 전체에 12코어 배정)에서 Tier 0(검출·추적·I0/I1), Avatar 워커, vLLM 이 상시 실행된다. 정밀 맵 굽기의 `texrecon` 시점 선택·그래프컷·Poisson 솔버는 고정 카메라 4대(`cam_A`~`cam_D`) 입력을 다루므로 4스레드로 충분한 병렬성을 얻으면서, 사이드카 CPU 코어(12개)의 1/3 이하·컨테이너 전체 CFS 예산(23.8 vCPU)의 17% 이하만 써서 Tier 0 의 CFS 스케줄링 지연을 막는다. 오프라인 실험이나 단독 벤치마크에서는 기존처럼 `--num-threads N` 또는 `SIMAI_BAKE_NUM_THREADS=N` 으로 덮어쓸 수 있다.

---

## 5. 버린 대안

1. **EC2 `simai.health.link-timeout-ms`(3000ms) 또는 프런트 `HEALTH_TIMEOUT_MS`(3000ms)를 10~15초로 늘리는 안**:
   - **기각 사유**: 명세 3.2 이중 감시 및 깨면 안 되는 규칙 2("모르면 비운다")에 위배된다. 타임아웃을 늘리면 실제 Tier 0 프로세스 정지나 링크 단절 때도 10초 넘게 정상처럼 보이게 되어 위험 판정 공백을 숨기게 된다.
2. **현장 세팅(`BAKING_TEXTURE`) 중에는 프런트엔드에서 `'Tier 0 응답 없음'` 배너를 숨기는 안**:
   - **기각 사유**: 정밀 맵 굽기 중이라도 Tier 0 가 실제로 죽거나 멈추면 운영자에게 즉시 알려야 한다. 배너를 숨기면 진짜 장애를 가리게 된다.

---

## 6. 진짜 끊김·정체를 숨기지 않는 이유 (명세 3.2 이중 감시 검증)

- `i1-publisher` 가 `judge_lock` 에서 분리되어 1Hz `health` 를 제때 보내더라도:
  1. `tier0-judge` 판정 루프가 실제로 멈추면 `Perception.ingest()` 가 더 이상 호출되지 않아 `t_latest_global` 이 증가하지 않고, `ProgressWatchdog.age_ms()` 가 `lag_unsafe_ms`(2,000 ms)를 넘겨 Tier 0 스스로 `health.system_health = "UNSAFE_UNKNOWN"` (`HealthReason.LAG_UNSAFE`)을 발행한다.
  2. 또한 EC2 `ViewerHealthService` 는 `health` 수신 시각(`tier0Up`)뿐 아니라 **`t_latest_global` 의 단조 증가 시각(`processing`, 3,000 ms)** 을 독립적으로 검사하므로(`ViewerHealthService.java:209`), 판정 루프가 3초 이상 새 프레임을 처리하지 못하면 `health` 패킷이 매초 도착하더라도 `processing = false` → `remote_state = "CONTROL_HALTED"` (`UNSAFE_UNKNOWN`)로 정확히 판정한다.

---

## 7. 잔여 위험 및 넘긴 일

- **잔여 위험**: `perception.step()` 이 새 프레임을 `ingest()` 한 직후 같은 틱의 3D 삼각측량·접지 계산에 수백 ms 가 걸리는 동안 `i1-publisher` 가 `health` 를 읽으면, `camera_clocks()` 에는 방금 `ingest()` 된 프레임 시각이 반영된다. 만약 해당 틱에서 판정 스레드가 영구 정지하더라도 다음 `ingest()` 가 불리지 않으므로 `t_latest_global` 은 그 시점 이후 더 이상 증가하지 않아 `ProgressWatchdog` 와 EC2 `processing` 감시가 정상 작동한다.
- **넘긴 일**: 다음 GPU Pod 세션에서 현장 세팅(`site_setup`) 정밀 맵 굽기 구간 동안 `stage_timing.loops.i1_wake_lag` 및 EC2 `viewer_health` 전이 로그에 `CONTROL_HALTED` 가 0건인지 실측 확인한다.

## Lead 조정 (2026-11-13)
- 굽기 기본 스레드를 고정 4 → **가용 코어(affinity·cgroup 반영)의 절반**으로 바꿨다(`ai/src/simai_ai/scene/cpu_budget.py` `default_bake_threads`). 24 vCPU Pod 면 12. 근거: 고정 4 는 14차(약 5분) 굽기를 몇 배 늘릴 수 있고(명세 5.3 은 굽기에 CPU 를 최대한 쓴다), 배너의 직접 원인은 원인 B(판정 잠금 경합)라 그 수정으로 막는다. 나머지 절반은 Tier 0 몫. `SIMAI_BAKE_NUM_THREADS` 로 덮어쓸 수 있다.
- 재구성 자식 프로세스 기본 OMP/MKL/OpenBLAS/NumExpr 도 1 → 같은 절반(OpenCV 는 1, -662 와 같음). 1 이면 미리보기·재구성 CPU 단계(torch·numpy)가 느려진다.
- GPU 세션 확인: 굽기 시간(14차 약 5분 대비)과 굽기 중 배너 0.
