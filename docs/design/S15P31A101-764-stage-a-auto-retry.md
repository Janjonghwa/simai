# S15P31A101-764 Stage A 자동 재시도(같은 배경·같은 초기값, 기준 카메라만 바꿈)

관련 명세: 4.3(세팅 정밀화 V2 Stage A, 보정 절대 품질 게이트·실패 시 재시도 안내), 3.2(분석 워커). 설계 선행: -465(Stage A),
-646(품질 게이트), -688(세팅 시간 예산), -711(비유한 삼각측량). 계약 변경 없음.

## 문제(2026-11-16 로비 세팅)

Stage A 가 세 번 거절됐고 매번 다른 쌍이었다: `worsened_pairs=['cam_D-cam_A']`(converged=True), `['cam_B-cam_C']`(converged=False,
`point_projection_not_converged`), `['cam_C-cam_D']`(converged=True), 그리고 품질 게이트 "cam_B-cam_C 홀드아웃 중앙값 0.87px, p90 8.74px".
실패마다 관리자가 세팅을 처음부터(배경 촬영부터) 다시 눌렀다. Carbon 분석(`.lead/carbon-design.md` 1.1)은 흔들림 원인으로 세팅마다 새로
뽑는 12장 중앙값 배경, MapAnything 초기값, 그리고 초기 재투영으로 정하는 기준 카메라(`_select_anchor_camera`)를 든다.

## 바꾼 것

1. `joint_ba_v2.run_stage_a_with_retry`: Stage A + 품질 게이트를 한 시도로 보고, 기하 사유로 실패하면 **같은 트랙(같은 배경 묶음)·같은
   MapAnything 초기 보정**으로 기준 카메라만 바꿔 최대 2번 더 푼다(`STAGE_A_MAX_ATTEMPTS = 3`). MapAnything·특징 추출은 다시 돌리지 않는다.
2. 첫 시도는 지금과 같은 호출(자동 기준 카메라)이다. 통과하면 바로 끝나므로 결과·보정 문서(sha256)가 예전과 같다.
3. 재시도 대상(`StageAAttempt.retryable`):
   - Stage A 검증 게이트 거절 중 **시간 안에 끝난 기하 거절**(미수렴·`point_projection_not_converged`·학습 점 깊이 무효·쌍 악화).
     `StageAError.anchor_retryable` 로 표시한다.
   - 품질 게이트 FAILED 이고 FAIL 사유가 **모두 쌍 홀드아웃**(`HOLDOUT_ERROR_HIGH`·`HOLDOUT_INVALID`)일 때(`quality_gate_retryable`).
   - 다시 풀지 않는 것: `INSUFFICIENT_FEATURES`·`BACKGROUND_INCOMPLETE`(특징 추출 단계), `FLOOR_FIT_FAILED`(그 앞 단계),
     높이 사유(`HEIGHT_MISMATCH`·`HEIGHT_UNKNOWN` — Stage A 는 중심 고정이라 기준 카메라로 바뀌지 않음), 시작 자세 비유한 삼각측량
     (초기 자세에서 정해져 기준 카메라와 무관), Stage A 시간 상한 초과(다음 시도도 넘는다), 보정 문서 스키마 실패.
   - 뒤 시도도 기하 사유로 실패해야 다음 후보로 간다.
4. 채택 규칙: 재시도에 들어가면 시간이 허락하는 한 남은 후보를 모두 풀고, **통과한 시도 중 `rank_key` 가 가장 작은 것**을 채택한다.
   `rank_key = (통과 0 / 게이트 미달 1 / 거절 2, 판정 순위 PASSED<NEEDS_CHECK<FAILED, 최악 쌍 비율, 평균 쌍 비율, 시도 번호)`.
   쌍 비율 = max(홀드아웃 중앙값 ÷ 3.0 px, p90 ÷ 6.0 px)(게이트 상한으로 나눔, 1 이하가 게이트 안). 홀드아웃 부족(NEEDS_CHECK) 쌍은
   근거가 없어 빼고, 투영이 깨진 쌍은 무한대. 비율은 소수 6자리 반올림, 같으면 앞선 시도(자동 기준 카메라에 가까운 쪽).
5. 모두 실패하면 `rank_key` 가 가장 작은 실패 시도(게이트 미달은 지표가 있어 거절보다 앞)의 사유로 **지금과 같은 코드·메시지**로 끝낸다.
   게이트 미달이면 그 시도의 `quality_gate`·`metrics.joint_ba` 스냅샷을 보낸다. 재시도했으면 메시지 끝에
   ` (같은 배경으로 자동 재시도 N회도 실패)`(카메라 ID 없음 → 화면 `setupFailures.ts` 의 쌍·카메라 추출에 영향 없음)를 480자 안에 남긴다.
