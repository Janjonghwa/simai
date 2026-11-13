# S15P31A101-684 World 신원 불확실 원인별 계측과 오억제·불필요한 불확실 수정 (작업 1~4)

> 1단계(아래 "왜 먼저 계측인가" ~ "넘긴 일")는 계측(작업 4)이다.
> 2단계(문서 끝 "2단계: 작업 1~3 수정")는 결함 3건 수정이다. 수정마다 재생 비교용 스위치를 두어, 같은 녹화에서 하나씩 끄고 켜며 원인 비중을 나눠 잰다.

## 관련
- 기획서 5.2(World Track·identity_confidence), 19.1(Re-ID 단계 도입). Jira S15P31A101-684 작업 4번.
- 이 단계는 판정을 바꾸지 않는다. 같은 스토리의 1~3번(별개 인물 반증 우회, 그림자·경쟁 플래그 순서, Re-ID 공가시성) 수정 효과를 원인별로 나누어 재기 위한 기준선 계측이다.

## 왜 먼저 계측인가
- 14차 세션 녹화의 World ID 불확실 비율은 65.8% 였다.
- 지금 `uncertain_reasons` 는 Cross-view 클러스터가 불확실로 표시된 **횟수**다. World Track 이 실제로 불확실이던 **시간**도, 어떤 사유가 그 시간을 만들었는지도 알 수 없다.
- 그 사유가 새로 붙어서 불확실한 것인지, `uncertain_hold_ms`(1초) 유지 때문에 남은 것인지도 나뉘지 않는다.
- 1~3번을 한꺼번에 고치면 무엇이 얼마나 줄였는지 가를 수 없다. 그래서 계측을 먼저 넣고 같은 녹화로 기준선을 잰 뒤, 수정을 하나씩 켜며 차이를 본다.

## 바꾼 것
- `ai/src/simai_ai/tracking/world_track.py`
  - `WorldTrackSnapshot.identity_flags_raised` 를 추가했다. `identity_flags` 중 이번 시각에 새로 붙은(갱신된) 사유다.
  - 판정·world_state 에는 쓰지 않는다. 계약에도 싣지 않는다.
- `ai/src/simai_ai/tracking/diagnostics.py` 의 `TrackingDiagnostics` 에 `identity` 항목을 추가했다. 누계(상태 파일 `tracking_diagnostics`)와 30 s 창(주기 로그) 둘 다에 들어간다.
  | 키 | 뜻 |
  |---|---|
  | `identity_ms` | (CONFIRMED·OCCLUDED → `CONFIRMED` 묶음 / `TENTATIVE`, identity_confidence) 별 Track·시간 ms |
  | `uncertain_reason_ms` | 확정 묶음 Track 이 IDENTITY_UNCERTAIN 인 동안 사유별 시간. 사유가 겹치면 각각 센다 |
  | `uncertain_sole_ms` | 그 사유 하나만 남아 있던 시간. 원인 비중은 이 값으로 본다 |
  | `uncertain_hold_only_ms` | 새로 붙은 사유 없이 1초 유지로만 불확실이던 시간 |
  | `uncertain_raised` | 확정 묶음 Track 에 사유가 새로 붙은 Track·tick 수 |
  | `uncertain_episodes_ms` | 불확실 구간이 끝난 원인(CLEARED·LOST·MERGED_AWAY)별 길이 분포. 열려 있는 구간 수는 누계의 `uncertain_open` |
  | `local_key_moves` / `local_key_swaps` | (카메라, 로컬 트랙)이 살아 있는 다른 World ID 로 옮겨 간 횟수 / 같은 tick 에 두 ID 가 서로 맞바꾼 쌍 수 |
- 주기 로그 한 줄 앞부분에 "확정 불확실 비율(유지만 ms), 단독 사유, 로컬 트랙 이동/맞바꿈"을 더했다.
- `ai/experiments/track_replay/replay_tracking.py` 는 이미 `metrics.json` 의 `diagnostics` 에 이 요약을 싣는다. 따라서 재생 도구는 바꾸지 않았다.

