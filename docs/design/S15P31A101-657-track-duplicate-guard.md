# S15P31A101-657 사람 트랙(World ID) 갈라짐 방지

14차 GPU 세션(2026-11-12 로비, 4대, 실제 3명)에서 33분간 World ID 0001→0089, 화면 "작업자 4명", 낙상 1회에 R4 가 두 ID 에
0.5 s 차로 RAISED 되고 4분 넘게 안 꺼졌다. CONFIRMED 소실 52회 중 되찾음 21회(약 60% 새 ID). 워치·PPE·아바타가 모두 World ID
에 기대므로 Tier 0 추적에서 갈라짐을 막는다. 계측·분석 자료: Gemini Carbon 분석(`.agy/carbon-track-plan.md`, 커밋 안 함),
30초 추적 계측(`.agy/track-log.txt`), 저널 요약(`.agy/journal-summary.txt`).

## 관련 명세

- 5.2 추적 3계층(Cross-view 계산 순서 4단계 완전 연결, World Track 연결 1~7단계, 분할 나머지 억제, 늦은 재연결·ID 교정, Re-ID 보조)
- 5.2 head_z: "UNCERTAIN head_z 만으로 '낙상이 아니다'를 확정하지 않는다"
- 6.2·6.4 RiskEvent 생명주기·관측 해소, 7.2 R4, 3.2 저널
- AGENTS 규칙 1(Tier 0 정본), 2(모르면 비운다), 7(관측/추정), 8(Re-ID 는 기하 게이트 뒤 보조)

## 바꾼 구조

| 위치 | 바꾼 것 |
|---|---|
| `ai/src/simai_ai/geometry/triangulation.py` | `joint_consistency_px`: 카메라가 겹치지 않는 view 묶음들을 관절마다 한 점으로 풀었을 때 재투영 오차 최댓값(묶음마다 1대 이상·합 3대 이상, 모자라면 None) |
| `ai/src/simai_ai/tracking/cross_view.py` | 4대 클러스터에서 6쌍 중 5쌍 통과 + 남은 1쌍이 EPIPOLAR·TRIANGULATION 이어도, 5쌍 2-view 3D 가 0.25 m 안에 모이고 4대 엉덩이 한 점 재투영 ≤ 20 px 면 묶는다(`four_view_soft_pair_enabled`). 그 기각은 금지 행렬(veto)에 넣지 않는다(2·3대 병합은 `_graph_linkage` 가 그대로 막음). 같은 카메라 금지 유지 |
| `ai/src/simai_ai/tracking/world_track.py` | ① 그림자 억제 `_shadow_target`: 이번 시각 갱신된 확정 Track 과 카메라 비겹침 + `_geometric_shadow`(방향별 반경 + 한 점 재투영) + 반증 없음이면 소유 이력·IDENTITY_UNCERTAIN 과 무관하게 새 ID·늦은 재연결보다 먼저 거른다(IDENTITY_UNCERTAIN 은 붙이지 않음). ② 동시 활성 중복 병합 `_active_duplicate_merges`: 둘 다 갱신된 두 Track 이 그림자 판정을 4시각 연속 통과하면 기존 `_merge`(MERGE·aliases·identity_revision+1). ③ 확정 지연 `_delay_confirmation`: 비겹침 확정 Track 1.35 m 안에서 저신뢰(클러스터 IDENTITY_UNCERTAIN 또는 골반 VALID 아님)로 갱신된 TENTATIVE 는 6회. ④ `_distinct` 를 TTL(15 s, 매 동시 관측마다 갱신) 사전으로, 분할 쌍은 `_distinct_fixed`(영구). 새 ID 첫 시각 "unreachable" 등록 제거. ⑤ 분신 이력 `_twins`(그림자 근거가 있었던 쌍, 둘 다 살아 있는 동안·장기 보류 포함): Re-ID 재입장·지연 병합의 2위 경쟁자, 갤러리 갱신 동결, crowd 판정에서 서로를 뺀다. 유사도 동률이면 먼저 만든 ID. ⑥ 기립 키(머리 − 발목) 표본과 재입장 지연 병합의 키 거부·동률 깨기. ⑦ `duplicate_pair_count` |
| `ai/src/simai_ai/tracking/diagnostics.py`, `ai/services/tier0/perception.py` | 30초 줄에 `확정승격, 임시소멸, 동시확정최대, 중복쌍최대, 활성병합, 그림자억제, 소실/되찾음 N/기하 g+ReID r, 키거부, 키동률깨기` |
| `ai/src/simai_ai/risk/rules/r4_fall_suspect.py` | ① 분신 R4 보류: 기립 이력(지면 기준 머리 ≥ 1.35 m 표본 5개) 없이 낙상 도중 태어난(첫 관측→급하강 ≤ 2.5 s) 트랙의 발생 판정을, 카메라가 겹치지 않는 1.35 m 안의 먼저 본 낙상 트랙이 있으면 유지(holding, `view_split_twin_suspect`)로 바꾼다. ② 2대 관측 기립 회복: UNCERTAIN 머리라도 지금 접지 기준 머리 ≥ 1.2 m 이고 관측 골반 − 접지 ≥ 0.65 m, 2대 이상이면 회복 근거로 센다. ③ 병합으로 사라진 ID(남는 ID 의 aliases)는 이력을 버려 '급하강 직후 관측 상실'을 내지 않는다 |
| `ai/src/simai_ai/risk/world.py` | 트랙의 `aliases`(world_state 기존 선택 필드)를 읽는다 |
| `ai/src/simai_ai/risk/lifecycle.py` | `correct_identity` 충돌: 옛 ID 이벤트를 남는 이벤트에 귀속(origin_world_ids)하고 RESOLVED(변경 사유 `IDENTITY_MERGED`, `MergeRecord`)로 마감. 검증 상태 유지, 재개 대상 아님, 현장 경보 냈으면 AlertClear |
| `ai/src/simai_ai/eventlog/journal.py`·`outbox.py` | 병합 흡수 RESOLVED 는 판정 전이가 아니다. outbox 가 `merge` → 계약 `resolution`(IDENTITY_MERGED)로 바꾸고 그 기록만 2.3.0 으로 보낸다 |
| `contracts/` | `common/defs` `identity_merge_resolution`, `risk_event_transition` 2.3.0(resolution oneOf, 2.2.x 이하 금지), 예시·반례, README v7.40.0. I1 world_state 변경 없음 |

