# S15P31A101-663 판정 중심 계산 줄이기(동작 불변)

Tier 0 `judge_tick` 이 사람 수에 따라 초선형으로 늘어난다(11-08 H100 녹화 재생: 0명 p95 0.3 ms, 3명 97.4 ms(최대 262: Cross-view
104·World Track 170), 4명 233, 5명 333, 6명 422 ms, `docs/design/tier0-gpu-priority.md` "아바타 게이트 p95 지속 초과"). 외부 리뷰
(Carbon, `.agy/carbon-review.md` 2절 1·2·3·5, 3절 1·3, 커밋 안 함)는 원인을 판정 중심의 Python 반복으로 봤다. 이 티켓은 판정 규칙·
임계값·순서를 그대로 두고 같은 계산을 덜 하게만 바꾼다. **결과는 이전 구현과 바이트 단위로 같다**(아래 "동치 보장").
곁가지 분리·자원 격리는 S15P31A101-662 가 한다.

## 관련 명세

- 5.2 추적 3계층: Cross-view 계산 순서(후보 근거 → 제약 그래프 배정 → 클러스터 검증), World Track 연결·ID 관리, 관절 유효성
  게이트, Re-ID 보조(기하 게이트 뒤 보조 비용)
- 6.6 지연 예산(capture_to_risk p95 600 ms), 3.2(Tier 0 가 위치·RiskEvent 의 정본)
- AGENTS 규칙 1(Tier 0 정본)·2(모르면 비운다)·7(관측/추정): 값·null 규칙·`joint_source` 는 건드리지 않는다

## 측정 도구

| 파일 | 내용 |
|---|---|
| `ai/tests/services/tier0/judge_core_scenes.py` | 합성 장면과 실행기. 실제 `Perception`(Cross-view → World Track → 접지 → world_state)과 `JudgmentLoop`(구성 묶음·Risk Engine·LifecycleTracker·저널)를 그대로 돌리고 검출·로컬 추적·Re-ID 모델만 결정적 입력으로 바꾼다. 방·카메라·관절 오프셋은 `bench_tier0_geometry.py` 와 같다(4대, 광각 왜곡, σ 1 px, 신뢰도 기준 미만 10%, 검출 없음 3%) |
| `ai/tests/services/tier0/bench_judge_core.py` | 인원수별(기본 1·3·5·7·9명, 100 tick, 앞 10 tick 데우기) 단계 시간 p50·p95·평균과 `--cprofile N` 상위 함수 |

- 위치: 저장소의 기존 벤치(`tests/geometry/bench_tier0_geometry.py`, `tests/services/tier0/bench_people_latency.py`)가 `tests/` 에
  있고 파일 이름이 `test_` 로 시작하지 않아 수집되지 않는다. 같은 장면을 동치 시험이 함께 쓰므로 같은 곳에 뒀다.
- 단계 이름은 Tier 0 `StageTiming` 과 같다(`judge_tick`, `perception`, `cross_view_2d_triangulation` 과 그 안의
  `cross_view_pairs`·`cross_view_clustering`·`cross_view_validate_triangulate`, `world_track`, `contact`, `judgment_and_output`,
  `risk_evaluate`). 4명 이상이면 ROI 거르기(-574)가 켜진다(구역 1개).
- 장면: `crowd(N)` 은 무작위 경유점 보행, 세 명마다 앞 사람 0.45 m 옆을 같이 걷는 근접 쌍, 사람·카메라마다 50% 확률의 5~20 tick
  가림 창, 첫 사람은 R1 구역을 지난다. `mixed()` 는 아래 동치 시험 장면이다. Re-ID 특징은 사람마다 고정 기준 벡터 + 잡음(3 tick 에
  한 번, 1 s 안 최신)이라 갤러리·스왑 검사·재입장 경로가 돈다.