## 정한 수치와 근거
- **시간 붙이기**: 기존 `state_ms` 와 같이 직전 tick 의 상태에 그 사이 시간을 붙인다. 그래서 확정 묶음 `identity_ms` 합 = `state_ms` 의 CONFIRMED+OCCLUDED 이고, SIM 시험으로 고정했다.
- **불확실 구간 길이 구간 경계**: 100, 300, 1000, 2000, 5000, 10000, 30000, 60000 ms. 1초 유지 경계(1000)와 G2 ID 유지 판단 단위(수 초~분)를 나눠 보려고 정했다.
- **swap 대리 지표의 정의**: 정답 없이 World ID 단위 맞바꿈을 보려고, 같은 tick 에 두 ID 가 서로의 로컬 트랙을 넘겨받은 경우로 정했다.
  - 병합으로 사라진 ID 의 로컬 트랙을 넘겨받은 것은 이동으로도 세지 않는다.
  - 기존 `replay_tracking.segment_metrics.id_changes` 는 "ID 집합이 바뀐 횟수"라 두 ID 가 사람만 바꾸면 집합이 같아 잡지 못한다.

## 버린 대안
- **WorldTrackManager 안에 사유별 시간 누계를 직접 두기**: 판정 객체에 계측 상태가 섞이고, 재시작·복원 경로에서 함께 옮겨야 한다. 스냅샷에 이미 있는 `identity_flags` 와 새 `identity_flags_raised` 만으로 관찰자 쪽에서 셀 수 있다.
- **정답 궤적 기반 swap**: 운영·녹화에는 정답이 없다. SIM 의 `tests/tracking/sim_world_track.py` `id_changes` 는 정답이 있을 때 쓴다.

## 시험
- `ai/tests/tracking/test_identity_diagnostics.py`(새, 6건)
  - 10 tick 마다 사유가 한 번 붙는 입력에서 불확실 시간의 90% 가 유지만으로 남은 시간으로 나뉜다.
  - 단독·겹친 사유 시간을 센다.
  - TENTATIVE 시간을 따로 센다.
  - 구간 끝 원인(CLEARED·LOST·MERGED_AWAY)을 가린다.
  - 맞바꿈·한쪽 이동·병합 넘겨받기를 구별한다.
  - 창 초기화와 로그 한 줄을 확인한다.
- `ai/tests/services/tier0/test_tracking_diagnostics.py`: SIM 장면에서 확정 묶음 신원 시간 합이 상태 시간과 같다. 기존 "계측 고장이 판정에 번지지 않는다" 시험이 그대로 통과한다.
- 실행(`ai/`, 2026-11-13)
  - `uv run --offline pytest tests/services/tier0/test_tracking_diagnostics.py tests/tracking`: 586 passed, 11 skipped(ultralytics·cv2 없음)
  - `uv run --offline pytest tests/services/tier0 tests/reid`: 564 passed, 14 skipped
  - `lint-imports`: 7 kept

## 잔여 위험
- `local_key_swaps` 는 로컬 추적기 자체의 swap(한 카메라 안에서 두 사람의 로컬 ID 가 바뀜)은 잡지 못한다. 대리 지표이고 정답 지표가 아니다.
- 사유가 붙었다가 같은 tick 안에서 지워지는 경우는 스냅샷에 나타나지 않는다. 현재 구현에는 그런 경로가 없다.
- 계측은 tick 마다 살아 있는 Track 수 × 로컬 키 수만큼 더 돈다. 판정 루프 지연 영향은 GPU 세션에서 확인하지 않았다.

## 넘긴 일 (같은 스토리)
1. 14차 세션 녹화로 기준선을 잰다.
   - `ai/experiments/track_replay/detect_cache.py`(GPU 1회)로 검출 캐시를 만든다.
   - `replay_tracking.py --config <운영 기본>` 으로 `metrics.json.diagnostics.identity` 를 남긴다.