## 새 수치와 근거 (모두 설정값)

| 설정 | 값 | 근거 |
|---|---|---|
| `WorldTrackConfig.shadow_radius_base_m` | 0.6 m | 기존 분할 나머지 반경(명세 5.2 초기값). 시선 수직(옆 사람 방향)은 이 근처에 남는다 |
| `shadow_radius_sigma_k` / `shadow_radius_max_m` | 2.5 / 1.35 m | 예각 2대 깊이 오차 0.5~1.0 m(Carbon 2.2 표, 합성 13° 쌍에서 ±14 px → 0.7 m 재현)를 덮고 gate_max_m(1.5 m)보다 작게 |
| `shadow_keypoint_sigma_px` | 8 px | 몸통 회전·숙임에서 엉덩이 점이 10~18 px 밀리는 것을 2σ 로. 관절 게이트의 2 px(VALID 판정)와 별개 |
| `shadow_reprojection_px`, `CrossViewConfig.four_view_pelvis_reprojection_px` | 20 px | triangulation 이상치 기준과 같다. 5 m 거리 0.5 m 옆 다른 사람은 f≈885 px 에서 한 점으로 풀면 각 카메라 수십 px(합성 시험에서 2인 0.45~0.8 m 모두 거부) |
| `active_merge_confirm_ticks` | 4 (0.4 s) | merge_confirm_frames(3)보다 하나 더 보수적. 낙상 R4 발생(낮은 자세 1.5 s + 지속)보다 먼저 합쳐진다 |
| `confirm_hits_near_confirmed` / `confirm_delay_radius_m` | 6 (0.6 s) / 1.35 m | 그림자는 억제·병합이 먼저 잡도록 시간을 번다. 전역 confirm_hits(3)는 그대로 |
| `distinct_ttl_ms` | 15 s | 함께 보이는 동안은 매 시각 갱신되므로 진짜 두 사람은 계속 금지. 1프레임 이중 박스·도달 불가 한 번이 영구 금지가 되던 것을 막는다. reconnect_window(10 s)보다 길게 |
| `stature_min_samples` / `stature_window` | 10 / 30 | 기립 1 s 뒤부터 쓰고 3 s 창. 재입장 지연 병합 창(6 s) 안에 모인다 |
| `stature_min_tolerance_m` / `stature_sigma_k` | 0.07 m / 2.5 | 2대 관측 머리·발목 Z 표본 잡음 각 3~5 cm 의 합. 허용치 = max(0.07, 2.5·√(σa²+σb²)), σ = MAD×1.4826. 거부는 허용치의 2배 초과(확실히 다름), 동률 깨기는 최선만 허용치 안이고 겨룬 후보 전부 2배 이상 |
| `standing_min_height_m` / `standing_min_pelvis_ratio` | 1.2 m / 0.45 | 기립만: 바닥 앉기·웅크림·누움 제외, 의자 앉기(비율 0.3~0.4) 제외 |
| R4 `R4_TWIN_RADIUS_M` / `R4_TWIN_BORN_MS` / `R4_TWIN_STANDING_SAMPLES`·`_M` | 1.35 m / 2.5 s / 5·1.35 m | 그림자 반경과 같다. 낙상 0.5~1 s + 여유. 기립 이력 0.5 s, 앉기 머리(약 1.25 m)보다 높게 |
| R4 `R4_RECOVERY_UNCERTAIN_MIN_M` / `R4_RECOVERY_PELVIS_MIN_M` | 1.2 m / 0.65 m | 해소 임계 1.0 m + 0.2 m 여유. 골반 서기 0.9~1.0 m, 의자 앉기 0.45~0.55 m |