- SIM 합성 입력의 처리 시간이다. 정확도 지표가 아니다. 운영 판정은 GPU 세션 녹화 재생으로 한다(아래).

이전 코드와 비교: `git archive 540936b4 ai/src ai/services` 를 꺼내 `PYTHONPATH=<꺼낸 곳>/ai/src;<꺼낸 곳>/ai` 로 같은 스크립트를
돌린다(`judge_core_scenes.py` 는 `ai/` 를 sys.path 뒤에 붙여 앞에 둔 이전 `services` 를 가리지 않는다).

## 기준선 핫스팟 (dev 540936b4, 9명, cProfile, tick 당 ms, 프로파일러 오버헤드로 실제보다 약 2배)

| 누적 | 호출/60 tick | 함수 | 원인 |
|---:|---:|---|---|
| 292.2 | 60 | `JudgmentLoop.tick` | |
| 194.4 | 60 | `CrossViewAssociator.associate` | |
| 141.5 | 60 | `_assign_graph` | 병합 반복마다 `_preferred_partition_pairs` 를 다시 돈다 |
| 118.2 | 1481 | `_preferred_partition_pairs` | 같은 부분집합의 `_graph_linkage` 를 반복마다 다시, 허용 간선을 쌍마다 numpy 행 연산(`.any()` 13만 회) |
| 117.6 | 20894 | `_graph_linkage` | 같은 `(a, b)` 를 tick 안에서 여러 번 |
| 103.7 | 96169 | `_pair_3d_distance` | 같은 두 쌍의 3D 어긋남을 반복 계산, 호출마다 `np.median`(56.1 ms) |
| 52.0 | 60 | `WorldTrackManager.update` | 그중 갤러리 유사도 22.9, 관절 게이트 18.6 |
| 22.9 | 12518 | `_cluster_gallery_sim` | 배정 비용·스왑 검사·갤러리 갱신이 같은 (Track, 클러스터) 를 여러 번 |
| 21.4 | 6974 | `pair_residual_thresholds` | 쌍 판정마다 카메라 쌍 잔차 분위수(`np.quantile`)를 다시 |
| 17.5 | 60 | `roi_filter.filter_views` | view 마다 1점 왜곡 제거(범위 밖, 아래 "하지 않은 것") |
| 17.0 | 60 | `ActiveConfig.evaluate`(Risk Engine) | 구역 거리 계산(범위 밖) |
| 3.0 | 60 | `WorldState.from_payload` | 위치 문구(f-string)를 값마다 만든다 |
| 0.45 | 531 | `world_track._others` | 리뷰가 짚은 곳이지만 핫스팟이 아니다 |

리뷰가 짚은 `_others` 재조립(9명 tick 당 0.45 ms, 프로파일 기준)과 관절 게이트의 σ·경쟁 후보(18.6 ms 중 경쟁 후보 7.4, σ 4.6)는
Cross-view 배정(141.5 ms)보다 훨씬 작아 순서를 바꿨다: 배정 → 갤러리 → 역파싱 → isfinite. `_pair_from_precomputed` 는 지금 코드에
없다(리뷰가 다른 판을 본 것으로 보인다).

## 바꾼 것

