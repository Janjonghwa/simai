# 깨진 PTS 시각선의 실시간 촬영 시각 (S15P31A101-315)

C110 4대 모두 카메라 PTS 가 13장 묶음마다 앞뒤로 약 0.4 s 번갈아 뛴다. 노트북 중계(LIVE)·녹화 도구·녹화 재생의 촬영 시각이 이 PTS 를 따르므로 Tier 0 가 카메라 대부분을 "늦은 카메라"로 빼거나 프레임을 "오래됨"으로 버렸다. 깨짐을 지나간 프레임만으로 감지해 그 epoch 의 남은 프레임을 프레임 시계로 매긴다.

- 관련 명세: 3.9 촬영 시각 추정·촬영 시각 계약, 5.1 프레임 최신성("이미 반영된 최신 결과보다 오래되면 버린다"), 5.2 시간 보간·삼각측량, 12.7 I0.
- 계약 변경 없음. `timestamp_source` 는 그대로 `RECEIVE_ESTIMATED`(수신 시각 기반 추정이라는 사실은 같다), 원본 `source_pts`·`received_at_ms` 는 헤더에 그대로 남는다.
- 오프라인 보정용 다시 매기기(-313 `extrinsics/sync.repair_timelines`, 행 전체·양방향)와 원리는 같고, 이 문서는 LIVE·녹화·재생의 인과 추정이다.

## 원인과 경로

### 데이터 (frames.jsonl 머리만, 영상 디코딩 없음)

LIVE 동시 녹화 `rec-20261028-034139z-live-demo-rehearsal`(4대 × 약 14,970장, 10분)과 걷기 녹화 `rec-20261028-020819z-walk-calibration`(4대 × 약 4,490장).

- PTS 묶음 점프: 이웃 PTS 간격 대부분 40 ms(묶음 안), 13장마다 약 +410 / −360 ms(진폭은 묶음마다 −86~−590 / +135~+670 으로 다양). 점프 간격 13장이 921~980 회(카메라당).
- 장기 비율은 맞다: PTS 기울기 40.08 ms/장 = 수신 시각 기울기 40.08 ms/장(노트북 시계로 24.95 fps, 공칭 25).
- 카메라가 보내지 않은 프레임이 있다: 묶음 안 PTS 간격 61~83 ms 다음 40 ms 가 카메라당 10분에 10~20 회. 프레임 순번만 세면 이 수만큼 시계가 밀린다.
- 접속 직후 몰림: 첫 약 28장이 1 ms 안에 도착한다(카메라 버퍼). 그 뒤 수신 시각은 프레임 순번에 선형이고 하한 위 흔들림은 키프레임(2 s 마다) 직후 최대 수백 ms.
- 녹화 추정 촬영 시각은 PTS 를 따른다: 수신 − 추정이 중앙값 약 405 ms, p5~p95 약 40~770 ms. 촬영 시각이 뒤로 간 걸음이 카메라당 551~552 회(최대 549~593 ms).

### 경로별로 깨진 PTS 가 들어가는 곳

| 경로 | 코드 | 수정 전 |
|---|---|---|
| LIVE 노트북 중계 | `gateway/relay` `relay.CameraRelay._relay_epoch` → `simai_recorder.capture_time.ReceiveTimeEstimator` | `pts + 창(30 s) 안 min(received − pts)` 이므로 묶음째 0.4 s 앞뒤로 흔들리고 뒤로 간다 |
| 녹화 도구 | `gateway/recorder` `recorder.CameraRecorder._record_epoch` → 같은 추정기 | 같은 값이 frames.jsonl 헤더에 남는다 |
| 녹화 재생(Tier 0 file) | `simai_ai.io.sources.RecordingReplay` | 헤더 값 그대로 |
| Tier 0 수신 | `io.freshness.LatestFrameScheduler._offer` | 직전보다 이른 촬영 시각은 `OUT_OF_ORDER` 로 버린다(명세 5.1). 뒤로 간 묶음 하나(약 10장)가 통째로 버려진다 |
| | `io.freshness.ResultGate.admit` | 반영한 결과보다 이르거나 같으면 버린다 |
| | `services/tier0/perception.Perception.ingest`·`time_model.choose_fusion_time` | 카메라별 `last_capture` 는 최댓값이다. +410 ms 묶음의 카메라가 `t_latest_global` 을 끌어올려 다른 카메라가 150 ms 넘게 "늦은 카메라"가 된다 |
| | `geometry.interpolation` | 같은 순간이 아닌 두 프레임 사이를 보간한다(0.4 s 떨어진 순간을 잇는다) |

