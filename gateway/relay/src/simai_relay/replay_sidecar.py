"""녹화 사이드카 촬영 시각 재생 (S15P31A101-721, docs/design/S15P31A101-721-replay-test-path.md).

녹화 도구(simai_recorder)는 프레임마다 사이드카 `<이름>.frames.jsonl` 한 줄을 남긴다:
`{"header": {I0 frame_header}, "offset": …, "size": …}`. header 의 `estimated_capture_time_ms`·`timestamp_uncertainty_ms` 가
그 카메라가 실제로 찍은 시각(추정)과 그 불확실성이다.

파일 재생을 설정 fps 로 고르게 내보내면 실제 카메라(24.94~24.98 fps)와 속도가 달라 카메라 사이 촬영 시각이 10분에 0.6~0.8 s
어긋난다(17차). 사이드카 모드(`timing = "sidecar"`)는 프레임 k 를 **네 카메라가 같이 쓰는 재생 시각축**에서

    재생 시각 = anchor + 회차 × period + offset[k]      offset[k] = 사이드카 촬영 시각[k] − 공통 기준(네 카메라 첫 촬영 시각 중 최소)

에 내보내고, 그 시각을 그대로 capture_time 으로 쓴다(불확실성은 녹화 원본 값). anchor 는 네 카메라가 함께 쓰는 하나의 벽시계·
단조 시계 쌍이고, period 는 네 카메라 공통 순환 길이(가장 긴 카메라의 마지막 offset + 프레임 간격)라 순환해도 정렬이 유지된다.
송신 지연(잠·스레드 지터)은 capture_time 에 들지 않는다(예정 시각을 쓴다). 원본 절대 시각과의 대응은 기동 로그
`replay_anchor` 한 줄(anchor_wall_ms ↔ recording_ref_ms)로 남긴다(I0 헤더 형식은 바꾸지 않는다).

잘라 붙인 파일은 `simai-relay sidecar-cut` 으로 같은 촬영 시각 구간의 사이드카를 만든다. 각 줄에 `replay_offset_ms`(구간을 이어 붙인
재생 시각축, 구간 경계가 네 카메라 공통이라 정렬이 유지됨)를 더하고, 재생은 그 값을 offset 으로 쓴다.

사이드카보다 영상 프레임이 많으면 남는 프레임은 촬영 시각을 모르므로 보내지 않는다(AGENTS 규칙 2). 개수가 어긋나면 WARNING 한 번.
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import threading
from array import array
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from simai_recorder.clock import Clock
from simai_recorder.segment import FRAMES_SUFFIX
from simai_recorder.sources import FileSource, SourcePacket, StreamInfo

log = logging.getLogger("simai_relay")

ANCHOR_LEAD_MS = 500  # 네 카메라가 파일을 여는 시간(수십 ms)보다 넉넉히. 늦게 연 카메라가 앞 프레임을 몰아 보내지 않게
TIER0_MAX_TIMESTAMP_UNCERTAINTY_MS = 25.0  # ai/src/simai_ai/geometry/interpolation.py DEFAULT_MAX_TIMESTAMP_UNCERTAINTY_MS 와 같다


class SidecarError(ValueError):
    """사이드카를 읽을 수 없거나 재생에 쓸 수 없다."""


@dataclass(frozen=True)
class Sidecar:
    """한 카메라 사이드카. capture_ms 는 원본 절대 촬영 시각(UTC ms), replay_offset_ms 는 sidecar-cut 이 더한 값(없으면 None)."""

    capture_ms: array
    uncertainty_ms: array
    replay_offset_ms: array | None

    def __len__(self) -> int:
        return len(self.capture_ms)

    def interval_ms(self) -> float:
        """프레임 간격 중앙값(순환 길이 끝에 한 칸 더할 때 쓴다). 한 장뿐이면 40 ms(25 fps)."""
        src = self.replay_offset_ms if self.replay_offset_ms is not None else self.capture_ms
        if len(src) < 2:
            return 40.0
        return float(statistics.median(b - a for a, b in pairwise(src)))


def default_sidecar_paths(parts: Sequence[Path]) -> list[Path]:
    """영상 파일(회전한 part 포함) 옆의 같은 이름 사이드카: cam_A.h264 → cam_A.frames.jsonl."""
    return [p.with_name(p.stem + FRAMES_SUFFIX) for p in parts]


def load_sidecar(paths: Sequence[Path]) -> Sidecar:
    """사이드카 파일(들)을 순서대로 이어 읽는다. 촬영 시각(또는 replay_offset_ms)이 앞으로만 가야 한다."""
    capture, unc, offsets = array("q"), array("d"), array("q")
    with_offset = without_offset = 0
    for path in paths:
        try:
            fh = path.open(encoding="utf-8")
        except OSError as e:
            raise SidecarError(f"사이드카를 열 수 없다 {path}: {type(e).__name__}") from None
        with fh:
            for n, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    h = row["header"]
                    capture.append(int(h["estimated_capture_time_ms"]))
                    unc.append(float(h["timestamp_uncertainty_ms"]))
                except (ValueError, KeyError, TypeError):
                    raise SidecarError(f"사이드카 줄을 읽을 수 없다 {path}:{n}") from None
                if "replay_offset_ms" in row:
                    offsets.append(int(row["replay_offset_ms"]))
                    with_offset += 1
                else:
                    without_offset += 1
    if not capture:
        raise SidecarError(f"사이드카가 비었다 {', '.join(map(str, paths))}")
    if with_offset and without_offset:
        raise SidecarError("replay_offset_ms 가 있는 줄과 없는 줄이 섞였다 (sidecar-cut 결과를 다른 사이드카와 잇지 않는다)")
    timeline = offsets if with_offset else capture
    for i in range(1, len(timeline)):
        if timeline[i] <= timeline[i - 1]:
            raise SidecarError(f"사이드카 시각이 앞으로 가지 않는다 (줄 {i + 1}: {timeline[i - 1]} → {timeline[i]})")
    return Sidecar(capture, unc, offsets if with_offset else None)


class ReplayAnchor:
    """네 카메라가 같이 쓰는 재생 원점. 처음 부른 카메라가 지금 + ANCHOR_LEAD_MS 로 정하고 나머지는 같은 값을 받는다."""

    def __init__(self, clock: Clock, period_ms: int, recording_ref_ms: int | None, lead_ms: int = ANCHOR_LEAD_MS) -> None:
        self._clock = clock
        self.period_ms = period_ms
        self._ref = recording_ref_ms
        self._lead_ms = lead_ms
        self._lock = threading.Lock()
        self._value: tuple[float, int] | None = None

    def get(self) -> tuple[float, int]:
        """(단조 시계 초, 벽시계 UTC ms) — 재생 offset 0 의 두 시계 값. 정한 뒤에는 잠그지 않는다(카메라 사이 대기 없음)."""
        value = self._value
        if value is not None:
            return value
        with self._lock:
            if self._value is None:
                self._value = (self._clock.monotonic() + self._lead_ms / 1000, self._clock.now_ms() + self._lead_ms)
                log.info("replay_anchor anchor_wall_ms=%d recording_ref_ms=%s period_ms=%d", self._value[1],
                         self._ref if self._ref is not None else "cut", self.period_ms)
            return self._value


@dataclass(frozen=True)
class ReplayPacket(SourcePacket):
    """사이드카 재생 패킷. relay 는 추정기 대신 이 촬영 시각·불확실성을 헤더에 쓴다."""

    capture_time_ms: int = 0
    uncertainty_ms: float = 0.0


class SidecarFileSource(FileSource):
    """FileSource 를 사이드카 촬영 시각 재생 시각축으로 내보낸다(모듈 설명). 페이싱은 이 클래스가 한다(부모는 realtime=False)."""

    def __init__(self, path: Path | str, clock: Clock, sidecar: Sidecar, offsets_ms: array, anchor: ReplayAnchor,
                 realtime: bool = True, loop: bool = False, fps: float | None = None, camera_id: str = "") -> None:
        super().__init__(path, clock, realtime=False, loop=loop, fps=fps)
        self._sidecar = sidecar
        self._offsets = offsets_ms
        self._anchor = anchor
        self._paced_realtime = realtime
        self._camera_id = camera_id
        self._pass = 0
        self._k = 0  # 이번 회차에서 읽은 영상 프레임 번호
        self._opened = False
        self._warned_extra = False
        self._warned_short = False

    def _open_container(self) -> None:
        if self._opened and self._part_index == 0:  # 순환으로 처음 part 로 돌아왔다
            self._check_short()
            self._pass += 1
            self._k = 0
        self._opened = True
        super()._open_container()

    def _check_short(self) -> None:
        if self._k < len(self._offsets) and not self._warned_short:
            self._warned_short = True
            log.warning("%s sidecar_frame_count_mismatch video_frames=%d sidecar_frames=%d (사이드카가 영상보다 길다, 영상·사이드카 "
                        "자른 구간이 같은지 확인)", self._camera_id, self._k, len(self._offsets))

    def open(self) -> StreamInfo:
        self._anchor.get()  # 처음 연 카메라가 원점을 정한다
        return super().open()

    def read(self) -> SourcePacket | None:
        n = len(self._offsets)
        while True:
            packet = super().read()
            if packet is None:
                self._check_short()
                return None
            k = self._k
            self._k += 1
            if k < n:
                break
            if not self._warned_extra:
                self._warned_extra = True
                log.warning("%s sidecar_frame_count_mismatch video_frames>%d sidecar_frames=%d (촬영 시각을 모르는 남은 프레임은 "
                            "보내지 않는다)", self._camera_id, n, n)
        mono0, wall0 = self._anchor.get()
        rel_ms = self._pass * self._anchor.period_ms + self._offsets[k]
        if self._paced_realtime:
            self._clock.sleep(mono0 + rel_ms / 1000 - self._clock.monotonic())
        return ReplayPacket(packet.data, packet.pts, packet.keyframe, packet.camera_time_us, packet.sender_report,
                            capture_time_ms=wall0 + rel_ms, uncertainty_ms=self._sidecar.uncertainty_ms[k])

    def describe(self) -> dict:
        return {**super().describe(), "realtime": self._paced_realtime, "timing": "sidecar"}


def replay_offsets(sidecars: dict[str, Sidecar]) -> tuple[dict[str, array], int, int | None]:
    """카메라별 재생 offset, 공통 순환 길이(ms), 공통 기준(원본 촬영 ms, sidecar-cut 결과면 None)."""
    cut = [cid for cid, s in sidecars.items() if s.replay_offset_ms is not None]
    if cut and len(cut) != len(sidecars):
        raise SidecarError("sidecar-cut 사이드카와 원본 사이드카를 한 재생에 섞지 않는다: " + ", ".join(sorted(cut)))
    ref: int | None = None
    if cut:
        offsets = {cid: s.replay_offset_ms for cid, s in sidecars.items()}
    else:
        ref = min(s.capture_ms[0] for s in sidecars.values())
        offsets = {cid: array("q", (t - ref for t in s.capture_ms)) for cid, s in sidecars.items()}
    for cid, off in offsets.items():
        if off[0] < 0:
            raise SidecarError(f"{cid}: replay_offset_ms 가 음수다")
    period = max(off[-1] + round(sidecars[cid].interval_ms()) for cid, off in offsets.items())
    return offsets, max(1, int(period)), ref


# --------------------------------------------------------------------------- 자르기·이어 붙이기 도구


def parse_range(text: str) -> tuple[int, int]:
    try:
        a, b = (int(x) for x in text.split(":", 1))
    except ValueError:
        raise argparse.ArgumentTypeError(f"구간은 START_MS:END_MS (UTC ms) 다: {text!r}") from None
    if b <= a:
        raise argparse.ArgumentTypeError(f"구간 끝이 시작보다 커야 한다: {text!r}")
    return a, b


def cut_sidecar(src: Path, dst: Path, ranges: Sequence[tuple[int, int]]) -> list[dict]:
    """src 사이드카에서 촬영 시각 구간들 [start, end) 의 줄만 골라 replay_offset_ms 를 더해 dst 에 쓴다(덮어쓰지 않음).

    replay_offset_ms = 앞 구간 길이(end − start) 합 + (촬영 시각 − 이 구간 start). 구간 경계가 카메라 공통이라 네 카메라의
    재생 시각축이 같다. 반환: 구간마다 원본 줄 번호 [first_index, end_index) 와 프레임 수(영상 자르기를 같은 프레임으로 맞출 때).
    줄 번호는 원본 사이드카의 0 부터 센 프레임 순서다(영상 프레임 번호와 같다고 본다).
    """
    ordered = sorted(ranges)
    for (_, b0), (a1, _) in pairwise(ordered):
        if a1 < b0:
            raise SidecarError("구간이 겹친다")
    base = [sum(b - a for a, b in ordered[:i]) for i in range(len(ordered))]
    found: list[dict] = [{"start_ms": a, "end_ms": b, "first_index": None, "end_index": None, "frames": 0} for a, b in ordered]
    with src.open(encoding="utf-8") as fin, dst.open("x", encoding="utf-8", newline="\n") as fout:
        for idx, line in enumerate(fin):
            if not line.strip():
                continue
            row = json.loads(line)
            t = int(row["header"]["estimated_capture_time_ms"])
            for i, (a, b) in enumerate(ordered):
                if a <= t < b:
                    seg = found[i]
                    if seg["first_index"] is None:
                        seg["first_index"] = idx
                    seg["end_index"] = idx + 1
                    seg["frames"] += 1
                    row["replay_offset_ms"] = base[i] + (t - a)
                    fout.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                    break
    return found


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("sidecar-cut", help="녹화 사이드카를 촬영 시각 구간으로 잘라 이어 붙인다 (잘라 붙인 영상의 사이드카 재생용)")
    p.add_argument("sidecars", nargs="+", help="카메라별 원본 사이드카 (cam_A.frames.jsonl …)")
    p.add_argument("--range", dest="ranges", action="append", type=parse_range, required=True,
                   help="촬영 시각 구간 START_MS:END_MS (UTC ms, 여러 번 주면 시각 순서로 이어 붙임. 네 카메라에 같은 구간을 준다)")
    p.add_argument("--out-dir", required=True, help="결과 폴더 (같은 파일 이름으로 쓴다, 덮어쓰지 않음)")


def cmd_cut(args: argparse.Namespace) -> int:
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    report = {}
    try:
        for s in args.sidecars:
            src = Path(s)
            report[src.name] = cut_sidecar(src, out / src.name, args.ranges)
    except (OSError, SidecarError, ValueError, KeyError) as e:
        print(f"사이드카 자르기 실패: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0