## 안전장치

- 같은 시각 같은 카메라에서 서로 다른 로컬 트랙으로 따로 보인 두 사람은 억제·병합하지 않는다: 카메라가 겹치면 그림자 후보가
  아니고(`_geometric_shadow`·병합 쌍 첫 조건), 받은 클러스터 밖 1대 클러스터 검출도 반증(`_co_observed_now`), 그 근거를 TTL
  안에 본 쌍(`_is_distinct`)은 제외. 분할(SPLIT) 쌍은 영구 제외.
- 반증이 없어도 기하가 맞아야 한다: 0.5 m 옆 사람은 한 점 재투영이 벌어진다. 늘 2대씩 따로 보인 0.5·0.8 m 두 사람도 두 ID
  유지(시험). 명세의 "첫 등장 근접 두 사람은 두 ID" 는 확정 Track 만 그림자 근거가 되고 첫 시각 새 Track 은 TENTATIVE 라 그대로다.
- R4 분신 보류는 카메라가 겹치면 하지 않는다(같은 카메라에 함께 보인 두 사람의 동시 낙상은 둘 다 발생, 시험).
- 키는 후보를 빼거나 외형 동률만 깬다. 위치·기하 게이트를 넘어서지 않고, 새 클러스터의 첫 시각 재연결에는 쓰지 않는다(표본 1개).

## 명세와 다른 점 (명세 정리 MR 에 넘김)

1. 5.2 분할 나머지 억제는 "주인 없는 클러스터는 기하만으로 억제하지 않는다". 이 티켓은 확정 Track 그림자에 한해 소유 이력
   없이도 억제한다. 다만 거리만이 아니라 두 묶음 한 점 재투영(같은 3D 점인지)을 요구하고 확정 Track 만 근거로 써서 "첫 등장 근접
   두 사람" 은 그대로 두 ID 다. 억제 때 IDENTITY_UNCERTAIN(CROSS_VIEW_UNCERTAIN)도 붙이지 않는다(한 점으로 맞아 신원이 애매하지
   않음. 14차에서 매 시각 붙어 PPE·R4 해소가 막혔다). 명세 5.2 문구 갱신 필요.