| 위치 | 이전 | 지금 | 같은 결과인 이유 |
|---|---|---|---|
| `cross_view.py` `_graph_linkage` | 호출마다 계산 | `_assign_graph` 한 번 동안 `(tuple(a), tuple(b))` 키로 결과를 다시 쓴다(`_AssignMemo.linkage`) | 그 호출 동안 입력(pairs·pair_joints·prior_pairs·연결 이력·이번 시각 view·설정)이 바뀌지 않는 순수 계산. 키는 순서까지 그대로라 a/b 나눔·합집합 순서가 다른 호출은 따로 계산한다 |
| 같은 곳, 3D 어긋남 검사 | 쌍 조합마다 `_pair_3d_distance` | 같은 호출 동안 두 쌍 키(정렬)로 다시 쓴다(`_AssignMemo.distance`) | 함수가 대칭(뺄셈 부호만 바뀌고 노름·중앙값 같음). 대칭은 비트 단위 시험 5000건(`test_pair_3d_distance_is_symmetric_bitwise`) |
| 같은 곳, 4대 엉덩이 검사 | 호출마다 재삼각측량 | 같은 키 순서로 다시 쓴다(`_AssignMemo.hips`) | 이번 시각 view 로만 정해진다 |
| `_preferred_partition_pairs` 허용 간선 | 후보 쌍마다 `(member & member[i]).any(axis=1)`·`near` 행 계산 | (후보 쌍, 클러스터) 행렬로 한 번에. 카메라 공유 행렬 `shares` 는 한 번 | 원소별 뺄셈·비교라 쌍 하나씩과 같은 값. 죽은 클러스터는 카메라 소속이 비어 있어 영향이 없다. 간선 순서는 `np.nonzero` 행 우선 = 이전 이중 반복 순서 |
| `_pairs`·`_verdict` 쌍 문턱 | 쌍 판정마다 `pair_residual_thresholds` | `_pairs` 한 번 동안 (카메라 a, 카메라 b) 로 다시 쓴다 | 잔차 표본은 `_pairs` 뒤 `_update_pair_residuals` 에서만 바뀐다 |
| `_pair_3d_distance` 중앙값 | `float(np.median(dists))` | `_median_1d`: 정렬 뒤 가운데 하나 또는 두 값의 합 / 2, NaN 이 있으면 NaN | np.median 의 정의 그대로(짝수는 `mean` = 합 / 2). 무작위 20만 건(NaN·inf·같은 값·1~17개) 비트 단위로 같음 |
| `world_track.py` `_cluster_gallery_sim` | 호출마다 갤러리 유사도 | tick 안에서 (World ID, 클러스터 키) → (갤러리 객체, version, 결과). 같은 객체·같은 version 이면 다시 쓴다. tick 특징을 읽을 때 비운다 | 결과는 갤러리 항목과 그 tick 특징(update 첫머리에 한 번 읽음)으로만 정해진다. `TrackGallery.version` 은 `add_observation` 호출마다(거절돼도) 는다. 병합(`merge_tracks`)은 남는 갤러리에 `add_observation` 을 부르고, 지운 갤러리는 `get` 이 다른 객체·None 을 준다 |
| `services/tier0/judge.py` + `risk/world.py` | 매 tick `WorldState.from_payload` | `WorldState.from_payload_fast`: 같은 검사·같은 변환을 위치 문구 없이. 하나라도 걸리면 `from_payload` 를 그대로 불러 같은 예외(같은 문구)를 낸다 | 검사 함수를 하나씩 옮겼다(`as_number` 의 bool·OverflowError·유한성, `as_timestamp_ms` int64, 열거형, 필수 키, 길이). 교차 필드 규칙은 같은 생성자. 계약 예시 6개·정상 변형 3가지·거절 변형 34가지에서 필드·타입·예외 문구가 같다(`tests/risk/test_world_fast_parse.py`). risk 는 표준 라이브러리만 쓴다(그대로) |
| `ultralytics_local.py` | 관절마다 `np.isfinite([u, v, c]).all()` | 사람마다 `np.isfinite(rows).all(axis=1)` 한 번, `tolist()` | 같은 판정. `tolist` 는 `float()` 와 같은 값. float16·32·64 + NaN·inf 900건 비트 단위로 같음. 판정 tick 밖(검출 스레드)이지만 같은 GIL 을 쓴다 |

새 설정값·임계값은 없다. 계약(`contracts/`)은 바꾸지 않았다.

## 동치 보장

