# Re-ID: COPE 외형·얼굴 보조 특징 추출 모듈 및 Tier 0 추적 보조·DORMANT 재입장 연결 (S15P31A101-463)

고정 카메라 4대 교실·로비 환경에서 기하 오차(4대 보정 잔차로 인한 2+2 클러스터 분할), 근접 교차 후 ID 뒤바뀜(swap), 그리고 가림·이탈 뒤 재진입으로 발생하는
World ID 단절을 완화하기 위해, **기하 게이트 뒤 보조 신호**로 쓸 전신 외형(COPE, CVPR 2026) 및 선택적 얼굴 특징 추출 모듈
(`simai_ai.reid`)을 구현하고 Tier 0 World Track 생명주기(`simai_ai.tracking.world_track`, `services.tier0`)에 연결했다.

- **1단계**: COPE 전신 외형·선택적 얼굴 특징 추출 모듈, 신체 부위 가시성(`PartVisibility`), 다중 시점·방향 슬롯 갤러리(`ReidGalleryStore`) 구현 및 10차·11차 녹화 오프라인 분리도·GPU 지연 검증.
- **2단계**: `-464`(재투영 클러스터링·분할 억제·늦은 병합) 기하 정본 위에 비동기 특징 수집기(`AsyncReidCollector`), 기하 경계 분할 판정(Split adjudication), 근접 교차 후 Swap 복원, `DORMANT`(30분) 재입장 ID 이어받기, 아바타 외형 갱신 보류(`avatar_update_hold`)를 연결하고 10/29 녹화 회귀 비교로 효과를 검증했다.

## 관련 명세 및 사용자 결정

| 절 / 근거 | 내용 | 이 문서에서 |
|---|---|---|
| AGENTS.md 규칙 1·8 | Tier 0가 위치·ID 정본이며, Re-ID는 전신 외형 + 선택적 얼굴 보조 특징을 기하 게이트 뒤 분할 판정·교차 swap 복원·`DORMANT` 30분 재입장 보조 비용으로만 쓰고 Tier 0 위치·ID 정본을 덮지 않는다. 생성형 얼굴 복원 금지 | 기하 게이트 밖 쌍은 외형 유사도가 높아도 절대 합치지 않으며, 가중치·GPU 부재 시 자동으로 기하 단독 추적(`disabled`)으로 폴백한다 |
| 명세 5.2 추적 3계층 | 2층 Cross-view 연결과 3층 World Track 생명주기(`TENTATIVE` → `CONFIRMED` → `LOST` → `DORMANT` → `EXPIRED`)·늦은 재연결. 애매하면 잘못 합치지 않는다 | 분할 판정(`reid.split_suppressed_same`/`reid.split_preserved_different`), 근접 교차 후 swap 복원(`reid.swap_reverted`), `DORMANT`(30분) 재입장 재연결(`reid.reentry_reconnected`/`reid.reentry_merged`)을 구현했다 |
| 명세 11.5 | `dev_test` 녹화는 내부 개발·시험에만 쓰며 사람이 찍힌 프레임·crop 그림은 커밋·저장하지 않는다 | 오프라인 평가·캐시 생성(`ai/experiments/reid_eval/`) 및 실시간 수집(`AsyncReidCollector`) 모두 메모리 안에서만 crop을 잘라 임베딩으로 바꾸고 즉시 폐기하며, 디스크(`C:/Users/SSAFY/simai-local/`)에는 수치·벡터만 남긴다 |
| 명세 19.1 | G2 측정(10차·11차 세션) 뒤 단계적 Re-ID 도입(`-463`): 1단계 COPE 외형+선택 얼굴 모듈·오프라인 검증, 2단계 Tier 0 비동기 특징 수집·분할 판정·근접 교차 swap 복원·`DORMANT`(30분) 재입장 연결 | 1·2단계 구현과 10/29 회귀 비교(`C:/Users/SSAFY/simai-local/track-463/round1`)를 완결했다 |

