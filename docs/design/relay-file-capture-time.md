# relay 파일 재생의 촬영 시각과 불확실성 — S15P31A101-353

## 명세와 원인

- 관련 명세: §3.9 촬영 시각·불확실성·25 ms 삼각측량 문턱, §12.7 I0 원본 PTS·수신 시각 보존.
- `ai/src/simai_ai/tracking/cross_view.py` 는 `simai_ai.geometry.interpolation.InterpolationConfig` 를 가져온다.
  정본 기본값은 `ai/src/simai_ai/geometry/interpolation.py` 의 `DEFAULT_MAX_TIMESTAMP_UNCERTAINTY_MS = 25.0` 이다.
- PTS 없는 raw H.264 에 공용 `CaptureTimeModel.uncertainty_no_pts_ms = 45` 가 적용되어 모든 관측이 문턱을 넘었다.
- RTSP 의 §3.9 PTS 없음 규칙은 그대로 따른다. 파일 재생은 실제 노출 시각 복원 대신 페이싱된 재생 시각을 FILE 출처로 표시한다.
  공용 FrameClock·추정기·계약은 수정하지 않는다.

## 결정과 수치

`gateway/recorder/src/simai_recorder/sources.py` 의 `FileSource.read()` 는 `_pace()` 뒤 반환한다.
PTS 가 있으면 PTS 간격, 없으면 설정 fps → 파일 공칭 fps → 25 fps 순으로 페이싱한다.
relay 는 반환 직후 현재 UTC ms 를 읽는다. 따라서 기존 ReceiveTimeEstimator 의 PTS 없음 식
`round(received_at_ms − latency_ms)` 을 그대로 써도 페이싱 시각 기반 시각이다. 원본 PTS 는 null 로 남긴다.

relay 의 설정 로더만 파일 기본 출처를 FILE 로, 실시간 파일의 PTS 없음 불확실성을 20 ms 로 정한다.
20 ms 는 정상 PTS 경로와 같은 잠정 여유이면서 Tier 0 기본 문턱 25 ms 미만이다. 8차 GPU 세션에서
설정을 20 ms 로 바꾸자 트랙이 생겼다는 지시서의 관측과도 맞는다(이 작업에서 GPU 재검증은 하지 않음).

2026-10-30 현 노트북 SystemClock 측정: `uv run --offline python -` 로 시험용 막대 무늬 raw H.264
10프레임 파일을 만들어 FileSource(realtime=True, loop=True, fps=25) 로 100프레임(10회 순환)을 읽었다.
매번 monotonic 을 읽어 `abs((t_i − t_0) × 1000 − i × 40)` 를 계산했다.

| 지표 | 결과 |
|---|---|
| 페이싱 절대 오차 중앙값 / p95 / 최대 | 5 / 14 / 15 ms |
| 수신 간격 중앙값 | 46 ms |
| 원본 PTS | 100프레임 전부 None |

20 ms 는 이 측정의 최대 오차 15 ms 를 덮으며, 25 fps 의 반 프레임 간격이기도 하다.
순간 간격은 OS 스케줄러 때문에 정확히 40 ms 가 아니므로 설정 fps 만으로 0 ms 를 주장하지 않는다.
단일 실행의 페이싱 점검이며 제품 촬영 성능 지표나 부하 환경의 보장은 아니다.

기본값 < 공통 `[capture_time]` < 카메라별 `capture_time` 순으로 덮는다.
`realtime = false` 는 페이싱 근거가 없어 기존 45 ms 를 유지한다. RTSP·합성 입력의 모델은 바뀌지 않는다.
직전 시각 +1 ms 올림과 그만큼 불확실성 증가도 공용 추정기를 그대로 사용한다.

## 코드와 시험

- 구현: `gateway/relay/src/simai_relay/config.py::load_cameras`.
- 회귀: `gateway/relay/tests/test_file_capture_time.py`. 설정부터 실제 raw 파일 페이싱·세 번 순환·I0 헤더까지 검사한다.
  수정 전 같은 시험이 `45.0 != 20.0` 으로 실패했다.
- 기본값·공통/카메라 덮어쓰기·명시 45 ms 존중·비실시간 45 ms·RTSP PTS 있음 20 ms/없음 45 ms 를 검사한다.
- 명령: `gateway/relay` 에서 `uv run --offline pytest -q -p no:cacheprovider`.
  실제 결과는 Owner 보고 `.agy/notes.md` 에 남긴다. 리뷰 생략(사용자 지시).

## 버린 대안과 남은 위험

- 공용 FrameClock/모델의 기본 45 ms 를 내리는 방식은 RTSP 의 Wi-Fi 지터 근거까지 바꿔 제외했다.
- 가짜 PTS 나 별도 프레임 시계를 추가하는 방식은 이미 있는 FileSource 페이싱과 중복이어서 제외했다.
- 높은 CPU 부하·다른 fps·파일 손상·카메라별 시작 오프셋·녹화 원본 동기화는 이 점검으로 보장하지 않는다.
  필요하면 불확실성을 설정으로 키워야 하며, 25 ms 를 넘으면 연관에서 제외되는 것이 정상이다.
- GPU Tier 0 의 실제 3D 트랙 생성 재확인은 Lead 의 현장 확인으로 넘긴다. 네트워크·GPU 실행은 하지 않았다.