1. **이전 커밋과 trace 비교(바이트 단위)**: `test_judge_core_equivalence.py::test_trace_matches_base_commit` 은
   `SIMAI_JUDGE_EQUIV_BASE=540936b4` 일 때 그 커밋의 `ai/src`·`ai/services` 를 꺼내 별도 프로세스로 같은 장면을 돌리고(쓴 모듈 경로를
   확인) 결과 JSON 을 바이트로 비교한다. 장면: `mixed` 300 tick + `crowd` 3·5·7·9명 × 200 tick = 1100 tick. **같았다**(193 s).
   이후 티켓이 동작을 바꾸면 깨지므로 기본은 건너뛴다.
2. **저장소 안 기준 경로와 비교(항상 돈다)**: 같은 시험 파일의 `test_mixed_scene_matches_reference`(300 tick)·
   `test_crowd_matches_reference`(7명 120 tick). 기준 경로는 바꾸기 전 함수의 고정 사본(`legacy_663_judge_core.py`: 이전
   `_preferred_partition_pairs`·`_cluster_gallery_sim`·`np.median`)과 메모를 끈 계산 함수(`_assign_graph_compute`·`_pairs_compute`·
   `_graph_linkage_compute`·`WorldState.from_payload`)다.
3. **trace 내용**: tick 마다 world_state payload 전체(World ID·observation_state·track_lifecycle·identity_confidence·
   identity_revision·위치·σ·골반·접지·지지 표면·head_z·관절·cameras·aliases), Cross-view 클러스터(association_id·키·불확실 사유·경쟁
   후보)와 쌍 근거 전체(탈락 사유·비용·경계·잔차), Track 별 Re-ID 내부 판단(갤러리 유사도·margin·스왑 의심 기한), ID 교정(병합·분할),
   RiskEvent 전이 payload(event_id·sequence·상태·근거), 현장 경보 신호, 끝의 World Track 진단 계수 전체·발급 ID. msg_id·event_id 는
   고정 순번·고정 난수다. 부동소수는 `repr` 왕복 문자열이라 **허용 오차 0**(비트 단위)이다.
4. **장면이 지나는 상황**(`mixed`, 시험이 확인): 근접 2인(0.4 m 나란히 구역 횡단), 가림(카메라 2대 번갈아·잠깐 3대), 2+2 분할 그림자
   (cam3·cam4 위치 0.7 m 어긋남 → ID 하나 더), 병합(revision MERGE)·분할(겹친 뒤 갈라짐), 재입장(70 tick 이탈, 로컬 ID 세대 바뀜,
   `reid.reentry_reconnected`), 낙상(R4 RAISED), 구역 진입(R1·R5 RAISED), IDENTITY_UNCERTAIN 부착.
5. **음성 대조**(`test_negative_controls_change_trace`): 갤러리 메모가 version 을 보지 않으면(같은 tick 에 갱신된 갤러리의 옛 유사도를
   다시 쓰면), 병합 비용 메모 키에서 b 를 빼면 trace 가 달라진다. (`_median_1d` 를 평균으로 바꾸는 것은 이 장면들의 3D 어긋남 문턱
   판정을 바꾸지 않아 trace 로는 안 잡히므로 비트 단위 단위 시험으로 본다.)
6. **성질 시험**: 중앙값 비트 동일, 3D 어긋남 대칭, 메모가 `_assign_graph`·`_pairs` 한 번 안에만 있음(`test_cross_view_memo.py`),
   갤러리 version(`tests/reid/test_gallery_version.py`), isfinite(`test_ultralytics_local.py::test_pose_result_matches_per_joint_isfinite`),
   빠른 역파싱(`tests/risk/test_world_fast_parse.py`).

## 전후 측정 (노트북 CPU)