## 1. 구현 범위 및 코드·시험 위치

| 구분 | 위치 | 역할 |
|---|---|---|
| 외형 임베더 | `ai/src/simai_ai/reid/embedder.py` | `Embedder` 추상 인터페이스, `COPEConfig`, `COPEEmbedder`(외부 저장소 `SIMAI_COPE_ROOT` + 가중치 `SIMAI_COPE_WEIGHTS` + CLIP `SIMAI_COPE_CLIP_PATH`, SHA-256 검증, 자동 다운로드 차단, GPU 우선·CPU 폴백, `256×128` 입력 정규화, 배치 추론, `1280-D` L2 정규화 출력 및 PSS 품질 점수) |
| 신체 부위 가시성 | `ai/src/simai_ai/reid/visibility.py` | `PartVisibility`(`visible_ratio` 포함), `PartWeights`, `estimate_part_visibility`(COCO 17관절 신뢰도·박스 경계 기반 `head`/`torso`/`legs` 가시성 판정), `co_visibility_confidence`(두 관측 간 공통 가시 부위 비중 계산) |
| 선택적 얼굴 백엔드 | `ai/src/simai_ai/reid/face.py` | `FaceConfig`, `FaceEmbedder`, `InsightFaceOnnxEmbedder`, `NoOpFaceEmbedder`, `estimate_face_region_from_pose`(최소 픽셀 크기 `min_face_size_px=40` 및 코·양눈 정면 랜드마크 2개 이상 문턱, 가중치 미존재 시 자동 다운로드 없이 건너뜀) |
| 트랙 갤러리 | `ai/src/simai_ai/reid/gallery.py` | `TrackGallery`, `ReidGalleryStore`, `(camera_id, orientation_bin)` 다중 시점·방향 슬롯 관리, 최대 용량·품질 기반 교체, `max`/`mean` 코사인 유사도 + 공가시성 가중 결합, `DORMANT` 보존(기본 TTL 30분 = `1,800,000 ms`) |
| 비동기 특징 수집·캐시 | `ai/src/simai_ai/reid/collector.py` | `LocalTrackFeature`, `LocalTrackFeatureCache`(O(1) 실시간 최신 조회 + 오프라인 재생용 시계열 이진 탐색 + `.npz` 직렬화), `ReidCollectorConfig`, `AsyncReidCollector`(Tier 0 10 Hz 루프를 막지 않는 비동기 워커 스레드, 트랙당 주기 `750 ms`·틱당 최대 `8`개 요청 제한, 큐 초과 시 조용히 드롭, 가중치 부재 시 `disabled` 폴백) |
| World Track Re-ID 통합 | `ai/src/simai_ai/tracking/world_track.py` | `WorldTrackConfig` Re-ID 문턱·플래그, `TrackFeatureLookup` 프로토콜, 다중 시점 갤러리 갱신 및 근접(`≤ 1.0 m`)/불확실 구간 갱신 동결(`reid.gallery_frozen`), 분할 판정(`reid.split_suppressed_same`/`reid.split_preserved_different`, `<n` 방지 가드), 근접 교차 후 Swap 복원(`reid.swap_reverted`), `DORMANT` 30분 재입장 즉시 재연결(`reid.reentry_reconnected`) 및 늦은 재입장 병합(`reid.reentry_merged`), `avatar_update_hold` 노출 |
| Tier 0 서비스·아바타 연동 | `ai/services/tier0/settings.py`, `perception.py`, `service.py`, `ai/src/simai_ai/hmr/appearance_stream.py`, `stream.py` | `ReidSettings`(`SIMAI_REID_*` 환경변수), `Tier0PerceptionPipeline` 내 `AsyncReidCollector` 구동·진단 지표(`status()["reid"]`) 노출, `avatar_update_hold` 시 아바타 외형 스트림의 `hold_appearance_update=True` 전달로 교차·불확실 구간 아바타 옷 색상 덮어쓰기 방지 |
| 오프라인 평가·회귀 재생 | `ai/experiments/reid_eval/`, `ai/experiments/track_replay/` | `build_replay_cache.py`(10/29 녹화 로컬 트랙 COPE 특징 `.npz` 캐시 생성), `replay_tracking.py`·`run_experiments.py`·`make_run_records.py`(`--reid-cache`, 재입장 ID 유지율 `reentry`, `ms_per_tick`, `plan_463.json` 4개 설정 비교 및 `run_record.json` 생성) |
| 단위 시험 | `ai/tests/reid/`, `ai/tests/tracking/test_world_track_reid.py` | `test_embedder.py`, `test_visibility.py`, `test_face.py`, `test_gallery.py`, `test_eval_metrics.py`, `test_collector.py`, `test_world_track_reid.py` (총 150개 관련 단위 시험 통과) |