6. 시간: 다음 시도 전 처리기의 `_stage_a_retry_fits` 가 `남은 시간 − 뒤 단계 몫 ≥ 지금까지 가장 오래 걸린 시도` 일 때만 허락한다.
   뒤 단계 몫 = 카메라 확인 전체(`camera_check_wait_s`, 기본 600 s, 확인기가 없으면 0) + 맵 몫(600 s, REUSE 요청이면 결과 몫 30 s).
   실행 상한이 없으면(시험·수동) 늘 허락한다. 취소도 여기서 본다.
7. 진행률: 시도 1 은 48→57 %, 시도 2·3 은 57→58·58→59 %(단조 증가 유지). 통과 뒤 59→61 % 는 그대로다.
8. 로그: 시도마다 `Stage A 시도 i/n: {attempt, anchor_camera_id, outcome, passed, converged, blockers, worsened_pairs,
   pairs{쌍: median_px, p90_px}, seconds}` 한 줄, 채택·전부 실패 한 줄.

## 시도마다 무엇을 바꾸나: 기준 카메라(근거)

- `Problem` 은 기준 카메라의 회전·중심을 **고정**하고(`self.free = 기준 제외`), 나머지 카메라 회전을 초기값 대비 5° 사전 구속으로 푼다.
  중심이 모두 고정이라 기준 카메라의 초기 회전 오차는 풀리지 않고 남으며, 나머지 카메라가 그 오차에 맞춰 비틀린다. 그래서 기준 카메라를
  바꾸면 같은 입력에서도 해가 실제로 달라진다.
- 합성 실험(`test_joint_ba_v2.synthetic_calibration_and_tracks`, cam_A 1.2°·cam_B 2.5°·cam_C 1.5° 틀림, cam_D 참): 기준 cam_D →
  4쌍 홀드아웃 0.0001~0.0005 px 통과, cam_A → 0.4~0.9 px(p90 2.2 px), cam_C → 2.18 px(p90 4.9 px), cam_B → `worsened_pairs=
  ['cam_C-cam_D','cam_D-cam_A']` 거절. 11-16 의 "converged 인데 쌍 악화" 꼴이 기준 카메라 하나로 재현·해소된다.
- 후보 순서 `stage_a_anchor_order`: 초기 자세에서 전체 트랙 재투영 중앙값이 작은 순(같으면 카메라 순서). 첫째는 지금의 자동 선택과
  같고(`_select_anchor_camera` 가 같은 `_anchor_ranking` 을 쓴다), 재시도는 아직 쓰지 않은 다음 후보다. 입력이 같으면 순서도 같다.
- 시험 장면(cam_A 0.5°·cam_C 3° 틀림, 게이트 p90 1.5 px): 시도 1 기준 cam_A p90 1.94 px FAIL → 시도 2 cam_B 0.0007 px 통과 → 시도 3
  cam_D 통과, 평균 쌍 비율이 작은 시도 2 채택.

## 버린 대안

- **배경 프레임 부분집합(다른 시각 표본)**: 운영 묶음(`ProductionSiteSetupStages._ready_bundle`)은 Tier 0 가 12장 이상 표본에서 만든
  카메라별 **중앙값 사진 1장**만 준다. 워커에는 다른 시각 표본이 없어 부분집합을 고를 수 없다. 넣으려면 Tier 0 묶음·계약을 바꿔야 하고,
  배경을 다시 고르는 것은 "같은 배경 묶음" 조건과도 어긋난다.
- **홀드아웃 분할 시드(`seed`) 바꾸기**: 같은 해를 다른 검증 집합으로 다시 재는 것에 가깝다. 통과할 때까지 시드를 바꾸면 홀드아웃이
  검증 구실을 못 한다(검증 집합 고르기). 학습 80% 도 바뀌지만 기하 결함(기준 카메라 오차 잠김)은 그대로다.
- **쌍별 가중 균형**: 명세 4.3 은 "쌍별 균형 가중치를 둔다"고 하지만 지금 `Problem.residual` 에는 쌍별 가중이 없다(Huber 만, 명세 차이로
  보고). 재시도 전용 손잡이로 넣을 수는 있으나 11-16 실패를 바꾼다는 근거가 없고 목적함수를 바꾸는 일이라 따로 검증해야 한다.
