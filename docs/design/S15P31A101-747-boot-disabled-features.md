# S15P31A101-747 부팅 보고에 「꺼진 기능」 목록, 설정 무효 WARNING, VLM 웜업

관련 명세: 3.2(Tier 0 우선·분석 워커·VLM), 3.12(GPU 실행 사이트), 5.2(Tier 0), 12.2(보고·토큰). 관련 티켓: -700(자동 부팅), -699(세션 등록·보고), -710(GPU 캐시), -718(ppe 자동 기동), -740(호스트 CPU 보고, 같은 계약).

## 문제(18차 GPU 세션 감사)

1. Re-ID 묶음 S3 캐시 miss 가 `simai_gpu_boot.py` 에서 `log()` 만 하고 보고에 오르지 않아 3세션 동안 아무도 몰랐다. Tier 0 도 「Re-ID 비활성화」를 INFO 로그로만 냈다.
2. `tier0-live.json` 의 `live_calibration.enabled: true` 는 `SIMAI_TIER0_HUMAN_CALIBRATION=0`(기본) 때문에 효과가 없는데 아무 경고가 없었다.
3. 첫 `vlm_rationale` 이 추론 중 Triton JIT 3건으로 TIMED_OUT, 둘째 2,164 ms, 이후 약 1,100 ms. 워커·bootstrap 어디에도 웜업이 없었다.

## 설계

### 1. 꺼진 기능 목록

- **계약**: `gpu_session_report` 1.2.0(컨트랙트 v7.62.0, 마이너)에 선택 `disabled_features`(문구 배열, 최대 20개·항목 1~200자). 칸 없음 = 아직 모름, 빈 배열 = 확인했고 꺼진 것 없음(지어내지 않는다). `stage_detail` 에 더하는 대신 칸을 따로 둔 이유: 상세는 500자 상한에서 잘리고(캐시 403 등 다른 note 와 섞임) GPU 패널이 `stage_detail` 을 보이지 않으며, 경고를 구조로 받아야 패널이 줄마다 보일 수 있다. 1.1.0 은 -740(`host_cpu`).
- **부팅 보고기(`deploy/scripts/simai_gpu_boot.py`)**: `Reporter.disabled`(키 → 문구, 같은 기능은 덮어쓴다). 근거 있는 것만 싣는다.
  - 캐시 결과(`cache_findings`): Re-ID 묶음이 없으면 「Re-ID 꺼짐 — 묶음 없음(seed-reid 필요)」(받기 결과가 error 면 그 사실도), TensorRT 엔진·vLLM 컴파일 캐시 miss·오류(기능이 꺼진 것은 아니나 부팅 시간·첫 VLM 요청에 영향이라 함께 싣는다).
  - 기동 서비스(`service_findings`): `--services` 에 ppe·avatar·worker·vllm 이 없거나 VRAM 부족으로 vLLM 을 못 띄움.
  - Tier 0 표시 파일 `state/tier0.live` 의 `status.disabled_features`·`status.process_split.disabled`(`tier0_findings`). 표시 파일에 `status` 칸이 아직 없으면 아무것도 싣지 않는다(옛 Tier 0 포함).
  - VLM 웜업 실패, vLLM 미준비(시한 초과).
  - 캐시 받기가 끝난 시점(Tier 0 전)부터 칸을 싣고(`set_disabled("start")` 로 연다), 서비스·Tier 0·웜업 결과는 이후 같은 목록에 더한다.
- **Tier 0**: `Tier0Settings.config_overrides`(설정 파일이 명시한 값을 환경 변수가 뒤집었을 때의 문구)와 Re-ID 꺼짐 이유를 `status.disabled_features` 로 낸다. 설정 파일이 말하지 않은 기본값은 「덮은 것」으로 세지 않는다(`live_calibration.enabled` 기본 true 라 기본 설정에서 매번 경고하면 소음). 기동 때 `설정 무효: …` WARNING. Re-ID 비활성화 로그를 INFO → WARNING.
- **EC2**: Flyway V81 `gpu_session_event.disabled_features jsonb`(배열만). 진행 사건 행에 그대로 두고(추가만) 값이 있는 가장 최근 행이 지금 값. `GET /api/gpu/sessions/current` 의 `session.disabled_features`(계약 밖 관리자 응답, 없으면 null). 게이트웨이용 `gpu_session_status` 는 그대로.
- **관리자 GPU 패널**: 준비 완료(READY)인 열린 세션에서 문구마다 경고 줄(`GpuInfoLines`). 조작 없음.