## 2. 명세에 없어 정한 수치와 근거

| 항목 | 정한 값 | 근거 |
|---|---|---|
| COPE 입력 해상도·정규화 | `256×128` (H×W), `mean=[0.5, 0.5, 0.5]`, `std=[0.5, 0.5, 0.5]` | COPE MSMT17 학습 설정(`configs/MSMT17/cope.yml`)과 동일해야 위치 임베딩 보간(`16×8` 패치 그리드, `129` 토큰)이 학습 분포와 일치한다 |
| COPE 출력 차원 | `1280` (`768` ViT-B/16 시각 특징 + `512` CLIP 투영 특징, L2 정규화) | `cope_msmt17.pth`의 `bottleneck`(768)과 `bottleneck_proj`(512) 결합 벡터를 L2 정규화하면 내적만으로 코사인 유사도를 O(D)에 계산할 수 있다 |
| 부위별 가시성 가중치 (`PartWeights`) | `torso=0.50`, `legs=0.35`, `head=0.15`, 관절 신뢰도 문턱 `0.30` | 교실·로비 카메라 각도에서 상의(몸통) 면적이 가장 크고 안정적이며, 난간·책상에 가려지는 하체(다리)가 안 보일 때 신뢰도를 `0.65`로 낮춰 부분 가림 오매칭을 줄인다(실측에서 `co_visibility ≥ 0.65` AUC `0.9876` vs `< 0.65` AUC `0.8552`) |
| 얼굴 보조 게이트 (`FaceConfig`) | `min_face_size_px = 40.0`, 정면 관절(코·왼눈·오른눈) 최소 `2`개 (`conf ≥ 0.35`), 보조 가중치 `face_weight = 0.25` | 명세 규칙 8의 물리 한계(30~50 px 미만 저해상도 얼굴 식별 불가)와 뒷모습 오검출을 막기 위해 최소 변 40 px 이상·정면 랜드마크 2개 이상일 때만 보조 점수로 섞는다 |
| 갤러리 슬롯·용량 (`GalleryConfig`) | 트랙당 최대 `16`개, `(camera_id, orientation_bin)` 슬롯당 최대 `2`개, 방향 bin `8`개, `min_quality_score = 0.25` | 4대 카메라 × 방향 회전 표본을 골고루 유지하되 한 카메라·자세가 갤러리를 독점하지 않게 제한한다. 트랙당 메모리 점유는 `16 × 1280 × 4B ≈ 80 KB`로 무시할 수 있는 수준이다 |
| 갤러리 대표 유사도 방식 | **`max` 유사도** (슬롯 내 최고 코사인 유사도 × 공가시성 가중) | 10/29 녹화 다인 구간 실측에서 `max` 유사도 Top-1 정확도가 **`98.08%`**(중앙 마진 `0.2461`)로, 시점 차이로 희석되는 `mean` 유사도(**`81.73%`**, 중앙 마진 `0.0725`)보다 16.3%p 높다 |
| 비동기 수집 주기 및 큐 상한 (`ReidCollectorConfig`) | 트랙당 최소 간격 `min_interval_ms = 750`, 틱당 최대 요청 `max_requests_per_tick = 8`, 배치 `8`, 큐 상한 `32` | 노트북 RTX 4050 GPU에서 배치 8 추론이 약 `74 ms`이므로, 모든 트랙을 매 틱(100 ms) 추출하지 않고 `0.75 s` 주기·틱당 최대 8개로 제한해 Tier 0 10 Hz 루프를 전혀 블로킹하지 않는다 |
| 갤러리 갱신 동결 거리 (`reid_gallery_freeze_distance_m`) | 활성 트랙 간 거리 `≤ 1.0 m`, 또는 분할 억제·저신뢰·모호 배정 틱, 가시율 `< 0.50` | 두 사람이 `1.0 m` 이내로 근접 교차할 때 바운딩 박스가 겹쳐 상대방의 옷 색상이 내 갤러리 슬롯에 오염되는 현상을 원천 차단한다(10/29 재생에서 `3,600`회 동결 작동) |
| 분할 판정 문턱 (`reid_split_same_threshold` / `diff_threshold`) | 같은 사람 억제 `sim ≥ 0.58` (경계 반경 `split_suppression_radius_m × 1.35`까지 확장), 다른 사람 보존 `sim ≤ 0.42` (단, 두 사람 모두 최근 `≤ 600 ms` 다중 카메라 검증 이력이 있을 때만 보존) | Cross-camera 같은 사람 중앙값(`0.7731`)과 다른 사람 중앙값(`0.3155`) 사이 경계에서 기하 보정 잔차로 벌어진 단일 인물 조각(`1.05~1.42 m`)은 `sim ≥ 0.58`일 때 흡수하고, 반대로 기하 반경 안에 들어왔더라도 두 검증된 인물이 `sim ≤ 0.42`로 뚜렷이 다르면 합치지 않아 인원 과소추정(`<n`)을 막는다 |
| 근접 교차 후 Swap 복원 (`reid_swap_min_similarity` / `min_margin`) | 근접 조우(`≤ 1.25 m`, 최근 `4,000 ms` 이내) 직후 두 활성 트랙과 교차 이전 고정 갤러리 사이의 교차 합이 현재 합보다 `≥ 2 × 0.08` 크고 각 교차 유사도가 `≥ 0.58`일 때 두 World ID 교환 | 평상시에는 기하 추적의 연속성을 흔들지 않고, 오직 근접 조우가 기록된 두 트랙이 분리된 직후 양쪽 모두 상대방의 교차 전 갤러리와 더 강하게 일치할 때만 ID와 고정 갤러리를 맞바꾼다 |
| `DORMANT` 보존 시간 및 재입장 문턱 (`reid_dormant_ttl_ms` / `reentry_min_similarity`) | `dormant_ttl_ms = 1,800,000` (30분), 재입장 유사도 `≥ 0.68`, 경쟁 마진 `≥ 0.10`, 최소 가시 부위 비중 `≥ 0.50`, 최소 관측 `2`회 | 교실 수업·쉬는 시간(최대 30분) 동안 화각 밖으로 나갔다 돌아오는 동일 인물의 ID를 복원하되, 오결합을 막기 위해 Cross-camera FPR `0.001~0.002` 구간인 `0.68` 문턱과 경쟁 마진 `0.10`, 그리고 활성 트랙과의 비충돌 가드를 동시에 요구한다 |