2. 1·2·3번 수정을 하나씩 설정으로 켜 같은 캐시로 다시 재고, `uncertain_sole_ms`·`uncertain_hold_only_ms`·`local_key_swaps`·새 ID·누락 차이로 원인 비중을 판정 기록으로 남긴다.
3. 그 결과로 5~8번(사유별 정상 복귀 등) 순서를 정한다.

## 2단계: 작업 1~3 수정

외부 재현(2026-11-13 ID 감사)의 실패 시험 3건을 `ai/tests/tracking/test_identity_regressions.py` 로 옮겼다. 각 결함 시험 옆에 정상 대조군을 함께 두었다. 수정 전 dev 에서는 3 failed / 3 passed 였다.

| 과제 | 원인(코드) | 수정 | 스위치(기본 True) |
|---|---|---|---|
| ID-01 별개 인물 반증 우회 | `_split_remainder_target` 3번 분기(Re-ID 외형 일치 억제)가 `is_distinct`(동시 관측으로 확인한 다른 사람)를 보지 않았다. 1·2번 분기는 본다 | 3번 분기 앞에서 `is_distinct` 이면 억제 후보로 두지 않는다 | `reid_split_respect_distinct` |
| ID-02 그림자 뒤 남는 애매 사유 | 3단계 배정에서 아직 아무 Track 도 받지 않은 근처 클러스터를 경쟁 클러스터로 보고 `AMBIGUOUS_TRACK_MATCH` 를 붙인다. 6단계에서 그 클러스터가 같은 Track 의 시점 분할 그림자로 확인돼 걸러져도 사유는 남았다. 감사 초안은 `TWO_VIEW_COMPETITOR_JOINT` 로 적었으나 재현에서 남은 사유는 이것이다 | 애매함의 원인이 "받은 Track 없는 경쟁 클러스터"뿐인 Track 을 기록한다. 그 클러스터가 모두 그 Track 의 그림자로 확인되면 이번 시각 사유를 지운다 | `shadow_clears_ambiguity` |
| ID-03 질의 가시성 누락 | `_cluster_gallery_sim` 의 `gallery.similarity` 호출이 `query_visibility` 를 넘기지 않아, 공통 가시 부위가 없어도 외형이 신원 근거가 됐다 | 질의 특징의 `visibility` 를 넘긴다. 공통 부위가 없으면 갤러리가 None 을 돌려준다 | `reid_query_visibility` |

ID-02 에서 사유를 지우지 않는 경우는 다음과 같다(보수적으로 불확실을 유지).
- 다른 Track 과 다툰 경우: 같은 클러스터를 비슷한 비용으로 원하는 다른 Track 이 있거나, 경쟁 클러스터를 다른 Track 이 받았다.
- 경쟁 클러스터 중 그림자로 확인되지 않은 것이 하나라도 남았다.
- 그 Track 이 swap 의심 중이다(`swap_suspected_until ≥ t`). swap 검사도 같은 사유를 붙이므로 구별하지 않는다.

지운 횟수는 `WorldTrackManager.diag["identity.shadow_ambiguity_cleared"]` 로 센다. 계측 상태 파일의 `world.manager` 에도 나온다.

### 시험
- `test_identity_regressions.py`(9건)
  - 수정 후 동작 고정: 위치가 튄 다른 사람은 흡수되지 않고 재연결 애매 표시를 단 새 ID 가 된다
  - 결함 3건과 정상 대조군 3건
  - 스위치를 끄면 이전 동작(억제 1회, 사유 30 tick, 가시성 무시 1.0)이 다시 나오는지
  - 그림자 사유 지움 횟수
- 외부 재현 파일(`test_identity_reproductions.py --runxfail`)은 수정 전 이 브랜치에서 3 failed / 9 passed 였다. 수정 뒤 저장소로 옮긴 같은 시험이 통과한다.