### 2. 설정 무효 WARNING

`load_settings` 가 `live_calibration.enabled=true`(설정 파일 명시)인데 `SIMAI_TIER0_HUMAN_CALIBRATION!=1`, `drift_watch_enabled=true`(명시)인데 같은 경우, `offload_side_work`·`process_split` 을 환경 변수가 설정 파일과 다른 값으로 뒤집은 경우를 `config_overrides` 로 모은다. 환경 변수 값을 기본 동작으로 바꾸지는 않는다(결정 사항 아님, 경고만).

### 3. VLM 웜업

supervise 루프에서 vLLM `/health` 200 을 처음 보고 READY 로 넘어가기 직전에 `warmup_vlm`: `POST 127.0.0.1:$SIMAI_VLLM_PORT/v1/chat/completions`, user 메시지에 text 파트 → `image_url` data URL(작은 합성 PNG 336x192, 표준 라이브러리로 생성, 색 띠라 약 1 KB), `max_tokens` 16, `repetition_penalty` 1.05(워커 VLM 클라이언트 `vlm/client.py` 와 같은 경로·형식. repetition_penalty 가 샘플러 JIT 를 건드린다). 상한 `SIMAI_BOOT_VLM_WARMUP_TIMEOUT_S`(기본 120 s). 걸린 시간은 로그와 보고 설명에 남기고 실패해도 부팅을 계속한다. 웜업 중 단계는 VLLM(READY 는 웜업 뒤).

## 명세에 없어 정한 수치와 근거

- 웜업 timeout 120 s: 18차 첫 요청이 JIT 3건으로 워커 timeout(기본 30 s)을 넘겼다. 그보다 충분히 길게, 부팅을 끝없이 막지는 않게. 실측 전 초기값이다.
- 이미지 336x192: 실제 요청(가로 1280 px)과 같은 형식의 경로를 타되 비전 인코더 입력을 작게. 해상도별 JIT 가 다를 수 있어 웜업이 모든 모양의 JIT 를 없애지는 못한다(잔여 위험).
- `disabled_features` 최대 20개·200자: 보고 detail(500자)과 같은 부류의 닫힌 payload 상한.

## 버린 대안

- `stage_detail`(note)에만 싣기: 500자 상한에서 잘리고 패널이 읽지 않는다.
- Tier 0 가 환경 변수 충돌을 오류로 끝내기: 시연 중 재기동을 부르므로 경고만.
- 웜업을 워커에서 하기: 워커는 vLLM 보다 먼저 뜨고 `SIMAI_WORKER_VLM_GATE` 로 접수만 막는다. READY 를 정하는 곳이 부팅 보고기라 거기서 한다.

## 잔여 위험·넘긴 일

- 웜업이 다른 요청 모양(여러 이미지·긴 프롬프트)의 JIT 를 못 없앤다. 다음 GPU 세션에서 웜업 뒤 첫 실제 `vlm_rationale` 지연으로 확인한다(`부팅 직후 확인 체크리스트`, deploy/README).
- 손 경로(`start-lobby`)에는 웜업·꺼진 기능 보고가 없다(자동 부팅 전용).
- Tier 0 가 READY 뒤 나중에 꺼지는 기능(예: 런타임 Re-ID 오류)은 한 번 읽은 뒤 다시 보지 않는다.
- 판 번호 v7.62.0·V81 은 -740(v7.61.0·V80)이 먼저 머지된다고 가정했다. 순서가 바뀌면 REPORT_SCHEMA_VERSION·README 줄·마이그레이션 번호를 다시 맞춘다.

## 코드·시험

- 코드: `deploy/scripts/simai_gpu_boot.py`(`cache_findings`·`service_findings`·`tier0_findings`·`dummy_png`·`warmup_vlm`·`Reporter.set_disabled`), `ai/services/tier0/{settings,service}.py`, `ai/src/simai_ai/reid/collector.py`, `contracts/messages/gpu_session_report.schema.json`, `server/.../GpuSessionService.java`·`V81`, `front/apps/admin/src/features/gpu/`.
- 시험: `ai/tests/deploy/test_simai_gpu_boot.py`, `ai/tests/services/tier0/test_human_calibration_settings.py`, `GpuSessionIntegrationTest`, `gpuStatus.test.ts`.
