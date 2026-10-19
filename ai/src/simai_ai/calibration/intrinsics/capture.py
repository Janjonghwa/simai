"""내부 파라미터 추정 입력: 카메라 RTSP·영상 파일에서 프레임을 메모리로만 받아 시간 중앙값 배경을 만든다.

- **디스크에 쓰지 않는다.** 실제 카메라 영상에는 수업 중인 사람이 찍힌다(명세 11.5 원본 영상 비저장). 프레임은 PyAV 가
  디코딩한 numpy 배열로만 들고, 배경을 만든 뒤 버린다. 이 모듈은 파일을 만들지 않는다(임시 폴더 포함).
- **시간 중앙값 배경.** `duration_s` 동안 `interval_s` 간격으로 프레임을 골라(최대 `max_frames`) 화소별 중앙값을 낸다.
  지나가는 사람은 지워지지만, 시간의 절반 넘게 같은 자리에 있는 사람(앉아 있는 학생)은 남는다. 남은 사람의 윤곽은
  곡선이라 고전 기준은 이상치 사슬로 빼고, 학습 모델은 장면 전체에서 추정한다.
- **간격은 영상 시각(pts) 기준이다.** 파일은 실시간보다 빠르게 디코딩되므로 벽시계로 고르면 간격이 무너진다. pts 가
  없으면 주입한 단조 시계로 잰다. 전체 벽시계 상한(`wall_timeout_s`)을 넘기면 멈춘다(RTSP 정지 대비).
- **모르면 비운다.** 열기·디코딩에 실패했거나, 고른 프레임이 `min_frames` 보다 적거나, 영상 크기가 중간에 바뀌면
  배경을 만들지 않고 status FAILED 와 사유를 남긴다.
- **계정.** RTSP 계정은 `RtspCredentials` 로만 받고 URL 을 만들 때만 쓴다. PyAV 예외 메시지에는 계정이 든 URL 이
  들어 있으므로 예외 문자열을 쓰지 않고 예외 종류만 남긴다(`from None`). 기록·로그에는 계정 없는 설명만 남긴다.
- RTSP 는 TCP 로 연다(gateway/recorder 와 같은 옵션). 카메라당 세션 하나만 연다(명세 3.3).
- **파일 입력은 로컬 일반 파일만.** 경로 문자열을 FFmpeg 에 넘기지 않고 파이썬 바이너리 핸들로 넘긴다. FFmpeg 의
  프로토콜 접두어(`cache:file:…` 는 읽은 영상 바이트를 작업 폴더의 `ffcache*` 에 쓴다) 해석을 막기 위해서다.
- **RTSP host 는 호스트 이름·IP 만.** 계정(`user:pass@`)·포트·경로·공백이 섞인 host 는 값을 보여 주지 않고 거부한다
  (repr·기록·로그에 남지 않게, `CameraInput` 생성 경계).
- **벽시계 상한의 범위.** 상한은 입력을 연 뒤부터 재고, 디코더가 프레임을 하나 내놓을 때마다 검사한다. 열기 시간은
  빠지고, 프레임이 오지 않는 동안은 PyAV 의 open·read timeout(`open_timeout_s`·`read_timeout_s`)이 막는다. 그래서 실제
  종료는 상한 + 프레임 하나 기다리는 시간(최대 read timeout)까지 늦을 수 있다.
"""

from __future__ import annotations

import re
import time
import urllib.parse
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

CAPTURE_VERSION = "0.1.0"

SOURCE_RTSP = "RTSP"
SOURCE_FILE = "FILE"

CAPTURE_OK = "OK"

# RTSP host: 호스트 이름·IPv4(영문·숫자·.·-) 또는 [IPv6]. 계정(@)·포트(:)·경로(/)·공백은 받지 않는다
_HOST_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?|\[[0-9A-Fa-f:.]+\]")
_PATH_RE = re.compile(r"[A-Za-z0-9._~/-]*")
CAPTURE_FAILED = "FAILED"


@dataclass(frozen=True)
class CaptureConfig:
    duration_s: float = 45.0
    interval_s: float = 3.0
    min_frames: int = 5
    # 메모리 상한: 2304x1296 BGR 한 장이 약 9 MB 다
    max_frames: int = 30
    open_timeout_s: float = 10.0
    read_timeout_s: float = 10.0
    # 벽시계 상한(초). None 이면 duration_s + 30
    wall_timeout_s: float | None = None

    def __post_init__(self) -> None:
        if self.duration_s <= 0 or self.interval_s <= 0:
            raise ValueError("duration_s·interval_s 는 양수다")
        if self.min_frames < 1 or self.max_frames < self.min_frames:
            raise ValueError("1 <= min_frames <= max_frames 여야 한다")

    def to_record(self) -> dict[str, Any]:
        return {
            "duration_s": self.duration_s,
            "interval_s": self.interval_s,
            "min_frames": self.min_frames,
            "max_frames": self.max_frames,
        }


@dataclass(frozen=True)
class RtspCredentials:
    """RTSP 계정. repr 에 값을 내지 않는다."""

    user: str = field(repr=False)
    password: str = field(repr=False)

    def __repr__(self) -> str:
        return "RtspCredentials(***)"


