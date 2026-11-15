# 프로세스 분리 Re-ID (S15P31A101-744)

-738 로 검출·판정 프로세스 분리(-728)를 기본으로 켜면 분리 경로가 Re-ID 를 끄므로, 다음 세션에 Re-ID 묶음(seed-reid)을 올려도
Re-ID 가 꺼진다(17·18차는 S3 GPU 캐시에 묶음이 없어 단일 경로에서도 `{"enabled": true, "state": "disabled", ...}` 였다). 이 티켓은
-728 설계 9절 4 를 끝내 분리 경로에서도 Re-ID 를 켠다. -738 과 함께(그 뒤에) 머지한다.

## 1. 관련 명세·코드·시험

| 명세 | 이 문서에서 |
|---|---|
| 3.2 World Track Manager 하나·정본 단일 소유, AGENTS 규칙 1 | Re-ID 결정(보조 비용·DORMANT 재입장·swap 복원·분할 판정)은 판정 프로세스의 World Track 그대로 |
| 5.2 Re-ID 는 기하 게이트 뒤 보조 비용, AGENTS 규칙 8 | 검출 프로세스는 특징만 만든다. ID·위치를 바꾸지 않는다 |
| 6.4 재시작 | 검출 프로세스가 다시 뜨면 판정 쪽 특징 사본을 비운다 |

| 구분 | 경로 |
|---|---|
| 검출 쪽 수집기·판정 쪽 사본 | `ai/services/tier0/split_side_work.py`(`SideWorkSpec.reid`, `ChildSideWork._build_reid`, `RemoteReidCollector`) |
| 경계 | `ai/services/tier0/detect_process.py`(곁가지를 검출 입력보다 먼저 만듦, `built` 메시지에 곁가지 상태, `SplitDetectorFeed.reid_collector`·`reid_submission_status`) |
| 수집 정책 함수 | `ai/src/simai_ai/reid/collector.py`(`sampling_snapshot`, `set_sampling`, 넘긴 빈 캐시를 버리던 `cache or` 수정) |
| file 재생 결정론 | `ai/services/tier0/perception.py`(file 입력은 묶음 안에서 크롭 제출·임베딩을 끝냄, `REID_FLUSH_TIMEOUT_S`) |
| 조립 | `ai/services/tier0/service.py`(`_reid_config`, `reid_embedder_factory` 시험 주입, 상태의 `disabled` 비움) |
| 시험 | `ai/tests/services/tier0/test_tier0_split_reid.py`(새), `split_fakes.py`(임베더·큰 그림 입력 대역) |

계약(`contracts/`) 변경 없음.

## 2. 경계

| 프로세스 | 하는 일 |
|---|---|
| 검출 | `AsyncReidCollector`(크롭·COPE 임베딩, 수집 주기·초당 상한). `DetectorFeed` 가 지금과 같이 크롭을 넘긴다. 캐시에 넣는 순간(`ForwardingCache.put`) 특징(`LocalTrackFeature`: 카메라·로컬 ID[-g세대]·frame_sequence·촬영 시각·임베딩·가시성·품질·방향)을 out 큐로 `("side", "reid_feature", feature)` 보낸다(차면 버리고 `features_dropped`) |
| 판정 | `RemoteReidCollector`: 받기 스레드가 특징을 `LocalTrackFeatureCache` 사본에 넣고(같은 키는 촬영 시각이 늘 때만), World Track 의 `feature_lookup` 이 사본을 읽는다. `update_sampling` 은 `sampling_snapshot`(단일 경로와 같은 함수)으로 계산해 `("side", now, "reid_sampling", t, stable_keys, people)` 명령으로 보낸다 |

- 특징 하나는 임베딩(COPE 1280차원 float32 약 5 KB)이 대부분이고 초당 상한 8개라 경계 비용이 작다.
- Perception 은 조립 때 `collector.active` 로 lookup 을 넘길지 정한다. 하트비트 전이라도 알 수 있게 검출 프로세스가 `built` 메시지에 곁가지 상태(Re-ID state)를 싣고, 판정 쪽은 하트비트가 없을 때 그 값을 쓴다.
- 수집 주기 명령: file 입력은 tick 마다 순서 큐로(단일 경로가 tick 마다 정책 시각을 갱신하는 것과 같게). LIVE 는 정책이 같으면 0.4초마다만(검출 쪽은 정책 시각과 촬영 시각 차 500 ms 안에서만 정책을 쓴다).
- 상태: `status.reid`·`stage_timing.reid` 는 단일 경로와 같은 키(`enabled`·`state`·`reason`·수집 계수, 검출 프로세스 보고값)에 `mode: "detect_process"`, `features_received`, `parent_cached_tracks`, `sampling_sent`, `resets` 를 더한다. 수집기 기동 실패는 `{"enabled": true, "state": "disabled", "reason": "start_failed"}`(검출 프로세스는 그대로 뜬다). health 계약에는 reid 칸이 없어 바꾸지 않았다.
- `process_split.disabled` 는 이제 빈 목록이다.

