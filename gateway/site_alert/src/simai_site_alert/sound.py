"""소리: 등급별 반복 패턴과 소리 백엔드.

백엔드는 주입한다. 코어는 `start(pattern)`(반복 시작, 바로 돌아옴)과 `stop()`만 부른다.
- WinSoundBackend: Windows 표준 라이브러리(winsound.PlaySound 비동기 반복 + winmm 출력 장치 수 확인). 패턴마다 WAV 를
  시작할 때 한 번 만들어 두고, 경보 때는 파일을 비동기 반복 재생만 걸어 수신 → 소리 시작 지연을 줄인다.
- FakeSoundBackend: 시험·벤치용. 호출을 기록하고, 실패를 흉내 낼 수 있다.
- NullSoundBackend: 소리 장치가 없는 환경. 항상 실패하므로 화면에 '소리 장치 오류'가 크게 뜬다(조용히 넘어가지 않음).
"""

from __future__ import annotations

import array
import math
import sys
import tempfile
import threading
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class SoundError(RuntimeError):
    """소리를 낼 수 없다(장치 없음, 재생 실패). 화면에 크게 알린다."""


@dataclass(frozen=True)
class SoundPattern:
    """한 주기의 (주파수 Hz, 길이 ms) 목록. 주파수 0 은 쉼. 주기를 끝없이 반복한다."""

    name: str
    tones: tuple[tuple[int, int], ...]
    repeat: bool = True

    @property
    def period_ms(self) -> int:
        return sum(d for _, d in self.tones)


# 등급별 소리 패턴 (명세 7.3: 등급별로 소리 패턴을 달리할 수 있다). 시험음은 실제 경보와 구별되게 낮고 느리다.
CRITICAL = SoundPattern("CRITICAL", ((1400, 160), (0, 40), (1000, 160), (0, 40)) * 3 + ((0, 200),))
HIGH = SoundPattern("HIGH", ((880, 300), (0, 200), (880, 300), (0, 700)))
TEST = SoundPattern("TEST", ((660, 250), (0, 250), (660, 250), (0, 1250)))
HIGH_ONCE = SoundPattern("HIGH_ONCE", HIGH.tones, repeat=False)
CRITICAL_ONCE = SoundPattern("CRITICAL_ONCE", CRITICAL.tones, repeat=False)
PATTERNS = {p.name: p for p in (CRITICAL, HIGH, TEST, HIGH_ONCE, CRITICAL_ONCE)}


def pattern_for(severity: str, once: bool = False) -> SoundPattern:
    if once:
        return CRITICAL_ONCE if severity == "CRITICAL" else HIGH_ONCE
    return CRITICAL if severity == "CRITICAL" else HIGH


class SoundBackend(Protocol):
    def check(self) -> None:
        """출력 장치를 쓸 수 있는지. 못 쓰면 SoundError."""

    def start(self, pattern: SoundPattern) -> None:
        """pattern 을 끝없이 반복 재생 시작(이미 재생 중이면 바꾼다). 바로 돌아온다. 실패하면 SoundError."""

    def stop(self) -> None:
        """재생을 멈춘다. 실패하면 SoundError."""


def write_wav(path: Path, pattern: SoundPattern, rate: int = 22_050, volume: float = 0.6) -> None:
    """패턴 한 주기를 16bit 모노 WAV 로 쓴다. 음 앞뒤 5ms 를 부드럽게 올리고 내려 딸깍 소리를 없앤다."""
    samples = array.array("h")
    for freq, dur_ms in pattern.tones:
        n = rate * dur_ms // 1000
        if freq <= 0:
            samples.extend([0] * n)
            continue
        ramp = max(1, rate * 5 // 1000)
        for i in range(n):
            env = min(1.0, i / ramp, (n - 1 - i) / ramp)
            samples.append(int(32767 * volume * env * math.sin(2 * math.pi * freq * i / rate)))
    if sys.byteorder != "little":
        samples.byteswap()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())


class WinSoundBackend:
    """Windows 표준 라이브러리 소리. PlaySound(SND_ASYNC|SND_LOOP) 로 반복하고 PlaySound(None) 으로 멈춘다."""

    def __init__(self, wav_dir: Path | None = None) -> None:
        self._dir = wav_dir or Path(tempfile.mkdtemp(prefix="simai-site-alert-"))
        self._files: dict[str, Path] = {}
        for p in PATTERNS.values():
            path = self._dir / f"{p.name.lower()}.wav"
            write_wav(path, p)
            self._files[p.name] = path

    @staticmethod
    def _winsound():
        if sys.platform != "win32":
            raise SoundError("Windows 가 아니라 winsound 를 쓸 수 없다")
        import winsound

        return winsound

    def check(self) -> None:
        self._winsound()
        import ctypes

        try:
            devices = ctypes.windll.winmm.waveOutGetNumDevs()
        except OSError as e:  # winmm 을 못 읽음
            raise SoundError(f"winmm 을 읽지 못했다: {e}") from e
        if devices == 0:
            raise SoundError("출력 오디오 장치가 없다")

    def start(self, pattern: SoundPattern) -> None:
        ws = self._winsound()
        path = self._files.get(pattern.name)
        if path is None:
            path = self._dir / f"{pattern.name.lower()}.wav"
            write_wav(path, pattern)
            self._files[pattern.name] = path
        try:
            ws.PlaySound(str(path), ws.SND_FILENAME | ws.SND_ASYNC | (ws.SND_LOOP if pattern.repeat else 0) | ws.SND_NODEFAULT)
        except RuntimeError as e:
            raise SoundError(f"소리 재생 실패: {e}") from e

    def stop(self) -> None:
        ws = self._winsound()
        try:
            ws.PlaySound(None, 0)
        except RuntimeError as e:
            raise SoundError(f"소리 정지 실패: {e}") from e


class NullSoundBackend:
    """소리 장치 없음. 모든 호출이 실패해 화면에 '소리 장치 오류'를 띄운다."""

    def __init__(self, reason: str = "소리 백엔드가 없다") -> None:
        self.reason = reason

    def check(self) -> None:
        raise SoundError(self.reason)

    def start(self, pattern: SoundPattern) -> None:
        raise SoundError(self.reason)

    def stop(self) -> None:
        pass


class FakeSoundBackend:
    """시험·벤치용. calls 에 ("start", 패턴 이름) / ("stop", None) 을 남긴다. fail_* 로 실패를 흉내 낸다."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.playing: SoundPattern | None = None
        self.fail_check = False
        self.fail_start = False
        self._lock = threading.Lock()

    def check(self) -> None:
        if self.fail_check:
            raise SoundError("흉내 낸 장치 없음")

    def start(self, pattern: SoundPattern) -> None:
        with self._lock:
            if self.fail_start:
                raise SoundError("흉내 낸 재생 실패")
            self.calls.append(("start", pattern.name))
            self.playing = pattern

    def stop(self) -> None:
        with self._lock:
            self.calls.append(("stop", None))
            self.playing = None
