# S15P31A101-756 꺼진 기능 안내를 쉬운 말로, READY 뒤에도 갱신

관련 명세: 3.2(Tier 0 우선·분석 워커), 3.12(GPU 실행 사이트), 12.2(보고). 앞 티켓 -747(꺼진 기능 목록, 같은 계약 1.2.0), -740(호스트 CPU).
관련 규칙: AGENTS 2(모르면 비운다: 표시 파일에 status 가 없으면 아무것도 지어내지 않는다), 6(추가만: 진행 사건 행), 7(관측과 추정 구분).

## 문제(19차 GPU 세션)

1. 정보창·패널에 「Tier 0: Re-ID 꺼짐: FileNotFoundError: COPE 외부 저장소 경로를 찾을 수 없습니다: /srv/simai/reid/cope」 같은 원문이 그대로 나오고 영향·할 일이 없다.
2. READY 때 한 번 읽은 값을 계속 보고해서 세션 중 `seed-reid`·`restart-tier0` 로 Re-ID 가 켜진 뒤(stage_timing `reid.state=active`)에도 「꺼짐」이 남았다.
3. 사람 관절 보정 기본 꺼짐(`SIMAI_TIER0_HUMAN_CALIBRATION=0`, 의도된 설정) 때문에 설정 파일의 `live_calibration.enabled=true` 가 무효라는 문구가 경고로 매 세션 뜬다.

## 설계

- **계약**: `gpu_session_report` 1.3.0(컨트랙트 v7.64.0, 마이너). 선택 `disabled_notices`(`{key, level: warning|info, text(≤200), detail?(≤300)}`, 최대 20, 닫힌 객체). `disabled_features`(1.2.0)는 warning 의 `text` 로 계속 싣는다(옛 수신 측 호환, 칸 의미 불변). `disabled_features` 항목을 문자열|객체 합집합으로 넓히는 대신 새 칸을 둔 이유: 옛 화면(zod 문자열 배열)이 새 값에서 깨지지 않게 하려고.
- **문구표(`deploy/scripts/simai_gpu_boot.py`)**: 형식은 「무엇이 꺼졌나 — 사람에게 어떤 영향 — 할 일」. 원문은 `detail` 로만.

| 종류 | 수준 | 문구 요지 |
|---|---|---|
| Re-ID, 묶음이 서버에 없음 | warning | 가렸다 나타난 사람이 새 번호를 받을 수 있어요. 관리자: seed-reid 로 묶음 등록 |
| Re-ID, 부팅 뒤 묶음이 올라옴(FileNotFoundError 이고 서버에 묶음 있음) | warning | 다음 부팅부터 자동으로 켜집니다(묶음 등록됨). 지금 켜려면 restart-tier0 |
| Re-ID, 묶음은 있는데 다른 이유 | warning | 묶음은 있는데 켜지지 않았어요. 관리자: 자세히·Tier 0 로그 확인 |
| 엔진 캐시 miss / vLLM 컴파일 캐시 miss | info | 이번 부팅이 느렸을 뿐, 할 일 없음(부팅 뒤 캐시에 올라감) |
| 엔진·vLLM 캐시 받기 오류 | warning | 관리자: GPU 캐시(S3) 연결·권한 확인 |
| 서비스 미기동(ppe·avatar·worker·vllm 없음/VRAM 부족) | warning | 각각 무엇이 안 되는지와 「서비스 목록에 추가하고 다시 켜세요」 |
| VLM 웜업 실패 / vLLM 시한 초과 | warning | 첫 AI 근거가 늦거나 시간 초과일 수 있어요(경보는 그대로) |
| 설정 파일 값이 환경 변수에 덮임(process_split 등) | warning | 의도한 설정인지 확인 |
| 사람 관절 보정 기본 꺼짐으로 live_calibration·drift_watch 설정 무효 | info | 의도된 설정이라 쓰이지 않음. 켜려면 SIMAI_TIER0_HUMAN_CALIBRATION=1 |
| 프로세스 분리로 꺼진 곁가지 | warning | 필요한 기능이면 분리를 끄세요 |