## 3. 1단계 오프라인 평가 및 GPU 벤치마크 결과

산출물 정본(저장소 밖): `C:/Users/SSAFY/simai-local/reid-eval/` (`metrics.json`, `summary.md`, `run_record.json`).
원본 프레임과 사람 crop 이미지는 디스크·저장소에 한 장도 저장하지 않았다.

### 3.1 같은 사람 vs 다른 사람 코사인 유사도 분포 (`rec-20261029-015512z-lobby-railing-pose`)

4대 카메라 키프레임(`2.0 s` 간격, `987`프레임)에서 추출한 사람 crop `1,966`개에 대해 측정한 코사인 유사도 분포는 다음과 같다.

| 비교 구분 | 표본 쌍 수 | 평균 (mean) | 표준편차 (std) | p10 | p25 | 중앙값 (p50) | p75 | p90 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **같은 사람** (동일 카메라, 1.5~20s 간격) | 11,885 | 0.7905 | 0.1354 | 0.6230 | 0.7362 | **0.8214** | 0.8834 | 0.9216 |
| **같은 사람** (전체 World ID 기준) | 19,340 | 0.7777 | 0.1237 | 0.6100 | 0.7139 | **0.8032** | 0.8683 | 0.9099 |
| **같은 사람** (서로 다른 카메라 간, ≤2.5s) | 7,862 | 0.7473 | 0.1231 | 0.5777 | 0.6780 | **0.7731** | 0.8404 | 0.8804 |
| **다른 사람** (전체 동시 관측) | 3,943 | 0.3119 | 0.1090 | 0.1683 | 0.2470 | **0.3172** | 0.3799 | 0.4345 |
| **다른 사람** (동일 카메라 내 동시 관측) | 1,059 | 0.3159 | 0.1160 | 0.1651 | 0.2502 | **0.3228** | 0.3838 | 0.4367 |
| **다른 사람** (서로 다른 카메라 간 동시 관측) | 2,884 | 0.3104 | 0.1063 | 0.1702 | 0.2454 | **0.3155** | 0.3784 | 0.4340 |