@dataclass(frozen=True)
class CameraInput:
    """카메라 한 대의 입력. RTSP(host·port·path) 또는 영상 파일(file)."""

    camera_id: str
    kind: str
    host: str | None = None
    port: int = 554
    path: str = "stream1"
    file: Path | None = None

    def __post_init__(self) -> None:
        if self.kind == SOURCE_RTSP:
            if not self.host:
                raise ValueError(f"{self.camera_id}: RTSP 입력에 host 가 없다")
            # 값은 메시지에 넣지 않는다(계정이 들어 있을 수 있다)
            if not isinstance(self.host, str) or not _HOST_RE.fullmatch(self.host):
                raise ValueError(
                    f"{self.camera_id}: RTSP host 는 호스트 이름이나 IP 만 쓴다(계정·포트·경로·공백 금지, 값은 표시하지 않음). "
                    "계정은 .env 의 RTSP_USER/RTSP_PASS 에 둔다"
                )
            if not _PATH_RE.fullmatch(self.path):
                raise ValueError(f"{self.camera_id}: RTSP path 에 쓸 수 없는 문자가 있다(@·공백 등, 값은 표시하지 않음)")
            if not (0 < int(self.port) < 65536):
                raise ValueError(f"{self.camera_id}: RTSP port 범위가 잘못됐다")
        elif self.kind == SOURCE_FILE:
            if self.file is None:
                raise ValueError(f"{self.camera_id}: 파일 입력에 file 이 없다")
        else:
            raise ValueError(f"{self.camera_id}: kind 는 {SOURCE_RTSP} 또는 {SOURCE_FILE}: {self.kind!r}")

    def url(self, credentials: RtspCredentials | None) -> str:
        """열 주소. RTSP 는 계정을 넣는다(이 값은 기록·로그·예외에 쓰지 않는다)."""
        if self.kind == SOURCE_FILE:
            return str(self.file)
        auth = ""
        if credentials is not None:
            user = urllib.parse.quote(credentials.user, safe="")
            auth = f"{user}:{urllib.parse.quote(credentials.password, safe='')}@"
        return f"rtsp://{auth}{self.host}:{self.port}/{self.path.lstrip('/')}"

    def describe(self) -> dict[str, Any]:
        """기록에 남길 입력 설명(계정 없음)."""
        if self.kind == SOURCE_FILE:
            assert self.file is not None
            return {"kind": SOURCE_FILE, "file": self.file.name}
        return {"kind": SOURCE_RTSP, "host": self.host, "port": self.port, "path": self.path}


@dataclass
class CaptureResult:
    """카메라 한 대의 수집 결과. background 는 메모리에만 있다(기록에 넣지 않는다)."""

    camera_id: str
    source: dict[str, Any]
    status: str
    frames_used: int = 0
    frames_decoded: int = 0
    first_sample_s: float | None = None
    last_sample_s: float | None = None
    image_size: tuple[int, int] | None = None
    timing: str = "pts"
    reasons: list[str] = field(default_factory=list)
    background: np.ndarray | None = field(default=None, repr=False)

    def to_record(self) -> dict[str, Any]:
        rec: dict[str, Any] = {
            "capture_version": CAPTURE_VERSION,
            "status": self.status,
            "frames_used": self.frames_used,
            "frames_decoded": self.frames_decoded,
            "timing": self.timing,
        }
        if self.first_sample_s is not None and self.last_sample_s is not None:
            rec["sample_span_s"] = [round(self.first_sample_s, 3), round(self.last_sample_s, 3)]
        if self.image_size is not None:
            rec["image_size"] = list(self.image_size)
        if self.reasons:
            rec["reasons"] = list(self.reasons)
        return rec


Opener = Callable[[str, dict[str, str], tuple[float, float]], Any]


def _open_rtsp(url: str, options: dict[str, str], timeout: tuple[float, float]) -> Any:
    import av

    if not url.startswith("rtsp://"):
        raise ValueError("RTSP 주소가 아니다")
    return av.open(url, options=options, timeout=timeout)


class _FileContainer:
    """파이썬 파일 핸들로 연 PyAV 컨테이너. 닫을 때 핸들도 닫는다."""

    def __init__(self, container: Any, handle: Any) -> None:
        self._container = container
        self._handle = handle

    def decode(self, *args: Any, **kwargs: Any) -> Any:
        return self._container.decode(*args, **kwargs)

    def close(self) -> None:
        try:
            self._container.close()
        finally:
            self._handle.close()


def _open_file(url: str, options: dict[str, str], timeout: tuple[float, float]) -> Any:
    """로컬 파일만 연다. 경로 문자열을 FFmpeg 에 넘기지 않고 파이썬 바이너리 핸들로 넘긴다.

    FFmpeg 는 경로 문자열의 프로토콜 접두어를 해석한다(`cache:` 는 읽은 영상 바이트를 작업 폴더의 `ffcache*` 에 쓴다).
    핸들로 넘기면 프로토콜 해석이 일어나지 않는다. 일반 파일이 아니면(프로토콜 문자열, 폴더, 장치) 열지 않는다.
    """
    import av

    path = Path(url)
    if not path.is_file():
        raise FileNotFoundError("로컬 일반 파일이 아니다")
    handle = path.open("rb")
    try:
        container = av.open(handle, mode="r", options=options)
    except BaseException:
        handle.close()
        raise
    return _FileContainer(container, handle)


