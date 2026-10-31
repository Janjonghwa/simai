# Tier 0 디코더 지연 BGR 변환과 색 변환 스레드 조정

S15P31A101-386. 관련 명세 §5.1(프레임 최신성 정책: 디코딩과 추론 분리)·§3.12(Tier 0 런타임 자원)·§6.6(이벤트 사진)·§12.7(I0 영상 스트림). 외부 계약(`contracts/`)은 변경하지 않는다. 리뷰 생략(사용자 지시).

- 구현 위치: `ai/src/simai_ai/io/decoder.py`, `ai/src/simai_ai/io/pyav_decoder.py`, `ai/src/simai_ai/io/ingest.py`, `ai/src/simai_ai/io/freshness.py`
- 시험 위치: `ai/tests/io/test_decoder.py`, `ai/tests/io/test_freshness.py`, `ai/tests/io/test_pyav_decoder.py`

## 1. 문제와 배경

H.264 스트림은 P/B 프레임 참조 의존성(DPB) 때문에 디코더(`PyAvDecoder.decode`)가 도착한 모든 압축 접근 단위(카메라 4대 × 25 fps = 100 fps)를 순서대로 디코딩해야 한다(명세 §5.1). 그러나 색 변환(`yuv420p`/`nv12` → `bgr24`, `swscale`)과 `numpy.ndarray`(`H×W×3 uint8`) 할당은 이전 프레임과 독립적이다.

기존 구현은 `PyAvDecoder._pictures()` 안에서 디코딩된 모든 `av.VideoFrame`에 대해 즉시 `self._reformatter.reformat(frame, format="bgr24", threads=self.convert_threads).to_ndarray()`를 실행했다.
- 2304×1296 BGR24 한 장은 `2304 × 1296 × 3 = 8,957,952 B` (약 `8.96 MB`, `8.54 MiB`)다.
- 4대 × 25 fps = 100 fps에서는 초당 `895.80 MB/s` (`854.30 MiB/s`)의 BGR 배열을 할당하고 100회 `swscale` 변환을 수행했다.
- 반면 최신성 대기열(`LatestFrameScheduler`, 기본 `queue_depth=1`)은 10 Hz 추론 주기가 `take()`를 부르기 전까지 도착한 카메라당 이전 프레임의 약 60%(초당 60장)를 `SkipReason.SUPERSEDED`로 즉시 버린다. 즉 초당 약 `537.48 MB`의 배열 할당과 60회의 `swscale` 변환이 추론·배경·이벤트 사진 어디에도 쓰이지 않고 버려지고 있었다(`.agy/ref/argon-gpu-consult-final.md` P1).

또한 카메라당 색 변환 스레드 기본값(`DEFAULT_CONVERT_THREADS = 4`)은 4대 카메라에서 최대 16개의 `swscale` 스레드를 만들어, 컨테이너 CPU 쿼터 안에서 H.264 `SLICE` 디코딩 스레드·수신 스레드 4개·이벤트 JPEG 인코딩 스레드(최대 4개)·배경 중앙값 스레드·추론/판정 스레드와 경합했다.

## 2. 대안 비교와 결정

| 대안 | 메모리·CPU 영향 | 하위 경로(`-383`·`-378`·`-363`) 정합성 | 결정 |
|---|---|---|---|
| (a) `DecodedPicture` 지연 변환 + `LatestFrameScheduler` 인계 시 구체화 | `SUPERSEDED`·`QUEUE_WAIT_EXCEEDED`·`OUT_OF_ORDER`·`STREAM_RESET` 드롭 프레임은 BGR 변환·할당 0회. 4대×25fps→10Hz 기준 변환 횟수·할당량 60% 감소 | `next_batch`/`next_frame`이 반환하는 `Frame.image`는 기존과 동일한 `np.ndarray`이므로 검출기·`EventFrameBuffer`·`BackgroundCleanBackground`가 단일 BGR 배열 참조를 그대로 공유 | **선택** |
| (b) 비참조/P 프레임 디코딩 자체 생략 | H.264 디코딩 CPU까지 줄지만 DPB 참조가 깨져 이후 프레임 디코딩 실패 또는 손상 프레임 발생 | 명세 §5.1("디코더는 프레임 의존성 때문에 모든 압축 프레임을 받는다") 위반 | 기각 |
| (c) `Frame.image`에 `av.VideoFrame`(YUV)을 담아 하위 소비자에게 전달 | 대기열까지는 빠르나 검출기·이벤트 사진·배경 누적기가 각자 BGR 변환을 중복 수행하거나 PyAV 타입에 결합됨 | `EventFrameBuffer`(`frame.image.nbytes` 검사)·OpenCV/Ultralytics 입력 형식(`BGR ndarray`)과 충돌 | 기각 |