### 3.2 분리도(ROC-AUC) 및 노트북 GPU (RTX 4050 Laptop) 벤치마크

| 구분 | ROC-AUC | 최적 문턱값 (Youden J) | 비고 |
|---|---:|---:|---|
| 전체 (같은 사람 vs 다른 사람) | **0.9910** | `0.50` (`J = 0.9276`) | `TPR = 0.960`, `FPR = 0.032` |
| 동일 카메라 내 (Same-Camera) | **0.9824** | `0.55` (`J = 0.9164`) | `TPR = 0.942`, `FPR = 0.026` |
| 서로 다른 카메라 간 (Cross-Camera) | **0.9901** | `0.50` (`J = 0.9201`) | 문턱 `0.65`에서 `TPR = 0.8040, FPR = 0.0024` |
| 공통 가시 부위 충분 (`co_visibility ≥ 0.65`) | **0.9876** | - | 상·하체 또는 상체+머리가 함께 보일 때 |
| 부분 가림 (`co_visibility < 0.65`) | **0.8552** | - | 난간 등에 하체가 가려진 관측 쌍 |

- **GPU 지연 및 VRAM**: 가중치 상주 `486.51 MB`, 배치 8 추론 시 `embed()` p50 **`73.85 ms`** (crop당 **`9.23 ms`**, 피크 VRAM `553.39 MB`).

## 4. 2단계 Tier 0 회귀 비교 결과 (`-464` 기하 정본 vs `-463` Re-ID 연결)

산출물 정본(저장소 밖):
- 10/29 녹화 COPE 특징 캐시: `C:/Users/SSAFY/simai-local/reid-eval/recording1029_reid_cache.npz` (4대 카메라 로컬 트랙 시계열 임베딩 `6,737`개, 원본/crop 이미지 미저장)
- 회귀 비교 실행 결과 및 `run_record.json`: `C:/Users/SSAFY/simai-local/track-463/round1/` (`summary.md`, `summary.json`, 각 실행 폴더의 `metrics.json` 및 `run_record.json`)
- 입력 조건: `rec-20261029-015512z-lobby-railing-pose` (`cache-s2` 포즈 캐시, `cal-fastrot-266z.json` 보정, `10 Hz` / `5,392` ticks / `8.99분`)

### 4.1 4개 설정 비교 표 (`plan_463.json`)