- **갱신**: `compose_disabled()` 가 근거(기동 서비스·캐시 결과·서버의 Re-ID 묶음 유무·Tier 0 표시 파일 status·웜업 결과)에서 목록 전체를 다시 계산하고 `Reporter.replace_disabled()` 가 통째로 바꾼다. 감시 루프가 READY 뒤에도 `report.interval_ms` 마다 표시 파일을 다시 읽어 호출한다. 해소(`status.reid.state == "active"`)되면 항목이 빠지고(부팅 때 캐시 miss 로 올린 「묶음 없음」도 낡은 값이라 지운다) 바뀐 때만 즉시 보고한다. 표시 파일에 `status` 가 아직 없으면(Tier 0 재시작 중) 앞 목록을 그대로 둔다(지어내지 않음, 빈 값으로 지우지도 않음).
- **Tier 0**: `Tier0Settings.config_info`(config_overrides 중 의도된 기본 설정 부분집합). 기동 `설정 무효` WARNING 은 그대로 모두 내고, 표시 파일 `status` 에서는 `disabled_features`(경고)에서 빼고 `info_notes` 로 싣는다.
- **EC2**: Flyway V83 `gpu_session_event.disabled_notices jsonb`(배열만, 추가만). 값이 있는 가장 최근 PROGRESS 행이 지금 값이라 해소되면 다음 보고(항목이 빠진 목록)가 최신이 된다. `GET /api/gpu/sessions/current` 의 `session.disabled_notices`(계약 밖).
- **화면(`GpuInfoLines`)**: warning 은 쉬운 말 줄 + 항목별 「자세히」(원문), info 는 「참고 N건」 접힘 안에. `disabled_notices` 가 없으면(옛 보고기) `disabled_features` 문구를 경고로 보인다.

## 명세에 없어 정한 수치와 근거

- 주기 = 보고 주기(`report.interval_ms`): 새 설정을 만들지 않고, 보고가 어차피 그 간격으로 나가므로 갱신이 보고보다 잦을 이유가 없다.
- detail 300자, text 200자: -747 의 200자 상한을 유지하고 원문 여유만 더했다.

## 버린 대안

- `disabled_features` 항목을 문자열|객체로 넓히기: 옛 수신 측이 깨진다.
- 문구 변환을 화면(front)에서: 문구·할 일 판단 근거(서버에 묶음이 있는지, 캐시 결과)가 부팅 보고기에만 있다.
- Tier 0 가 쉬운 말을 직접 내기: Re-ID 묶음 등록 여부 같은 배포 상태는 Tier 0 가 모른다.

## 잔여 위험·넘긴 일

- 배포 순서: payload 가 닫혀 있어 옛 EC2 는 1.3.0 보고를 400 으로 거부한다. EC2(서버) 먼저.
- 판 번호 v7.64.0·V83 는 지금 dev(v7.63.0·V81) 다음을 가정했다. 다른 MR 이 먼저 머지되면 README 줄·마이그레이션 번호를 다시 맞춘다.
- `restart-tier0` 직후에는 표시 파일이 잠깐 `status` 없이 새로 써지므로 항목이 한 주기 늦게 빠질 수 있다.
- 손 경로(`start-lobby`)에는 보고기가 없어 영향 없음(-747 과 같음).

## 코드·시험

- 코드: `deploy/scripts/simai_gpu_boot.py`(`notice`·문구표·`cache_findings`·`service_findings`·`tier0_findings`·`compose_disabled`·`Reporter.replace_disabled`·supervise 루프), `ai/services/tier0/{settings,service}.py`, `contracts/messages/gpu_session_report.schema.json`(+예시·반례·README), `server/.../GpuSessionService.java`·`V83`, `front/apps/admin/src/features/gpu/{types,gpuStatus}.ts`·`GpuInfoLines.tsx`.
- 시험: `ai/tests/deploy/test_simai_gpu_boot.py`, `ai/tests/services/tier0/test_human_calibration_settings.py`, `GpuSessionIntegrationTest`, `gpuStatus.test.ts`.