LIVE 녹화 헤더로 Tier 0 판정을 흉내 내면(노트북 수신 순서, tick 100 ms, 150/500 ms) 늦은 카메라 로그 줄이 1,668 이고, 같은 녹화를 Pod 에서 Tier 0 로 재생한 실제 로그(`replay-lobby/r1`)는 1,486 줄이다. 원인이 이 경로임을 뒷받침한다.

## 방법

`gateway/recorder/src/simai_recorder/capture_time.py` 의 `FrameClock` 이 epoch 마다 프레임을 순서대로 본다. `ReceiveTimeEstimator` 는 공칭 fps 를 받으면 이것을 함께 돌려, 깨짐을 보기 전에는 기존 PTS 식 그대로, 본 뒤에는 프레임 시계 값을 낸다.

1. **감지.** 이웃 프레임 PTS 간격이 공칭 간격에서 150 ms 넘게 벗어난 걸음을 "깨진 걸음"으로 센다. 뒤로 깨진 걸음이 한 번 나오거나 최근 250장(10 s) 안에 깨진 걸음이 2번이면 그 프레임부터 epoch 끝까지 프레임 시계다(되돌리지 않는다). 앞으로 한 번 뛴 것(카메라 멈춤 뒤 재개)은 바꾸지 않는다.
2. **프레임 시계.** 이어 붙인 PTS 다: 이웃 간격이 공칭의 0.5~2.5 배면 그 간격(흔들림·빠진 프레임 1장까지 그대로), 아니면 공칭 간격 1장. 묶음 점프는 1장으로 바뀌고 빠진 프레임은 센다.
3. **원점·기울기.** `y = received_at_ms − clock` 을 1 s 칸마다 최솟값으로 모으고(최근 `window_ms` = 30 s), 그 아래를 지나며 위로 거리 합이 가장 작은 직선(Moon·Skelly·Towsley 1999 의 선형계획 = 아래 볼록 껍질에서 x 평균을 품은 변)을 하한으로 쓴다. 공칭 40 ms 와 실제 40.08 ms 의 차이(0.2%)는 이 기울기가 맞춘다. 명세 3.9 의 "하한은 긴 창에서 직선으로 맞춘다" 를 이 경로에서 따른다.
4. **값.** `estimated = clock + 하한(clock) − latency_ms`, 그 프레임의 수신 시각을 넘지 않는다. 칸이 5 s 넘게 쌓이기 전(직선 전, 최솟값 기울기 0)은 불확실성을 `uncertainty_no_pts_ms`(45)로, 그 뒤는 `uncertainty_ms`(20)로 둔다.
5. **epoch.** 재접속하면 새 추정기다(PTS 식으로 시작해 다시 감지). 녹화 도구는 `events.jsonl` 에 `pts_timeline_broken`(stream_epoch, frame_sequence, clock), 중계는 WARNING `pts_timeline_broken` 을 epoch 마다 한 번 남긴다.
6. **녹화 재생.** `ai/src/simai_ai/io/capture_clock.py` 가 같은 `FrameClock` 을 옮겨 쓰고(ai 는 gateway 를 import 하지 않는다), `RecordingReplay(repair_capture_time=True 기본)` 가 세그먼트마다 녹화 헤더의 원본 `received_at_ms`·`source_pts` 와 manifest `capture_time_model`, codec_info `nominal_fps` 로 다시 매긴다. 깨짐을 보기 전 프레임은 헤더 그대로다(그때까지 녹화 도구도 PTS 식이었다). 녹화 파일은 고치지 않는다. Tier 0 는 열 때 카메라·epoch 별 전환 프레임을 WARNING 으로 남긴다.