2. 5.2 "전역 확정 횟수(3회)는 늘리지 않는다": 전역은 그대로이고 확정 Track 근처 저신뢰 후보만 6회.
3. 5.2 head_z "UNCERTAIN head_z 만으로 '낙상이 아니다'를 확정하지 않는다": UNCERTAIN 머리 + 관측 골반 + 접지(독립 관측 두 가지)
   일 때만 회복을 센다. 머리 하나로는 여전히 해소하지 않는다(시험). 명세에 "골반과 함께면 회복 근거" 를 적어야 한다.
4. 6.4 병합 흡수 마감(IDENTITY_MERGED)은 관측 해소가 아닌 RESOLVED 의 두 번째 종류다(운영자 종료와 같은 자리, resolution).
5. 5.2 Re-ID: 기립 키를 재입장 보조 근거로 더했다(사용자 결정). 명세 19.1·5.2 갱신 필요.

## Carbon 분석: 맞은 것 / 틀린 것

맞은 것(코드로 확인): 새 ID 발급은 `WorldIdIssuer.issue()` 한 곳, 4대 클리크 예외가 RESIDUAL 만(`_graph_linkage`), Step 4 owner
반경 0.35 m, `split_suppress_ownerless=False` 와 `c.uncertain` 때 기하 억제 꺼짐, 3시각 무조건 확정, 활성-활성 쌍은 병합 근거에
오르지 않음(`stale_ids`), `_distinct` 무기한, Re-ID 2위 경쟁자에 분신이 들어감, R4 의 급하강 기준이 새 트랙의 첫 표본에서 시작,
2대 관측 UNCERTAIN 머리가 회복을 매번 되돌림, `correct_identity` 충돌 시 IDENTITY_CONFLICT 로 방치.

틀리거나 부정확한 것:
- 줄 번호·이름: `_reid_margin()`·`_advance_recovery()`·`_HeadHistory`·`possible_duplicate_radius_m` 은 없다(실제는
  `_reid_reconnect_target`/`_reid_late_reentry_merge` 의 rival 계산, R4 `_high()` 의 `head_uncertain_recovery`, `_Memory.samples`,
  `duplicate_radius_m` 0.35 m). POSSIBLE_DUPLICATE 반경은 0.8 m 가 아니라 0.35 m 라, 14차 상시 IDENTITY_UNCERTAIN 의 주원인은
  POSSIBLE_DUPLICATE 보다 분할 나머지 억제가 매 시각 붙인 CROSS_VIEW_UNCERTAIN 쪽이다.
- 4대 클리크는 `_graph_linkage` 만이 아니라 `_assign_graph` 의 금지 행렬(EPIPOLAR 쌍 veto 가 병합 때 전파)도 막는다. 둘 다 고쳤다.
- "outlier_reproj_px 16 px" 는 이름·값이 틀렸다(삼각측량 이상치 20 px, 16 px 는 pairwise_gate_max_px).
- 첫 시각 unreachable 등록은 옛 주인이 이번 시각 갱신되지 않은(stale) 경우에만 생긴다. 주인이 갱신된 그림자는
  `holder_updated_now` 로 그 전에 빠지므로 "예각 0.55 m 그림자가 태어나는 순간 발동" 은 과장이다(제거는 했다).
- 14차 `reid.reentry_ambiguous` 5380·`reconnect_skip.holder_updated_now` 6887 의 대부분은 재입장 실패가 아니라, 분할 나머지
  클러스터가 억제되기 전에 재연결·Re-ID 경로를 지나며 센 것이다(창별 수가 `split_remainder_suppressed` 와 거의 같다). 이제 그림자
  판정이 먼저라 이 수는 크게 줄어야 하고, 남는 값이 진짜 재입장 애매다.
- `TENTATIVE` 발급을 CONFIRMED 로 미루는 안은 하지 않았다(아래 버린 대안).

## 버린 대안