`uv run python tests/services/tier0/bench_judge_core.py --json <파일>`(ai/). 1·3·5·7·9명 × 100 tick(앞 10 tick 데우기 제외).
이전은 540936b4 의 `ai/src`·`ai/services` 를 PYTHONPATH 앞에 두고 같은 스크립트로 쟀다. 이전 → 지금 → 이전 → 지금 순서로 두 번씩 돌려
값마다 두 번의 중앙값(=평균)을 적었다. 환경: Intel Core Ultra(Family 6 Model 170), 22 논리 코어, Windows 11, Python 3.11.15,
numpy 2.4.6, 단일 스레드. 같은 노트북에서 반복하면 ±20% 흔들린다(9명 judge_tick p95 이전 183.8·189.6, 지금 81.9·98.6).

| 사람 | 단계 | 이전 p50 | 이전 p95 | 지금 p50 | 지금 p95 | p50 비율 | p95 비율 |
|---:|---|---:|---:|---:|---:|---:|---:|
| 1 | **judge_tick** | 11.7 | 14.2 | 9.9 | 12.1 | 0.85 | 0.86 |
| 1 | cross_view_2d_triangulation | 8.8 | 10.9 | 7.5 | 8.9 | 0.85 | 0.82 |
| 1 | cross_view_clustering | 1.9 | 2.5 | 0.7 | 0.9 | 0.38 | 0.34 |
| 1 | cross_view_pairs | 3.5 | 4.9 | 3.4 | 4.4 | 0.98 | 0.90 |
| 1 | world_track | 1.5 | 1.9 | 1.4 | 1.7 | 0.93 | 0.93 |
| 1 | risk_evaluate | 0.4 | 2.2 | 0.4 | 2.2 | 1.00 | 1.02 |
| 3 | **judge_tick** | 30.1 | 37.2 | 20.4 | 26.9 | 0.68 | 0.72 |
| 3 | cross_view_2d_triangulation | 21.9 | 28.1 | 12.9 | 16.8 | 0.59 | 0.60 |
| 3 | cross_view_clustering | 10.1 | 14.6 | 2.5 | 5.4 | 0.24 | 0.37 |
| 3 | cross_view_pairs | 7.2 | 9.0 | 5.2 | 6.8 | 0.72 | 0.75 |
| 3 | world_track | 4.7 | 6.3 | 4.3 | 6.2 | 0.93 | 0.99 |
| 3 | risk_evaluate | 1.3 | 4.7 | 1.1 | 5.2 | 0.82 | 1.11 |
| 5 | **judge_tick** | 65.1 | 81.1 | 40.8 | 54.0 | 0.63 | 0.67 |
| 5 | cross_view_2d_triangulation | 44.7 | 59.3 | 20.3 | 27.6 | 0.45 | 0.47 |
| 5 | cross_view_clustering | 26.7 | 36.5 | 5.2 | 10.4 | 0.20 | 0.28 |
| 5 | cross_view_pairs | 12.0 | 16.6 | 8.0 | 10.5 | 0.67 | 0.63 |
| 5 | world_track | 8.1 | 10.6 | 7.5 | 10.7 | 0.92 | 1.00 |
| 5 | risk_evaluate | 4.1 | 7.8 | 4.4 | 8.3 | 1.06 | 1.07 |
| 7 | **judge_tick** | 98.9 | 118.6 | 59.2 | 74.6 | 0.60 | 0.63 |
| 7 | cross_view_2d_triangulation | 66.6 | 82.5 | 27.9 | 37.3 | 0.42 | 0.45 |
| 7 | cross_view_clustering | 42.9 | 55.1 | 8.4 | 13.9 | 0.20 | 0.25 |
| 7 | cross_view_pairs | 16.2 | 20.7 | 10.3 | 14.0 | 0.64 | 0.67 |
| 7 | world_track | 16.5 | 20.4 | 14.2 | 19.4 | 0.86 | 0.95 |
| 7 | risk_evaluate | 4.1 | 7.8 | 4.7 | 8.9 | 1.15 | 1.14 |
| 9 | **judge_tick** | 144.4 | 186.7 | 71.8 | 90.3 | 0.50 | 0.48 |
| 9 | cross_view_2d_triangulation | 101.4 | 138.0 | 33.8 | 45.6 | 0.33 | 0.33 |
| 9 | cross_view_clustering | 70.8 | 96.2 | 11.7 | 19.2 | 0.17 | 0.20 |
| 9 | cross_view_pairs | 22.2 | 33.9 | 12.3 | 16.0 | 0.55 | 0.47 |
| 9 | world_track | 21.4 | 27.2 | 16.3 | 22.0 | 0.76 | 0.81 |
| 9 | risk_evaluate | 6.2 | 11.5 | 6.3 | 11.2 | 1.02 | 0.98 |