| 실행 ID | 설명 | 총 World ID (`ids/min`) | Confirmed ID | 트랙 수명 p50 (s) | 단명 비율 (`<6s`) | 분할 억제 횟수 (`reid_same`) | 구역 중복 진입 (raw/dedup) | 재입장 ID 유지율 (`reentry`) | 2인 구간 `new/p·min` | 2인 구간 `=n / >n / <n` | 2인 구간 Swap | 3인 구간 `new/p·min` | 3인 구간 `=n / >n / <n` | Tick 처리 시간 (`ms/tick`) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---|---:|
| **`G3_464_reid_off`** | `-464` 기하 단독 기준선 (Re-ID 꺼짐) | 29 (`3.23`) | 19 | 2.489 | 0.690 | 326 (`0`) | 5 (`10/5`) | **5.0%** (`1/20`) | 1.46 | `0.905 / 0.017 / 0.078` | 25 | 1.71 | `0.759 / 0.000 / 0.241` | 58.10 |
| **`R1_463_split_only`** | Re-ID 분할 판정만 켬 | 24 (`2.67`) | 15 | 3.341 | 0.583 | 357 (`31`) | 2 (`7/5`) | 6.2% (`1/16`) | 1.10 | `0.914 / 0.009 / 0.077` | 16 | 1.71 | `0.759 / 0.000 / 0.241` | 62.40 |
| **`R2_463_split_swap`** | Re-ID 분할 판정 + Swap 복원 | 24 (`2.67`) | 15 | 3.341 | 0.583 | 357 (`31`) | 2 (`7/5`) | 6.2% (`1/16`) | 1.10 | `0.914 / 0.009 / 0.077` | 16 | 1.71 | `0.759 / 0.000 / 0.241` | 62.17 |
| **`R3_463_full_reid`** | **Re-ID 전체 (`DORMANT` 30분 재입장 포함)** | **20 (`2.23`)** | **12** | **2.922** | **0.650** | **357 (`31`)** | **1 (`6/5`)** | **29.4% (`5/17`)** | **1.02** | **`0.914 / 0.009 / 0.077`** | **15** | **1.14** | **`0.759 / 0.000 / 0.241`** | **62.58** |

### 4.2 핵심 검증 결과 해석

1. **World ID 남발 및 단편화 추가 감소**:
   - `-464` 기하 단독 기준선(`G3_464_reid_off`) 대비 총 World ID가 **`29 → 20` (`-31.0%`)**, `CONFIRMED` World ID가 **`19 → 12` (`-36.8%`)**로 줄었다.
   - 다인 구간 인당 분당 신규 ID 생성률(`new/p·min`)도 2인 구간 **`1.46 → 1.02` (`-30.1%`)**, 3인 구간 **`1.71 → 1.14` (`-33.3%`)**로 감소했다.
2. **인원 과소추정(`<n`) 미증가 (안정성 가드 검증)**:
   - 2인 구간 정원 일치율(`=n`)은 **`90.5% → 91.4%`**로 향상되었고, 과대추정(`>n`)은 **`1.7% → 0.9%`**로 절반 가까이 줄었으며, 과소추정(`<n`)은 **`0.078 → 0.077`**로 전혀 늘지 않았다(3인 구간 `<n = 0.241`도 동일 유지).
   - 이는 경계 영역 분할 억제(`reid.split_suppressed_same = 31`회)가 실제 다른 사람을 잘못 합치지 않고 동일 인물의 `2+2` 분리 조각만 선택적으로 흡수했음을 뜻한다.
3. **2인 교차 구간 Swap 및 구역 중복 진입 감소**:
   - 2인 구간 로컬→월드 배정 전환(Swap) 횟수가 **`25 → 15` (`-40.0%`)**로 감소했고, 위험 구역 중복 진입(`zone_dup`)도 **`5회 → 1회` (`-80.0%`)**로 줄었다.