원본 PTS 식 추정값은 헤더의 `source_pts`·`received_at_ms` 로 언제든 다시 계산된다(기존 녹화는 파일에 그대로 있다). frame_header 에 원래 추정값 필드를 더하지 않았다(I0 계약 변경, 아래 버린 대안).

멀쩡한 PTS(흔들림, B 프레임 2장 재정렬, B 묶음 경계의 한 번의 1 s 멈춤)는 감지되지 않아 결과가 바꾸기 전과 같다(시험). 실제 녹화 8개 세그먼트에서도 전환 전 프레임의 새 추정값이 녹화 헤더와 모두 같다.

### 명세에 없어 정한 수치와 근거

| 값 | 근거 |
|---|---|
| 깨진 걸음 150 ms | B 프레임 2장 재정렬(공칭에서 ±120 ms 이하)·흔들림보다 크고 묶음 점프(대부분 360~440 ms)보다 작다. -313 과 같은 기준 |
| 10 s 안 2번, 또는 뒤로 1번 | 묶음 점프는 13장마다라 10 s 에 약 19번이다. 한 번 앞으로 뛴 것은 카메라 멈춤일 수 있다. 뒤로 150 ms 넘게 가는 PTS 는 멀쩡한 스트림에 없다. 실제 녹화는 13~28장째에 바뀐다 |
| 이어 붙이기 0.5~2.5 배 | 간격 1·2장(흔들림 ±20 ms 포함)만 받는다. 3장까지(±16 ms) 받으면 작은 진폭 점프(+135 ms 등)를 빠짐으로 잘못 세어 하한이 계단처럼 밀렸다(아래 비교) |
| 1 s 칸, 30 s 창(`window_ms`), 직선은 5 s 부터 | 칸 최솟값이 키프레임 지연을 거른다. 창은 기존 PTS 식과 같은 모델 값(manifest 에 남는다) |
| 기울기 상한 5000 ppm | 실측 0.2%(2000 ppm). 넘으면 맞춤이 틀린 것으로 보고 최솟값을 쓴다 |
| 불확실성 20 ms(직선 뒤) | 인과 추정과 앞뒤 ±15 s 하한 직선(비인과)의 차이가 LIVE 녹화 \|p95\| 13~17 ms, 걷기 6.5~13 ms. PTS 식과 같은 잠정값 20 을 쓴다. 고정 지연(G0 LED)은 두 식에 같이 남는다 |
| 불확실성 45 ms(직선 전) | 접속 직후 몰림 동안 추정은 수백 ms 틀릴 수 있다. 25 ms 기준(3.9)을 넘겨 삼각측량에서 빠진다(PTS 없음과 같은 처리). 녹화에서 카메라당 약 123~137장(5 s) |

## 검산 (GPU 없음, frames.jsonl 머리만)

도구는 Owner scratchpad 의 일회성 스크립트(커밋하지 않음)이고, 같은 계산을 저장소 구현(`ReceiveTimeEstimator`)으로 돌렸다. ai `repair_segment` 결과는 8개 세그먼트 모두 gateway 와 같다.

LIVE 동시 녹화(10분, 4대):

| 지표 | 수정 전(녹화 헤더) | 수정 후 |
|---|---|---|
| 수신 − 추정, 중앙값 / p95 | 402~416 / 751~768 ms | 20~21 / 123~153 ms |
| 촬영 시각이 뒤로 간 걸음(카메라당) | 551~552 (최대 549~593 ms) | 0 |
| Tier 0 흉내: 카메라 간 last_capture 차이 p50 / p95 / p99 | 231 / 682 / 750 ms | 39 / 167 / 248 ms |
| 차이가 150 ms 를 넘은 tick | 78.3% | 6.7% |
| 융합 카메라 수 4 / 3 / 2 / 1 (tick 5,986) | 2,020 / 1,919 / 1,984 / 63 | 5,595 / 333 / 58 / 0 |
| 늦은 카메라로 뺀 tick(4대 합) / 로그 줄 | 4,350 / 1,668 (실제 재생 1,486) | 449 / 358 |
| 최신성 OUT_OF_ORDER 로 버린 프레임(카메라당) | 5,070~5,157 (약 34%) | 0 |
| 인과 추정 − 비인과 하한, \|p95\| / 최대 | — | 13~17 / 27~51 ms |