- **MapAnything 다시 돌리기·배경 다시 받기**: 요청 범위 밖(GPU·시간). 관리자 「다시 시도」가 이미 그 경로다.
- **첫 통과 시도에서 멈추기**: 더 빠르지만 요청이 "통과한 시도 중 가장 좋은 것"이다. 재시도에 들어간 경우에만 한 번(약 10~20 s) 더 든다.

## 정한 수치·근거

| 값 | 근거 |
|---|---|
| 최대 3 시도(재시도 2) | 요청. 4대면 기준 후보 4개 중 3개, 15차 Stage A 약 20 s·BA 상한 10 s 라 최악 약 50 s 로 Stage A 몫 60 s 안 |
| 뒤 단계 몫 = 카메라 확인 600 s + 맵 600 s(REUSE 30 s) | -688 예산(`SETUP_BUDGET_CAMERA_CHECK_S`·`SETUP_BUDGET_MAP_S`, `camera_check_reuse_reserve_s`) |
| 시도 예상 = 지금까지 가장 긴 시도 | 같은 입력·같은 문제 크기라 시도 시간이 비슷하다. 최댓값으로 보수적으로 |
| 쌍 비율 = 게이트 상한으로 나눈 값 | 게이트가 쌍별 중앙값·p90 상한으로 판정하므로 같은 잣대로 "상한까지의 여유"를 잰다 |

## 계약

계약 스키마에 시도별 요약을 실을 자유 필드가 없다(`metrics.joint_ba`·`quality_gate`·`progress` 모두 `additionalProperties: false`).
그래서 스냅샷에는 **채택(또는 실패 보고) 시도의 값만** 기존 필드로 싣고(`metrics.joint_ba.anchor_camera_id`·`pair_holdout`,
`quality_gate`), 시도별 요약은 로그에만 남긴다. 화면·서버에 시도별 요약을 보이려면 `site_setup` 마이너 변경(예: `metrics.joint_ba.attempts`)
이 필요하다 — Lead 결정으로 넘긴다.

## 시험

- `tests/calibration/test_joint_ba_v2.py`(진짜 BA): 게이트 미달 → 다음 기준 채택(문서가 그 기준으로 한 번 푼 것과 같음), 같은 입력 두 번 →
  같은 시도 순서·같은 content_hash·버전, 첫 시도 통과 → 예전 단일 호출과 같은 문서, 전부 실패 → 가장 나은 시도 보고, 시간 거부 →
  한 번만, `INSUFFICIENT_FEATURES`·높이 차 → 재시도 없음, 검증 거절의 `anchor_retryable`(쌍 악화 True, 시간 초과 False),
  `quality_gate_retryable`·`quality_gate_score` 규칙.
- `tests/services/worker/test_site_setup.py`(처리기): 첫 거절 → 두 번째 통과 채택(스냅샷 기준 카메라, 로그), 전부 거절 → 같은 코드·
  `worsened_pairs` 메시지 + 재시도 안내, 전부 게이트 미달 → 가장 나은 시도 스냅샷, 남은 시간 < 맵 몫 → 재시도 안 함,
  `INSUFFICIENT_FEATURES` → 재시도 안 함.

## 잔여 위험

- 기준 카메라를 바꿔도 원인 B(카메라 중심 `C0` 고정 시차, Carbon 1.2)나 배경 특징 희박은 고치지 못한다. 그런 세팅은 3번 모두 실패하고
  지금처럼 관리자 다시 시도로 간다(시간 약 20~40 s 더 씀).
- 여러 시도 중 홀드아웃이 가장 좋은 것을 고르므로 홀드아웃 지표가 약간 낙관적으로 치우친다(최대 3개 중 선택). 사람 걷기 「카메라 확인」이
  뒤에서 독립 검산한다.
- 결정성은 시도 순서·결과에 대해 성립하고, 시간 상한으로 멈추는 지점만 실행 시간에 따라 달라진다.
- 11-16 실측 트랙으로 기준 카메라 교체 효과를 재현하지 못했다(그 세션 트랙 캐시 없음). 다음 GPU 세션 로그의 `Stage A 시도` 줄로 확인한다.

## 넘긴 일

- 시도별 요약을 계약에 싣기(`site_setup` 마이너, Lead 결정).
- 명세 4.3 "쌍별 균형 가중치" 문구와 코드 차이 정리(명세 정리 MR 또는 별도 티켓).