def capture_background(
    camera: CameraInput,
    config: CaptureConfig,
    *,
    credentials: RtspCredentials | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    opener: Opener | None = None,
) -> CaptureResult:
    """입력을 열어 프레임을 골라 받고 시간 중앙값 배경을 만든다. 예외를 내지 않고 결과의 status·reasons 로 알린다."""
    result = CaptureResult(camera.camera_id, camera.describe(), CAPTURE_FAILED)
    open_ = opener or (_open_rtsp if camera.kind == SOURCE_RTSP else _open_file)
    options = {"rtsp_transport": "tcp"} if camera.kind == SOURCE_RTSP else {}
    wall_limit = config.wall_timeout_s if config.wall_timeout_s is not None else config.duration_s + 30.0
    try:
        container = open_(camera.url(credentials), options, (config.open_timeout_s, config.read_timeout_s))
    except Exception as e:  # noqa: BLE001 - 메시지에 계정이 든 URL 이 있다. 종류만 남긴다
        result.reasons.append(f"입력을 열지 못했다({type(e).__name__})")
        return result
    frames: list[np.ndarray] = []
    wall_start = monotonic()
    try:
        fatal = _sample(container, config, result, frames, monotonic, wall_start, wall_limit)
    finally:
        try:
            container.close()
        except Exception:  # noqa: BLE001, S110 - 닫기 실패는 결과에 영향이 없다(메시지에 계정 URL 이 있을 수 있어 남기지 않는다)
            pass
    if fatal:
        return result
    if len(frames) < config.min_frames:
        result.status = CAPTURE_FAILED
        result.reasons.append(f"고른 프레임 {len(frames)}장이 최소 {config.min_frames}장보다 적다")
        return result
    result.background = temporal_median(frames)
    result.status = CAPTURE_OK
    return result


def _sample(
    container: Any,
    config: CaptureConfig,
    result: CaptureResult,
    frames: list[np.ndarray],
    monotonic: Callable[[], float],
    wall_start: float,
    wall_limit: float,
) -> bool:
    """프레임을 골라 frames 에 넣는다. 배경을 만들 수 없는 실패면 True(사유는 result.reasons)."""
    t0: float | None = None
    next_t = 0.0
    size: tuple[int, int] | None = None
    try:
        for frame in container.decode(video=0):
            result.frames_decoded += 1
            if monotonic() - wall_start > wall_limit:
                result.reasons.append(f"벽시계 상한 {wall_limit:.0f}s 를 넘겨 멈췄다")
                break
            t_frame = getattr(frame, "time", None)
            if t_frame is None:
                result.timing = "monotonic"
                t_frame = monotonic() - wall_start
            if t0 is None:
                t0 = float(t_frame)
            rel = float(t_frame) - t0
            if rel >= config.duration_s:
                break
            if rel + 1e-9 < next_t:
                continue
            image = frame.to_ndarray(format="bgr24")
            h, w = image.shape[:2]
            if size is None:
                size = (int(w), int(h))
                result.image_size = size
            elif size != (w, h):
                result.reasons.append(f"영상 크기가 {size} 에서 {(w, h)} 로 바뀌었다")
                frames.clear()
                result.frames_used = 0
                return True
            frames.append(image)
            result.frames_used = len(frames)
            if result.first_sample_s is None:
                result.first_sample_s = rel
            result.last_sample_s = rel
            next_t = max(next_t + config.interval_s, rel + config.interval_s * 0.5)
            if len(frames) >= config.max_frames:
                break
    except Exception as e:  # noqa: BLE001 - 디코딩·수신 오류. 메시지에 계정이 든 URL 이 있을 수 있어 종류만 남긴다
        note = f"디코딩·수신이 끊겼다({type(e).__name__})"
        if len(frames) < config.min_frames:
            result.reasons.append(note)
            return True
        result.reasons.append(note + ". 그 전까지 고른 프레임으로 배경을 만든다")
    return False


def temporal_median(frames: Sequence[np.ndarray], rows_per_chunk: int = 64) -> np.ndarray:
    """같은 크기 프레임들의 화소별 중앙값(입력 dtype 으로 반올림). 메모리를 줄이려고 행 묶음으로 계산한다."""
    if not frames:
        raise ValueError("프레임이 없다")
    first = np.asarray(frames[0])
    out = np.empty_like(first)
    h = first.shape[0]
    for r0 in range(0, h, rows_per_chunk):
        block = np.stack([np.asarray(f)[r0 : r0 + rows_per_chunk] for f in frames])
        med = np.median(block, axis=0)
        if np.issubdtype(first.dtype, np.integer):
            info = np.iinfo(first.dtype)
            med = np.clip(np.rint(med), info.min, info.max)
        out[r0 : r0 + rows_per_chunk] = med.astype(first.dtype)
    return out