## 3. file 재생 결정론(다시 정함)

단일 경로도 크롭 제출·임베딩이 작업 스레드라 특징이 어느 판정 tick 에 들어갈지가 스레드 시각에 달렸다(Re-ID 가 켜지면 최대 속도
재생도 결정론이 아니었다). 분리 경로는 특징이 IPC 를 한 번 더 지나므로 그 차이가 커진다.

정한 것: **file 입력은 한 검출 묶음의 크롭 제출·임베딩을 그 묶음 안에서 끝낸다**(`DetectorFeed` 비동기 아님 + 수집기 active 일 때
`submit_observation` 직접 → `flush`, 상한 2초). 단일·분리 모두 같은 지점(검출 묶음 끝, 관측을 판정에 넘기기 전)이고, 분리 경로는
특징 메시지가 같은 out 큐에서 step 응답보다 먼저 가므로(같은 프로세스 큐의 넣은 순서) 판정 쪽 사본에 특징이 먼저 들어간다. 그래서
같은 녹화는 Re-ID 켬에서도 분리 켬/끔 같은 world_state(클러스터·World ID·msg_id)를 낸다(시험 b).

허용 차이(이 시험이 고정한 조건):
- 초당 크롭 상한(`max_crops_per_second`)은 벽시계 기준이라 최대 속도 재생에서 버리는 크롭이 실행마다 달라질 수 있다. 시험은 상한을
  넉넉히 둔다. 운영 녹화 재생(1배속)은 상한이 촬영 시각 흐름과 비슷하게 작동한다.
- 한 묶음 임베딩이 2초를 넘으면 그 특징은 다음 묶음 뒤에 들어간다(`reid_submission.dropped` 로 셈).
- LIVE 는 비동기 그대로다(판정을 기다리게 하지 않는다). LIVE 에서 분리 켬/끔의 특징 도착은 IPC 한 번(1 ms 미만)만큼 다르고, 특징
  유효 시간(30초)·조회 규칙(촬영 시각 ≤ 판정 시각)은 같다.

## 4. 재시작

검출 프로세스가 다시 뜨면(`on_new_generation`) 판정 쪽 사본을 비우고 수집 정책을 다시 보낸다. 새 세대 로컬 ID 는 `-g<세대>` 라 옛
특징과 키가 겹치지 않고, 옛 세대 out 큐는 버려져 늦은 옛 특징이 들어오지 않는다. World Track 갤러리(판정 프로세스)는 그대로라
DORMANT 재입장·swap 복원은 새 세대 특징으로 이어진다.

## 5. 버린 대안

| 대안 | 버린 이유 |
|---|---|
| 판정 쪽이 lookup 마다 검출 프로세스에 묻기 | 판정 tick 안에서 IPC 왕복을 기다린다(지연·결정론 모두 나쁨) |
| 특징을 하트비트(0.5초)에 묶어 보냄 | 도착이 최대 0.5초 늦어 재입장·swap 판단이 늦고 file 결정론이 깨진다 |
| 크롭(그림)을 판정 쪽으로 보내 임베딩 | 그림 전송·판정 프로세스 CUDA 문맥(-728 1절 버린 대안) |
| file 결정론을 포기하고 분리 켬/끔 비교만 느슨하게 | Re-ID 켬 녹화 재생 비교(A/B·리허설)가 의미를 잃는다. 묶음 안 flush 비용은 REPLAY 에서만 든다 |

## 6. 남은 일·잔여 위험

- 실측 전: COPE 가 검출 프로세스 GPU 에서 TensorRT 검출과 함께 돌 때 검출 처리 시간(`processing.detect`)·아바타 관문 영향은 다음 GPU 세션(seed-reid) 에서 본다. 단일 경로에서도 같은 프로세스 GPU 를 나눴다.
- `ForwardingCache` 는 검출 쪽 캐시와 같은 특징을 판정 쪽에도 둔다(메모리 두 배, 사람 수 × 카메라 × 5 KB 수준).
- LIVE 에서 out 큐가 차면 특징을 버린다(Re-ID 는 보조라 기하 추적은 그대로). `features_dropped` 로 본다.
- -728 9절 5(판정 프로세스 CPU 묶음)·6(명세 3.1·3.2 문구 정리 MR)은 남았다.