걷기 녹화(3분): 카메라 간 차이 p95 649 → 56 ms, 150 ms 넘은 tick 52.9% → 0%, 4대 융합 53% → 100%, 늦은 카메라 로그 447 → 0, OUT_OF_ORDER 카메라당 약 1,515 → 0, 인과 − 비인과 \|p95\| 6.5~13 ms.

- 수정 후 남은 150 ms 초과(LIVE 6.7%)는 촬영 시각 오류가 아니라 실제 도착 지연이다(수신 − 추정 p95 123~153 ms, p99 206~251 ms, 키프레임 직후). 늦게 도착한 카메라를 기다리지 않는 것은 설계 의도대로다(`docs/design/tier0-fusion-time.md`). Pod 수신은 노트북 → Pod 전송 지연이 더해지므로 GPU 재생·LIVE 에서 다시 잰다.
- 흉내는 카메라마다 받은 모든 프레임을 넣는다. 실제 Tier 0 는 tick 마다 카메라당 한 장(latest-frame-wins)이고 검출 시간이 있다.

## Tier 0 판정과의 정합 (코드 확인, 값은 바꾸지 않음)

- 최신성(5.1): 프레임 시계 값은 epoch 안에서 앞으로만 간다(합성·실데이터 모두 뒤로 간 걸음 0). `OUT_OF_ORDER` 가 없어지고 `ResultGate` 도 통과한다. 접속 직후 몰림 동안 같은 ms 가 반복되는 것(카메라당 약 20장)은 수정 전과 같다.
- 융합 시각(`choose_fusion_time`, 150 ms): 카메라별 최신 촬영 시각이 PTS 점프로 0.4 s 앞서지 않으므로 늦은 카메라 판정은 실제 도착 지연에만 걸린다. 150 ms 값(설계 제안값)은 그대로 둔다.
- 보간 기준(3.9, 25 ms): 직선이 선 뒤 20 ms 로 보간에 들어가고, 직선 전 약 5 s 는 45 ms 로 빠진다.
- 명세 5.1 의 값(대기열 1~2장, queue_wait 150 ms)은 촬영 시각과 무관하다(대기 시간 기준). 바꿀 근거가 없다.

## 코드·시험 위치

- `gateway/recorder/src/simai_recorder/capture_time.py`: `FrameClock`, `lower_envelope_line`, `ReceiveTimeEstimator(nominal_fps=…)`·`mode`·`broken_since`.
- `gateway/recorder/src/simai_recorder/recorder.py`, `gateway/relay/src/simai_relay/relay.py`: 공칭 fps 전달, 전환 기록.
- `ai/src/simai_ai/io/capture_clock.py`, `ai/src/simai_ai/io/sources.py`(`RecordingReplay`·`CaptureTimeRepair`), `ai/services/tier0/service.py`(재생 열 때 로그).
- 시험: `gateway/recorder/tests/test_capture_time.py`(묶음 점프 → 바꿈·앞으로만·정답 12 ms 안·빠진 프레임, 앞으로 먼저 뛴 경우, 멀쩡한 PTS·B 프레임·멈춤은 그대로, 공칭 fps 없음, 새 epoch, 하한 직선, 공유 벡터), `gateway/recorder/tests/test_recorder.py::test_broken_pts_timeline_is_recorded_with_frame_clock`, `gateway/relay/tests/test_relay.py::test_broken_pts_timeline_switches_to_frame_clock_per_epoch`(재접속 포함), `ai/tests/io/test_capture_clock.py`(공유 벡터 일치, 재생 다시 매김, 끄기, 공칭 fps·수신 시각 없음, 멀쩡한 녹화).
- 공유 벡터 `gateway/recorder/tests/data/capture_clock_vector.json`: 두 구현이 같은 값을 내는지 본다. 알고리즘을 바꾸면 gateway/recorder 에서 `uv run python tests/test_capture_time.py --write-vector` 로 다시 만들고 ai 쪽을 같이 고친다.