4. **이탈 후 재입장(`DORMANT`) ID 유지율 5.9배 향상**:
   - `LOST` 후 기하 재연결 창(`1.5 s`)이 지나 `DORMANT`(`lifecycle.dormant = 11`회)로 전환된 트랙 중, 화각에 다시 들어와 외형 유사도(`≥ 0.68`)와 마진(`≥ 0.10`)을 통과해 기존 World ID를 되찾은 건수가 `reid.reentry_reconnected = 3`회 + `reid.reentry_merged = 1`회 발생하여, 재입장 ID 유지율(`reentry`)이 **`5.0% (1/20) → 29.4% (5/17)`**로 **5.9배** 향상되었다.
5. **실시간 예산 준수**:
   - 갤러리 조회·유사도 계산·`DORMANT` 매칭이 추가된 후에도 재생 처리 시간은 틱당 **`58.10 ms → 62.58 ms`**(`+4.48 ms/tick`)에 불과해, 10 Hz(`100 ms`) 실시간 예산 안에서 여유 있게 동작한다.

## 5. 버린 대안

1. **Tier 0 10 Hz 루프 안에서 매 틱 동기식으로 COPE 추론을 호출하는 안**
   - 배치 8 기준 GPU 추론에 약 `74 ms`가 걸려 10 Hz(`100 ms`) 기하 추적·위험 판정 루프 예산을 잠식할 위험이 있었다.
   - 대신 `AsyncReidCollector` 백그라운드 데몬 스레드와 `LocalTrackFeatureCache`를 두어, 추적 루프는 캐시된 최신 특징을 O(1)에 읽기만 하고 큐가 가득 차면 지체 없이 건너뛰도록 분리했다.
2. **기하 잔차가 큰 임의의 두 관측을 외형 유사도만으로 직접 병합하는 안**
   - 같은 색 옷·유사 체형 인물이 교실 안에 동시에 있을 때 기하 게이트 없이 외형만으로 합치면 두 사람이 하나로 뭉개져 `<n`이 급증하고 위험 판정 위치가 왜곡된다.
   - 따라서 외형은 오직 (a) 기하 분할 억제 경계 반경(`1.35×`) 안, (b) 근접 조우 이력이 있는 두 트랙의 분리 직후, (c) 현재 활성 트랙과 겹치지 않는 `DORMANT` 후보와의 재연결에서만 작동하도록 제한했다.
3. **근접 교차 중에도 매 틱 갤러리 슬롯과 아바타 외형을 계속 갱신하는 안**
   - 두 사람이 `1.0 m` 이내로 스쳐 지나갈 때 검출 박스에 상대방의 몸통이 겹쳐 들어가 갤러리와 HMR 아바타 텍스처가 오염된다.
   - 이를 막기 위해 `≤ 1.0 m` 근접 및 분할 억제/저신뢰 틱에서는 갤러리 갱신을 동결(`reid.gallery_frozen`)하고 `avatar_update_hold=True`(`hold_appearance_update=True`)를 내보내도록 설계했다.

## 6. 잔여 위험 및 운영 수칙

1. **단체복·교복 등 극단적 동일 복장 환경**
   - 피험자들이 완전히 같은 상·하의를 입은 경우 `reid_margin`(`≥ 0.08~0.10`)이 확보되지 않아 Re-ID가 자동으로 보수적(불확실 → 기하 정본 유지)으로 동작한다. 이 경우 오결합은 막히지만 `DORMANT` 장기 재입장 복원율은 낮아질 수 있다.
2. **가림 직후 부분 관측만으로 재입장하는 경우**
   - 난간·책상 뒤에서 머리·어깨 일부만 보인 채 재등장하면 `co_visibility < 0.50` 또는 유사도 `< 0.68`로 즉시 재연결되지 않는다. 이를 보완하기 위해 트랙이 전신 관측을 회복해 `CONFIRMED`가 된 직후(`≤ 2.5 s`)에도 `_reid_late_reentry_merge`로 한 번 더 `DORMANT` 복원을 시도하도록 구현했다.