## 3. 구조와 수명주기

1. **`DecodedPicture` 지연 로더 (`ai/src/simai_ai/io/decoder.py`)**
   - `DecodedPicture(key, image)`(기존 즉시 값, `FakeDecoder` 등)와 `DecodedPicture(key, loader=callable)`(지연 변환, `PyAvDecoder`)를 모두 지원한다.
   - `picture.converted` 프로퍼티로 변환 여부를 조회할 수 있다.
   - `picture.image`를 처음 읽을 때 그림별 잠금(`threading.Lock`) 아래에서 `loader()`를 정확히 1회 호출해 `_image`에 캐시하고, 즉시 `_loader = None`으로 지워 클로저가 붙잡고 있던 원본 `av.VideoFrame` 참조를 해제한다(GC 가능).
2. **`PyAvDecoder` (`ai/src/simai_ai/io/pyav_decoder.py`)**
   - `_pictures()`에서 `pts → FrameKey` 결합(G3)과 `frame.is_corrupt` 검사는 디코딩 시점에 즉시 수행한다.
   - 색 변환은 즉시 실행하지 않고 `_make_loader(frame, reformatter, reformat_lock, output_format, convert_threads)`를 넘겨 `DecodedPicture`를 만든다.
   - `reformatter`와 `reformat_lock`을 로더 클로저에 함께 묶어 두므로, `decode()`가 돌려준 `DecodedPicture`의 `.image`를 `flush()`·`reset()`·재-`configure()` 뒤에 읽더라도(직접 디코더를 호출하는 단위 시험·재연결 경계 등) 안전하게 변환된다.
   - 실제로 색 변환이 실행된 횟수를 `decoder.converted`에 기록한다.
3. **`CameraIngest._emit` 및 `LatestFrameScheduler` (`ai/src/simai_ai/io/ingest.py`, `ai/src/simai_ai/io/freshness.py`)**
   - `CameraIngest._emit()`은 `picture.image`를 읽지 않고 `Frame(..., image=picture)`를 `LatestFrameScheduler.offer()`에 넣는다.
   - 대기열 안에서 `SUPERSEDED`, `OUT_OF_ORDER`, `QUEUE_WAIT_EXCEEDED`, `STREAM_RESET`(`discard`)으로 버려지는 프레임은 끝까지 `.image`를 읽지 않으므로 BGR 변환이 일어나지 않는다.
   - `LatestFrameScheduler._handoff()`(`take()` / `take_one()`)는 대기열 잠금(`self._lock`) 안에서 최신성 상한(`max_queue_wait_ms`)을 통과한 티켓을 꺼낸 뒤, **잠금 밖에서** `_materialize(ticket)`을 호출해 `DecodedPicture.image`를 실제 BGR `ndarray`로 풀어 `ticket.frame.image`에 넣는다. 수신 스레드의 `offer()`는 변환 시간 동안 막히지 않는다.
4. **하위 소비자(`-383`, `-378`, `-363`)와의 공존**
   - `I0Ingest.next_batch()` / `next_frame()`에서 나온 `InferenceTicket.frame.image`는 항상 구체화된 BGR `np.ndarray`다.
   - 따라서 `DetectorFeed`(및 `-383`의 분리된 검출 스레드), `EventFrameBuffer`(`-378`, `frame.image.nbytes` 확인 후 참조 보관), `BackgroundCleanBackground`(`-363`, `frame.image` 참조 보관), `Perception._share_avatar`는 모두 수정 없이 동일한 단일 BGR `ndarray` 참조를 공유하며, 추가 복사나 중복 색 변환이 발생하지 않는다.

## 4. `convert_threads` 기본값 조정 (4 → 2) 및 실측 수치

합성 2304×1296 H.264 스트림(`live_video.encode`, `yuv420p`, GOP=10)을 사용해 (1) 단일 프레임 디코딩/변환 지연과 (2) 4카메라 × 25 fps(40 ms 간격, 1초간 100장 수신) + 10 Hz `next_batch()`(100 ms 간격, 1초간 40장 추론, 60장 `SUPERSEDED`) 구간의 전후 비용을 측정했다.