## 버린 대안

- **프레임 순번만 세는 시계(PTS 무시).** 빠진 프레임(카메라당 10분 10~20회)만큼 40 ms 씩 밀려 하한이 계단이 된다. 칸 최솟값의 하한 위 이탈 p95 가 72 ms(0.5~2.5 배 이어 붙이기는 30 ms)였다.
- **간격을 공칭의 정수배(3장까지, ±16 ms)로 반올림해 이어 붙이기.** 작은 진폭 점프(+135, −86 ms 등)를 빠짐으로 읽어 계단이 생겼다(cam_A 13,500장 부근 약 100 ms). 이탈 p95 34 ms.
- **창 10~15 s.** 하한 이탈 지표는 더 작지만(p95 16~22 ms) 혼잡으로 바닥이 함께 올라가는 동안 따라 올라가 촬영 시각이 늦어진다. 기존 모델 값(30 s, manifest 에 남음)을 그대로 쓴다.
- **늘 프레임 시계로 매기기.** B 프레임 스트림에서 표시 순서가 틀어지고, 멀쩡한 카메라 결과가 바뀐다. 깨짐을 본 epoch 에만 쓴다.
- **녹화 도구는 그대로 두고 재생에서만 고치기.** 녹화 파일과 LIVE 헤더가 다른 규칙이 된다. 녹화도 같은 추정기로 쓰고, 원래 PTS 식 값은 원본 필드로 다시 계산되게 했다.
- **frame_header 에 원래 추정값·시계 종류 필드 더하기.** I0 계약 변경(Lead 승인)이 필요하고, 원본 `source_pts`·`received_at_ms` 로 다시 계산되므로 정보가 늘지 않는다. 전환은 이벤트·로그로 남긴다.
- **RTCP Sender Report 카메라 시각(`CAMERA_STREAM`).** G0 LED 시험 전이라 쓸 수 없다(3.9). 이 PTS 와 같은 시계라면 함께 깨졌을 수 있다(미확인).
- **불확실성을 25 ms 이상으로.** 25 ms 기준(3.9)에 걸려 모든 프레임이 삼각측량에서 빠진다. 측정한 추적 오차(\|p95\| 13~17 ms)는 PTS 식 잠정값 20 안이다.

## 잔여 위험

- 실제 촬영 시각과의 차이는 재지 않았다(G0 LED 전). 하한은 "가장 덜 늦게 온 프레임" 기준이라 카메라마다 전송 고정 지연이 다르면 그만큼 카메라 사이에 남는다(PTS 식도 같다).
- 몇 초짜리 ±40~55 ms 구간이 있다(LIVE 녹화 cam_C·cam_D 각 1~2 s, cam_A 최대 42 ms). 점프와 겹친 빠짐을 세지 못하거나 바닥이 바뀔 때 직선이 따라가는 동안이다. 이때도 불확실성은 20 이다.
- 깨짐을 보기 전 프레임(13~28장, 접속 직후 몰림 안)은 PTS 식 그대로다. 첫 점프가 앞으로면 다음 점프까지 한 묶음(13장)이 0.4 s 틀릴 수 있다(접속 직후 1 s 안).
- 10 s 안에 카메라 멈춤이 두 번이면 멀쩡한 스트림도 프레임 시계로 바뀐다(값은 여전히 수신 하한에 맞지만 멈춘 시간만큼 하한이 계단이 되어 창 길이 동안 이르게 나온다). B 프레임 3장 이상 재정렬 스트림, B 프레임 스트림에서 멈춤이 B 묶음 가운데에 걸린 경우(뒤로 1 s 넘게 가는 PTS 로 보인다)는 오감지될 수 있다(C110 은 B 프레임 없음).
- 공칭 fps 가 없는 입력(RTSP 가 fps 를 주지 않음)은 감지하지 않는다(그대로 PTS 식).
- 중계 링크 측정(`link_stats` 의 PTS 간격·카메라 끊김)은 원본 PTS 로 계산하므로 깨진 카메라에서 PTS 간격 최댓값이 크게 나온다(이 이슈에서 바꾸지 않음).