바꾼 것별 몫(9명):

| 바꾼 것 | 근거 측정 | 효과 |
|---|---|---|
| 배정 메모(`_graph_linkage`·3D 어긋남·엉덩이) + 허용 간선 행렬 | `cross_view_clustering` 위 표 | p50 70.8 → 11.7 ms(7명 42.9 → 8.4, 5명 26.7 → 5.2). 프로파일 `_assign_graph` 누적 141.5 → 21 ms, 그중 `_preferred_partition_pairs` 118.2 → 14.6(메모만) → 10.6(행렬까지) |
| 쌍 문턱 재사용 | `cross_view_pairs` 위 표 | p50 22.2 → 12.3 ms(`pair_residual_thresholds` 6974회 → 카메라 쌍 6회/tick). 사람이 적으면 표본이 모자라 기본값을 돌려주므로 1명은 그대로 |
| 갤러리 유사도 tick 재사용 | `world_track` 위 표, 프로파일 | p50 21.4 → 16.3 ms. 프로파일 `_cluster_gallery_sim` 누적 21.7 → 9.7 ms(`compute_similarity` 14981 → 6672회) |
| `_median_1d` | 프로파일 | `_pair_3d_distance` 누적 8.1 → 4.0 ms(메모 뒤 남은 7577회 기준) |
| `from_payload_fast` | 9명(트랙 10개) payload 한 번 단독 측정 | 0.79 → 0.52 ms(프로파일 3.2 → 2.5) |
| isfinite 묶음 | 9명 한 프레임 단독 측정 | 관절 변환만 0.39 ms(이전) → 함수 전체 0.28 ms(지금). 검출 스레드 몫이라 위 표에는 안 나온다 |

- 9명 judge_tick 은 p50 144 → 72 ms, p95 187 → 90 ms 로 절반이 됐다. 줄어든 몫은 사람 수가 많을수록 크다(1명 0.85, 3명 0.68~0.72,
  5명 0.63~0.67, 7명 0.60~0.63, 9명 0.48~0.50). 1명은 배정이 원래 작아 거의 그대로다.
- 남은 시간(9명 p50 72 ms): Cross-view 34(후보 근거 12, 배정 12, 왜곡 제거·클러스터 검증 약 10), World Track 16(프로파일 비율로
  관절 게이트 약 8, 갤러리 약 5), Risk Engine 6, ROI 거르기 약 7(프로파일 비율), 나머지. 아래 "하지 않은 것" 이 다음 후보다.

## 하지 않은 것·버린 대안