- ID 발급을 CONFIRMED 시점으로: TENTATIVE 도 world_state 에 world_id 로 나가므로 미루면 계약 의미(TENTATIVE 의 world_id)가
  바뀌거나 TENTATIVE 를 내보내지 않아야 한다(Lead 결정 1: I1 불변). 순번 낭비는 계측(임시소멸)으로만 본다.
- 거리만의 방향별 반경 억제: 첫 등장 근접 두 사람과 구별이 안 된다. 한 점 재투영을 함께 요구한다.
- 단일 카메라 1프레임 이중 박스 무시(Carbon): 안전장치(같은 카메라 별도 검출은 두 사람)와 충돌. TTL 로만 완화했다.
- `reconnect_crowd_radius_m` 1.5→0.9 m: 분신만 crowd 에서 빼고 반경은 두었다(진짜 두 사람 보호).
- 병합 충돌 이벤트를 VERIFIED RESOLVED 로: 관측 해소가 아닌데 VERIFIED 라 6.4 의미가 깨진다. resolution 종류를 더했다.

## 시험 (노트북, GPU 없음)

- `ai/tests/tracking/test_track_duplicate_guard.py`: 예각 2+2(주인 없음·불확실 포함) ID 1개·IDENTITY_UNCERTAIN 없음, 처음부터
  갈라진 두 ID 0.4 s 에 MERGE·aliases·Track 1개, 같은 카메라 동시 관측은 TTL 동안 병합 안 함, 근접 2인(0.45·0.5 m) 300 tick
  Track 2개·병합 0·억제 0, 늘 2대씩 따로 보인 2인 두 ID, 확정 지연 6회, 재입장 (a) 3.5 s 가림 같은 ID (b) 20 s 이탈·분신 남음
  원래 ID(분신 제외를 끄면 실패함을 확인) (c) 외형 같은 두 사람 재입장 새 ID, 키 표본은 기립만(앉기·누움 제외), 외형 같고 키
  다른 두 사람(약 1.62/1.80 m) 맞는 ID, 키 비슷하면 새 ID, 기립 표본 없으면 키 미사용, 계측 줄.
- `ai/tests/tracking/test_cross_view_four_view_soft.py`: cam2/cam4 한 쌍만 EPIPOLAR 기각이어도 4대 한 클러스터(기능을 끄면 2+2),
  4대 엉덩이 한 점 재투영이 상한을 넘으면 묶지 않음.
- `ai/tests/risk/test_r4_duplicate_guard.py`: 낙상 도중 태어난 분신 R4 1건, 카메라 공유 두 사람 동시 낙상 2건, 반경 밖 2건,
  2대 관측 기립(골반 0.95 m) 10 s 안 해소, 머리만·앉은 골반이면 해소 안 함, 병합으로 사라진 ID 상실 발생 없음.
- `tests/risk/test_lifecycle.py`·`test_world_track_reconnect.py`·`services/tier0/test_tier0_judge.py`: 충돌 흡수 마감으로 기대값 갱신.
  `tests/eventlog/test_outbox.py`: 흡수 마감이 2.3.0 resolution 으로 계약 통과, 2.2.0 이면 위반.
- 기준선 시험 2개(`test_split_person_two_ids_are_flagged_while_close_not_merged`, `test_split_person_rejoined_merges_into_first_id`)는
  새 기능을 끈 설정으로 옛 동작을 계속 고정한다.

## GPU 14차 녹화 재생 검증 (live-20261112-135633)

절차: 같은 보정(cal-lobby-init-c85f2a97, cfg-66)으로 Tier 0 를 녹화 재생(REPLAY 입력, 알림 채널 없음)으로 33분 돌리고, 상태 파일의
`tracking_diagnostics` 와 30초 줄, 저널(R4 전이)을 모은다. 비교 기준선은 같은 녹화를 `shadow_suppression_enabled=False,
active_merge_enabled=False, four_view_soft_pair_enabled=False` 로 한 번 더 돌린 결과다(GPU 사용은 한 번의 큐로 묶는다).

통과 기준과 판독:

| 기준 | 판독 |
|---|---|
| 3명 구간 동시 확정 ≤ 3 이 99% tick 이상 | 30초 줄 `동시확정최대` 가 3명 구간 창에서 ≤ 3. 창 최대가 4 인 창이 있으면 상태 파일 state_ms 로 4명 이상 지속 시간 < 1 s 확인 |
| 30분 확정 ID ≤ 6 | 창별 `확정승격` 합 ≤ 6 (`새 ID` 는 TENTATIVE 포함이라 참고만) |
| 낙상 1회당 R4 1건, 기립 후 10 s 안 해소 | 저널 R4 RAISED 가 낙상 시각마다 1건, `IDENTITY_MERGED` 흡수 마감은 남는 이벤트 쪽으로만. RESOLVED − 기립 시각 ≤ 10 s |
| 근접 2인 오병합 0 | 2명 근접 교차 구간 창의 `활성병합` 0, 교차 전후 확정 ID 유지 |
| CONFIRMED 소실 뒤 같은 사람 ID 되찾기 ≥ 80%(애매한 경우 제외) | `소실/되찾음 N/기하 g+ReID r` 합계로 (g+r)/N, 분모에서 `reid.reentry_ambiguous` 로 새 ID 가 된 진짜 애매 건과 녹화 밖으로 나간 사람은 뺀다 |
| 그림자 억제가 실제로 일함 | `그림자억제` > 0 이고 `split_remainder_suppressed`·`holder_updated_now` 가 기준선보다 크게 줄었는지. `중복쌍최대` 가 3명 구간에서 0~1 |
| 키 보조 | `키거부`·`키동률깨기` 횟수와 그 재입장이 맞는 ID 였는지(저널·화면 녹화로 표본 확인) |

## 잔여 위험

- 4대 묶음 허용 뒤 `_validate` 가 이상치 카메라를 떼면(VIEW_SPLIT) 남은 클러스터가 IDENTITY_UNCERTAIN 이 될 수 있다(14차에선
  분할 나머지 억제 표시로 어차피 불확실이었다). 재생에서 VIEW_SPLIT 수를 본다.
- 네 카메라가 한 축으로 늘어선 퇴화 배치에서는 그 축 위 두 사람을 한 점 재투영으로 가를 수 없다. 이때는 같은 카메라 동시 관측
  이력(TTL)만이 보호한다. 로비는 네 모서리 배치라 해당 없다고 본다.
- 동시 활성 병합 직후 남는 ID 는 LATE_REASSOCIATION 으로 약 1.3 s IDENTITY_UNCERTAIN 이다(기존 병합 규칙).
- 계약 2.3.0: 이전 EC2 는 병합 흡수 전이를 거부해 그 기록에서 저널 전송이 멈춘다. **EC2 먼저 배포**. 관제 화면이 resolution 을
  운영자 종료로만 읽으면 흡수 카드를 '운영자 종료'로 보일 수 있다(후속). TS 생성 모델(`front/packages/contracts`)은 대상
  메시지가 아니라 바뀌지 않는 것으로 보았으나 이 worktree 에 node_modules 가 없어 `check:generated` 를 돌리지 못했다(CI 확인).
- 키 가정: head_z 는 귀·눈 keypoint 로 만들어 안전모 유무 영향이 작다고 본다(모자 위가 아니라 귀·눈 높이 + 고정 오프셋). 14차
  녹화의 안전모 착용 구간에서 같은 사람 키 표본 중앙값 변화가 허용치(7 cm) 안인지 재생으로 확인한다. 발목이 가려지면 표본이 없다.
- 그림자 판정은 매 시각 후보 클러스터마다 공분산·한 점 삼각측량을 더 한다(3명 기준 쌍 수 작음). 재생에서 judge p95 를 본다.

## 넘긴 일

- PPE·아바타의 병합 승계(aliases), 프론트 인원수(CONFIRMED+OCCLUDED 만), 관제 화면의 IDENTITY_MERGED 표시: 후속 티켓.
- 명세 5.2·6.4·19.1 갱신(위 "명세와 다른 점").