### 4.1 2304×1296 단일 프레임 단계별 지연 (100장 반복)

| 단계 | `convert_threads` | p50 (ms) | p95 (ms) | 비고 |
|---|---:|---:|---:|---|
| H.264 `ctx.decode` (YUV420P) | - | 2.33 | 3.56 | 참조 프레임 유지를 위해 모든 프레임 수행 |
| `VideoReformatter.reformat` + `to_ndarray` (BGR24) | 1 | 3.86 | 4.71 | 스레드 1개 |
| `VideoReformatter.reformat` + `to_ndarray` (BGR24) | **2 (기본값)** | **2.52** | **3.11** | `threads=1→4` 단축분의 63% 확보, 스레드 수 절반 |
| `VideoReformatter.reformat` + `to_ndarray` (BGR24) | 4 (이전 기본값) | 1.74 | 2.25 | 4대 합계 16 스레드 |

### 4.2 4카메라 × 25 fps 수신 + 10 Hz `next_batch()` (1초, 2304×1296)

| 구성 | 디코딩 프레임 | BGR 변환 프레임 | 초당 BGR 할당량 | 벽시계 시간 (ms) | 프로세스 CPU 시간 (ms) |
|---|---:|---:|---:|---:|---:|
| 변경 전 (즉시 변환, `threads=4`) | 100 | 100 | 895.80 MB (854.30 MiB) | 426.4 | 1093.8 |
| 변경 후 (지연 변환, `threads=4`) | 100 | 40 (-60%) | 358.32 MB (341.72 MiB) | 232.1 | 687.5 |
| **변경 후 (지연 변환, `threads=2`, 기본값)** | **100** | **40 (-60%)** | **358.32 MB (341.72 MiB)** | **237.2 (-44.4%)** | **546.9 (-50.0%)** |
| 변경 후 (지연 변환, `threads=1`) | 100 | 40 (-60%) | 358.32 MB (341.72 MiB) | 250.3 | 531.2 |

- 지연 변환(`-386`)으로 초당 BGR 변환 횟수가 `100 → 40회`(`-60.0%`), 초당 BGR `ndarray` 할당량이 `895.80 MB/s → 358.32 MB/s`(`-537.48 MB/s`)로 줄었다.
- `DEFAULT_CONVERT_THREADS`를 `4 → 2`로 낮추면서 4대 카메라의 `swscale` 워커 스레드 총합이 `16 → 8`개로 절반이 되었고, 지연 변환 상태에서 `threads=4` 대비 벽시계 차이는 10 배치 합계 `5.1 ms`(배치당 `0.5 ms`)에 불과한 반면 프로세스 총 CPU 시간은 `687.5 ms → 546.9 ms`(변경 전 `1093.8 ms` 대비 `-50.0%`)로 줄었다.

## 5. 남은 위험과 한계

- **대기열 내 원본 YUV 프레임 보유**: `LatestFrameScheduler`에 머무는 카메라당 최신 프레임(`queue_depth=1~2`장)은 `take()`로 꺼내거나 새 프레임에 밀려날 때까지 `av.VideoFrame`(2304×1296 YUV420P 기준 약 `4.48 MB`)을 보유한다. 이는 기존에 대기열이 BGR24 배열(`8.96 MB`)을 보유하던 것보다 작으며, `SUPERSEDED`로 밀려나면 즉시 참조가 해제된다.
- **변환 시점 이동**: BGR 색 변환이 수신 스레드(`offer`)에서 추론 인계 시점(`take`/`take_one`, 대기열 잠금 밖)으로 이동하므로 `next_batch()`를 호출하는 스레드가 카메라당 선택된 1장(최대 4장, 2304×1296 기준 합계 약 `10 ms`)의 변환을 수행한다. `-383` 검출 루프 분리와 결합되면 이 변환은 10 Hz 판정 루프가 아니라 검출 스레드에서만 수행된다.
- **실제 GPU Pod 재측정 필요**: 위 수치는 합성 2304×1296 H.264 기준이며, 시연 Pod(NVDEC 또는 CPU 디코딩 + TensorRT FP16 YOLO26x-pose)에서의 전체 `tick_ms` p95와 RSS는 `-383` 결합 후 LIVE 환경에서 함께 확인해야 한다.