| 대안 | 이유 |
|---|---|
| Re-ID 유사도를 갤러리 행렬 × 질의 한 번(행렬곱) | 이 노트북에서 `np.dot(M[i], q)` 와 `(M @ q)[i]` 가 float32 128~2048차원 무작위 200회 모두 비트가 달랐다(sdot 과 sgemv 의 합 순서). 유사도는 스왑·재입장 문턱 비교에 쓰이므로 동작이 바뀔 수 있다. tick 안 재사용(version)으로 대신했다 |
| `_others` 를 프레임당 한 번 | 9명 tick 당 0.45 ms(프로파일). 이득이 측정 잡음보다 작아 코드만 늘어난다 |
| 관절 게이트를 Track 전체 묶음으로(σ·경쟁 후보) | 9명 프로파일 16~18 ms(실제 약 절반). World Track 이 Track 마다 `JointGate.update` 를 부르는 구조를 바꿔야 하고, 묶음 einsum·행렬곱은 행 수에 따라 연산 경로가 달라 비트 동일을 보장하지 못한다. 동작 불변 범위 밖 후보 |
| 제약 그래프 성분 분할 결과 재사용(정점·키·간선·비용 비트가 같은 성분) | 구현해 A/B 했으나 이득 없음(mixed·crowd-9·밀집 6·9명, 분기 한정 탐색이 60 tick 에 14~84회로 드묾). 되돌렸다 |
| ROI 거르기(`roi_filter`)를 카메라마다 한 번에 왜곡 제거 | 9명 프로파일 약 17 ms. 정규화가 `(N, 3) @ K⁻¹ᵀ` 라 N=1 과 N>1 이 BLAS gemv/gemm 다른 경로를 탈 수 있어 비트 동일을 보장하지 못한다. 범위(판정 중심) 밖 후보 |
| Risk Engine 구역 거리(`zone_geometry`) | 9명 프로파일 약 15 ms. risk 패키지는 EC2 컨테이너와 공통 시험 벡터를 함께 쓰므로 별도 티켓 후보 |
| 계산량 상한(성분 크기 상한 12 → 8, 혼잡 때 Re-ID 표본 주기 늘리기, 리뷰 3절 5) | 동작이 바뀐다(이번 범위 아님). 운영 측정 뒤 별도 판단 |
| 2-view DLT SVD → eigh | -237 에서 버린 이유(해 유일성 판정 정밀도)와 같다 |

## 남은 위험

- 측정은 노트북 CPU(같은 노트북에서 다른 작업과 함께, 반복 측정 ±20%)다. H100 노드 CPU 에서의 절대값은 GPU 세션에서 다시 잰다.
- 이득은 Cross-view 배정에 몰려 있다. 실제 녹화에서 배정 비중이 합성 장면보다 작으면 줄어드는 몫도 작다(11-08 3명 최대값 262 ms 는
  World Track 170 ms 쪽이 컸다: Re-ID·그림자 검사가 실제 데이터에서 더 무거울 수 있다).
- 메모는 "그 호출 동안 입력이 바뀌지 않는다"에 기댄다. 이후 `_graph_linkage`·`_cluster_gallery_sim` 이 새 상태를 읽게 바꾸면 키나
  무효화 조건도 같이 바꿔야 한다(코드 주석과 음성 대조 시험이 지킨다).
- 비트 동일 비교는 같은 numpy·BLAS 에서 이전/지금을 비교한 것이다. 다른 CPU 에서도 이전/지금이 서로 같다는 근거는 바꾼 계산이 같은 연산을
  같은 순서로 하기 때문이다(원소별 연산·정렬·같은 함수 재호출).

## GPU 세션 확인 항목 (같은 커밋)

1. 14차 녹화 재생(file 입력, `speed` 실시간)에서 30초 StageTiming 로그·`tier0.live` 표시 파일의 인원수별 `judge_tick` p95·최대,
   `cross_view_2d_triangulation`·`world_track`·`risk_evaluate` p95. 11-08 표(3명 p95 97.4, 4명 233, 5명 333, 6명 422 ms)와 비교한다.
2. 같은 녹화를 이 티켓 직전 커밋(540936b4)과 이 커밋으로 각각 돌려 같은 Pod 에서 A/B 한다(-662 스위치는 끈 채로 같게).
3. 결과 동일성: 두 실행의 risk_event JSONL(file 입력 사본)과 world_state 저장분이 같은지(재생 시계가 실시간이면 tick 경계가 달라질 수
   있으므로 `speed=max` 결정론 재생으로 비교).
4. 아바타 게이트(`AVATAR_JUDGE_BUDGET_MS` 100 ms) 통과 시간 비율.