## 다음 GPU 세션: 같은 녹화를 Tier 0 로 다시 재생해 비교

LIVE 관제 시간이 아닐 때, `replay-lobby/r1` 과 같은 설정(보정 `cal-wf-20261028-030007z`, 구성 묶음 `cfg-lobby-1`, Scene `scene-lobby-0`)으로 출력 폴더만 새로 둔다. 실행 형식은 `docs/design/tier0-recording-replay.md` "실행".

```sh
cd ai
# r1 과 같은 tier0.json 을 복사하고 input.jsonl_dir 만 새 이름으로(있는 폴더는 덮어쓰지 않고 종료 코드 2)
#   "input": {"mode": "file", "session_dir": ".../recordings/rec-20261028-034139z-live-demo-rehearsal",
#             "decoder": "cpu", "speed": "max", "jsonl_dir": "out"}
uv run --extra tier0 python -m services.tier0 --config <runs>/replay-lobby/r2/tier0.json 2>&1 | tee <runs>/replay-lobby/r2/tier0.log
grep -c "PTS 시각선이 깨져" <runs>/replay-lobby/r2/tier0.log   # 4 (카메라마다 한 번, frame_sequence 15·28·27·27)
```

비교 지표(같은 스크립트로 r1·r2):

```sh
python - <runs>/replay-lobby/r1 <runs>/replay-lobby/r2 <<'PY'
import json, statistics, sys
from collections import Counter, defaultdict
from pathlib import Path
for run in map(Path, sys.argv[1:]):
    cams, ids, t0, t1 = Counter(), defaultdict(list), None, None
    for line in open(run / "out" / "world_state.jsonl", encoding="utf-8"):
        p = json.loads(line)["payload"]; t0 = t0 or p["t_latest_global"]; t1 = p["t_latest_global"]
        for tr in p["tracks"]:
            cams.update([len(tr["cameras"])] if tr["observation_state"] == "OBSERVED" else [])
            ids[tr["world_id"]].append(p["t_latest_global"])
    ev = {e["event_id"]: e for e in (json.loads(l)["payload"] for l in open(run / "out" / "risk_event.jsonl", encoding="utf-8"))}
    lag = sum("늦은 카메라를 융합에서 뺀다" in l for l in open(run / "tier0.log", encoding="utf-8", errors="replace"))
    print(run.name, dict(sorted(cams.items())), len(ids) / ((t1 - t0) / 60000),
          statistics.median((max(v) - min(v)) / 1000 for v in ids.values()),
          sum(e["resolved_at"] is None for e in ev.values()), lag)
PY
```

r1(수정 전) 값: OBSERVED 트랙의 기여 카메라 0/2/3/4대 = 200 / 3,960 / 216 / 2, World ID 분당 7.6, 수명 중앙값 0.58 s, 해소되지 않은 R1 47건, 늦은 카메라 로그 1,486 줄. 기대: 3·4대 기여가 다수, 늦은 카메라 로그가 수백 줄 이하(노트북 흉내 358 줄 + Pod 전송 지연), World ID 분당 수와 R1 미해소 감소. 다르면 Tier 0 의 `frames.skipped`(OUT_OF_ORDER) 와 `lagging_ticks` 를 먼저 본다. 결과는 run_record 로 남긴다.

## 넘긴 일

- 명세 3.9 "긴 창 직선 맞춤 반영 여부는 확인 필요": 프레임 시계 경로에는 반영했고 PTS 식(멀쩡한 카메라)은 그대로 창 최솟값이다. 문구 정리는 명세 정리 MR 몫.
- 같은 카메라의 RTCP SR 카메라 시각이 이 PTS 와 같이 깨지는지(`gateway/rtsp_probe`) — G0 `CAMERA_STREAM` 판단 재료.
- -313 `repair_timelines` 는 새 녹화(이 수정 뒤)에는 걸리지 않을 것이다(헤더가 이미 프레임 시계). 기존 녹화용으로 남긴다.