### 잔여 위험
- **ID-01 반증 오검출**: 일반 동시 관측 반증은 마지막 동시 관측부터 15 s 뒤 만료되고, SPLIT 반증만 영구다(명세 5.2 "병합 금지 반증과 만료"). 한 사람이 일시적 검출 오류로 두 사람으로 동시 관측되면, 그 반증이 살아 있는 동안 진짜 분할 나머지도 Re-ID 분기로는 억제되지 않아 새 ID 가 생길 수 있다. 1·2번 분기와 같은 규칙이다.
- **ID-01 수정 뒤 위치가 튈 때 ID 가 갈림(재생 페이지로 확인)**:
  - 장면: 다른 사람으로 확인된 B 가 부분 가림 중 2대 삼각측량 오차로 A 쪽(B Track 이 갈 수 없는 거리)으로 튀어 보인다.
  - 수정 전: B 를 A 의 분할 나머지로 억제했다. B 의 존재가 A 에 묻히고 A 가 불확실해진다.
  - 수정 후: `reconnect_skip.unreachable` 로 B 의 옛 Track 에 잇지 않고, 재연결 애매(`REASSOCIATION_AMBIGUOUS`)·불확실 표시를 단 새 ID 가 생긴다. 이 동작은 명세 5.2 그대로다("근거가 모자라면 새 ID … IDENTITY_UNCERTAIN", "한 번이라도 갈 수 없는 곳에서 관측됐으면 병합 금지 쌍").
  - B 가 제자리로 돌아올 때도, 로컬 트랙이 옛 Track(가림 3 s 뒤 LOST)과 새 ID 두 곳에 나뉘어 있어 `reconnect_skip.multiple_holders` 로 ID 가 하나 더 생긴다. 60 tick 합성 장면에서 발급 ID 는 수정 전 3, 수정 후 4다.
  - 안전 쪽으로는 수정 후가 낫다. 다른 사람의 위험이 A 에 묻히지 않고 새 ID 에 불확실 표시가 붙는다. 다만 ID 수는 는다.
  - 시험 `test_jumped_distinct_person_gets_flagged_new_id_not_absorbed` 로 수정 후 동작을 고정했다.
  - 줄이려면 명세 5.2 를 바꿔야 한다(Lead 결정). 후보는 아래 둘이다.
    - (가) 새 ID 가 옛 Track 의 로컬 트랙에서 태어난 재연결 애매 Track 이고, 뒤에 둘의 로컬 트랙이 한 클러스터로 다시 모이면 옛 ID 로 합친다.
    - (나) 보정과 독립적인 신원 유지: 로컬 트랙·외형 이력으로 신원을 잇고, 튄 관측은 위치 갱신에 쓰지 않는다.
  - 이 브랜치에서는 하지 않는다.
- **ID-03**: 공통 가시 부위가 없으면 외형 근거가 사라진다. 그래서 Re-ID 재입장·분할 억제가 기하 근거로만 판단되는 경우가 늘 수 있다. 실영상 효과는 재생으로 잰다.
- **ID-02**: 지우는 근거가 `_shadow_target`(카메라 비겹침·방향별 반경·한 점 재투영·동시 검출 반증)에 기댄다. 그림자 판정이 틀리면 애매함 표시도 같이 사라진다.

### 원인 비중 재기(같은 스토리)
같은 검출 캐시로 다음 네 설정을 각각 재생하고, 1단계 계측 차이를 판정 기록으로 남긴다.
1. 세 스위치 모두 False(기준선, 수정 전 동작)
2. ID-01 만 True
3. ID-02 만 True
4. ID-03 만 True

5번은 셋 모두 True. 비교할 계측은 `uncertain_sole_ms`, `uncertain_hold_only_ms`, `local_key_swaps`, 새 ID, `reid.split_suppressed_same`, `identity.shadow_ambiguity_cleared` 다.
