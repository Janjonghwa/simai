"""I0 수신 → 디코딩 → 최신성 대기열 (명세 12.7, 5.1).

카메라마다 CameraIngest 하나가 stream_epoch·코덱 정보·키프레임 대기·frame_sequence 빈칸을 다룬다.

- 새 stream_epoch: 디코더를 버리고(reset) 그 epoch 의 코덱 정보가 올 때까지 NO_CODEC_INFO, 코덱 정보가 오면
  디코더를 다시 초기화(configure)하고 키프레임이 올 때까지 WAITING_FOR_KEYFRAME. 그동안 그 카메라는 관측 없음이다.
  이전 스트림에서 정렬을 기다리거나 디코더·추론 대기열에 남은 프레임은 STREAM_RESET 으로 버린다(같은 epoch 코덱 변경도).
- 역순·지연 도착은 보간 창(reorder_window_ms, 예 200ms) 안이면 frame_sequence 순서로 정렬해 디코더에 넣는다.
  빈 번호 뒤에 온 프레임이 창을 넘겨 기다리면(또는 붙잡은 프레임이 max_reorder_frames 를 넘으면) 빈 번호를
  NETWORK_LOSS 로 확정하고 재요청하지 않는다. 참조 프레임이 끊겼으므로 기본은 다음 키프레임까지 디코딩하지 않는다
  (깨진 그림을 관측으로 쓰지 않음, FreshnessConfig.keyframe_after_loss).
  창 판정은 수신 때 새 프레임을 넣기 전과 추론 루프의 poll 때 하므로, 결과가 poll 시점에 달리지 않는다.
- 창을 넘긴 뒤 지나간 frame_sequence 가 오면 LATE_ARRIVAL 로 버리고 기록만 한다. 손실로 셌던 번호면 손실에서 뺀다.
- 끝난 epoch 의 프레임은 STALE_EPOCH 로 버린다.
- 디코더 출력은 입력 때 넘긴 열쇠 (stream_epoch, frame_sequence) 로만 헤더와 묶는다. 이렇게 해야 디코더 지연·
  재정렬·재접속이 겹쳐도 촬영 시각이 다른 프레임에 붙지 않는다(G3).

시각 측정에 쓰는 현재 시각은 주입받은 Clock 으로만 읽는다.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable, Sequence

from simai_ai.io.decoder import DecodedPicture, DecodeError, Decoder
from simai_ai.io.freshness import (
    FrameDisposition,
    FrameLog,
    FrameRecord,
    FreshnessConfig,
    InferenceTicket,
    LatestFrameScheduler,
    RecordSink,
    SkipReason,
)
from simai_ai.io.messages import FrameKey, I0CodecInfo, I0Frame, I0FrameHeader, I0Message
from simai_ai.providers.base import Frame
from simai_ai.risk.clock import Clock
from simai_ai.tracking.time_model import require_ms

DecoderFactory = Callable[[str], Decoder]


class CameraIngest:
    """카메라 한 대의 I0 스트림 상태. 한 카메라의 메시지는 한 연결에서 순서대로 온다고 보지만, 어긋나도 안전하게 버린다."""

    def __init__(
        self,
        camera_id: str,
        decoder: Decoder,
        scheduler: LatestFrameScheduler,
        log: FrameLog,
        clock: Clock,
        config: FreshnessConfig,
    ) -> None:
        self.camera_id = camera_id
        self._decoder = decoder
        self._scheduler = scheduler
        self._log = log
        self._clock = clock
        self._config = config
        self._lock = threading.Lock()
        self.stream_epoch: int | None = None
        self.codec: I0CodecInfo | None = None
        self.waiting_keyframe = True
        self._last_sequence: int | None = None  # 디코딩 단계로 넘긴 마지막 frame_sequence
        # frame_sequence 빈칸 뒤에 와서 정렬을 기다리는 압축 프레임과 도착 시각
        self._held: dict[int, tuple[I0Frame, int]] = {}
        self._missing: OrderedDict[int, None] = OrderedDict()
        self._in_decoder: OrderedDict[FrameKey, I0FrameHeader] = OrderedDict()
        # max_decoder_pending 을 넘겨 버린 열쇠. 디코더가 뒤늦게 그 그림을 내면 조용히 버린다.
        self._evicted: OrderedDict[FrameKey, None] = OrderedDict()

    @property
    def ready(self) -> bool:
        """코덱 정보와 키프레임이 모두 도착해 관측을 낼 수 있는 상태인지 (명세 12.7 재접속)."""
        with self._lock:
            return self.codec is not None and not self.waiting_keyframe

    def _now(self) -> int:
        return require_ms("clock.now_ms()", self._clock.now_ms())

    def _record(
        self,
        disposition: FrameDisposition,
        epoch: int | None,
        seq: int | None,
        reason: SkipReason | None = None,
        capture: int | None = None,
    ) -> None:
        self._log.record(
            FrameRecord(
                camera_id=self.camera_id,
                stream_epoch=epoch,
                frame_sequence=seq,
                disposition=disposition,
                at_ms=self._now(),
                reason=reason,
                estimated_capture_time=capture,
            )
        )

    def _skip_header(self, header: I0FrameHeader, reason: SkipReason) -> None:
        self._record(
            FrameDisposition.SKIPPED_BY_POLICY,
            header.stream_epoch,
            header.frame_sequence,
            reason,
            header.estimated_capture_time_ms,
        )

    def _drop_in_decoder(self, reason: SkipReason) -> None:
        for header in self._in_decoder.values():
            self._skip_header(header, reason)
        self._in_decoder.clear()

    def _drop_queued(self) -> None:
        """디코더 안과 추론 대기열의 프레임을 버린다. 새 스트림이 준비될 때까지 관측 없음이다(명세 12.7 재접속)."""
        self._drop_in_decoder(SkipReason.STREAM_RESET)
        self._scheduler.discard(self.camera_id, SkipReason.STREAM_RESET)

    def _abandon_held(self) -> None:
        """끝나는 스트림(새 epoch·같은 epoch 코덱 변경)에서 정렬을 기다리던 프레임.

        앞의 빈 번호는 손실로 확정하고 프레임은 디코딩하지 않고 버린다.
        """
        for seq in sorted(self._held):
            frame, _ = self._held.pop(seq)
            if self._last_sequence is not None and seq > self._last_sequence + 1:
                self._confirm_loss(frame.header.stream_epoch, self._last_sequence + 1, seq)
            self._last_sequence = seq
            self._log.count(self.camera_id, sequenced=1)
            self._skip_header(frame.header, SkipReason.STREAM_RESET)

    def _start_epoch(self, epoch: int) -> None:
        """새 stream_epoch. 이전 스트림의 프레임은 버리고 코덱 정보부터 다시 기다린다."""
        self._abandon_held()
        self._drop_queued()
        self._decoder.reset()
        self.stream_epoch = epoch
        self.codec = None
        self.waiting_keyframe = True
        self._last_sequence = None
        self._missing.clear()
        self._evicted.clear()

    def on_codec_info(self, info: I0CodecInfo) -> None:
        with self._lock:
            self._release()  # 이미 창을 넘긴 빈 번호를 먼저 확정한다(poll 을 불렀든 안 불렀든 같은 결과)
            if self.stream_epoch is not None and info.stream_epoch < self.stream_epoch:
                return  # 끝난 연결의 코덱 정보
            if self.stream_epoch is None or info.stream_epoch > self.stream_epoch:
                self._start_epoch(info.stream_epoch)
            elif info == self.codec:
                return  # 같은 코덱 정보를 다시 받음
            elif self.codec is not None:
                # 같은 epoch 에서 코덱 정보가 바뀜: 정렬 대기·디코더 안·대기열의 이전 코덱 프레임을 버리고
                # 새 코덱의 키프레임부터 다시 시작한다.
                self._abandon_held()
                self._drop_queued()
                self._decoder.reset()
            self._decoder.configure(info)
            self.codec = info
            self.waiting_keyframe = True

    def _confirm_loss(self, epoch: int, start: int, stop: int) -> None:
        """frame_sequence start..stop-1 이 오지 않았다고 확정한다(NETWORK_LOSS)."""
        for missing in range(start, stop):
            self._record(FrameDisposition.NETWORK_LOSS, epoch, missing)
            self._missing[missing] = None
            if len(self._missing) > self._config.max_missing_tracked:
                self._missing.popitem(last=False)
        if self._config.keyframe_after_loss:
            self.waiting_keyframe = True  # 참조 프레임이 끊겼다

    def _release(self, force: bool = False) -> None:
        """정렬을 기다리는 프레임 중 순서가 된 것을 디코딩 단계로 넘긴다.

        맨 앞 빈 번호는 그 뒤 프레임이 창 안에 있는 동안 기다리고, 창을 넘기거나 force 이면 손실로 확정한다.
        """
        window = self._config.reorder_window_ms
        while self._held:
            seq = min(self._held)
            last = self._last_sequence
            if last is not None and seq > last + 1:
                waited = self._now() - min(arrived for _, arrived in self._held.values())
                if (
                    not force
                    and window > 0
                    and waited <= window
                    and len(self._held) <= self._config.max_reorder_frames
                ):
                    return
                self._confirm_loss(self._held[seq][0].header.stream_epoch, last + 1, seq)
            frame, _ = self._held.pop(seq)
            self._decode(frame)

    def poll(self) -> None:
        """시간이 지나 창을 넘긴 빈 번호를 손실로 확정한다. 추론 루프가 부른다.

        이 카메라가 수신·디코딩 중이면 기다리지 않고 넘어간다(그 수신이 끝날 때 같은 일을 한다).
        """
        if not self._lock.acquire(blocking=False):
            return
        try:
            self._release()
        finally:
            self._lock.release()

    def on_frame(self, frame: I0Frame) -> None:
        header = frame.header
        if header.camera_id != self.camera_id:
            raise ValueError(f"{self.camera_id} 수신기에 {header.camera_id} 프레임이 왔다")
        with self._lock:
            self._log.count(self.camera_id, received=1)
            # 이 프레임으로 빈칸을 채우기 전에, 이미 창을 넘긴 빈 번호를 확정한다. 그래야 창 밖에 온 프레임은
            # 추론 루프가 그 전에 poll 을 불렀든 안 불렀든 똑같이 LATE_ARRIVAL 이다.
            self._release()
            if self.stream_epoch is not None and header.stream_epoch < self.stream_epoch:
                self._skip_header(header, SkipReason.STALE_EPOCH)
                return
            if self.stream_epoch is None or header.stream_epoch > self.stream_epoch:
                self._start_epoch(header.stream_epoch)  # 코덱 정보 없이 새 epoch 프레임이 먼저 옴
            seq = header.frame_sequence
            if (self._last_sequence is not None and seq <= self._last_sequence) or seq in self._held:
                if seq in self._missing:
                    del self._missing[seq]
                    self._log.recover_loss(self.camera_id)
                self._skip_header(header, SkipReason.LATE_ARRIVAL)
                return
            self._held[seq] = (frame, self._now())
            self._release()

    def _decode(self, frame: I0Frame) -> None:
        """순서대로 넘어온 압축 프레임 한 장을 디코딩한다."""
        header = frame.header
        self._last_sequence = header.frame_sequence
        self._log.count(self.camera_id, sequenced=1)
        if self.codec is None:
            self._skip_header(header, SkipReason.NO_CODEC_INFO)
            return
        if self.waiting_keyframe and not header.keyframe:
            self._skip_header(header, SkipReason.WAITING_FOR_KEYFRAME)
            return
        self._in_decoder[header.key] = header
        try:
            pictures = self._decoder.decode(frame.payload, header.key, header.keyframe)
        except DecodeError:
            del self._in_decoder[header.key]
            self._record(
                FrameDisposition.DECODE_FAILED,
                header.stream_epoch,
                header.frame_sequence,
                capture=header.estimated_capture_time_ms,
            )
            self._drop_in_decoder(SkipReason.STREAM_RESET)
            codec = self.codec
            self._decoder.reset()
            self._decoder.configure(codec)
            self.waiting_keyframe = True
            return
        self.waiting_keyframe = False
        self._emit(pictures)
        while len(self._in_decoder) > self._config.max_decoder_pending:
            key, stale = self._in_decoder.popitem(last=False)
            self._skip_header(stale, SkipReason.DECODER_DROPPED)
            self._evicted[key] = None
            if len(self._evicted) > self._config.max_decoder_pending:
                self._evicted.popitem(last=False)

    def flush(self) -> None:
        """입력 끝. 정렬을 기다리던 프레임(빈 번호는 손실로 확정)과 디코더에 남은 그림을 내보낸다."""
        with self._lock:
            self._release(force=True)
            if self.codec is not None:
                self._emit(self._decoder.flush())
            self._drop_in_decoder(SkipReason.DECODER_DROPPED)

    def _emit(self, pictures: Sequence[DecodedPicture]) -> None:
        for picture in pictures:
            header = self._in_decoder.pop(picture.key, None)
            if header is None and picture.key in self._evicted:
                del self._evicted[picture.key]
                continue  # 이미 DECODER_DROPPED 로 기록한 프레임
            if header is None:
                # 넣지 않았거나 이미 버린 열쇠다. 어느 헤더인지 추측해 붙이지 않는다.
                raise RuntimeError(f"{self.camera_id}: 디코더가 모르는 열쇠 {picture.key} 의 그림을 냈다")
            self._log.count(self.camera_id, decoded=1)
            self._scheduler.offer(
                Frame(
                    camera_id=self.camera_id,
                    estimated_capture_time=header.estimated_capture_time_ms,
                    timestamp_source=header.timestamp_source,
                    frame_ref={"stream_epoch": header.stream_epoch, "frame_sequence": header.frame_sequence},
                    timestamp_uncertainty_ms=header.timestamp_uncertainty_ms,
                    image=picture.image,
                )
            )


class I0Ingest:
    """카메라 여러 대의 I0 입력을 받아 최신성 정책에 따라 추론할 프레임을 낸다.

    receive 는 카메라별 수신 스레드에서, next_frame·next_batch 는 추론 루프에서 부른다(카메라마다 잠금이 따로라 한
    카메라의 디코딩이 다른 카메라 수신을 막지 않는다).
    """

    def __init__(
        self,
        camera_ids: Sequence[str],
        decoder_factory: DecoderFactory,
        clock: Clock,
        config: FreshnessConfig = FreshnessConfig(),
        sink: RecordSink | None = None,
    ) -> None:
        self.camera_ids = tuple(camera_ids)
        self.config = config
        self.log = FrameLog(self.camera_ids, config, sink)
        self.scheduler = LatestFrameScheduler(self.camera_ids, clock, config, self.log)
        self._cameras = {
            c: CameraIngest(c, decoder_factory(c), self.scheduler, self.log, clock, config) for c in self.camera_ids
        }

    def camera(self, camera_id: str) -> CameraIngest:
        try:
            return self._cameras[camera_id]
        except KeyError:
            raise ValueError(f"모르는 camera_id: {camera_id!r}") from None

    def receive(self, message: I0Message) -> None:
        if isinstance(message, I0Frame):
            self.camera(message.header.camera_id).on_frame(message)
        elif isinstance(message, I0CodecInfo):
            self.camera(message.camera_id).on_codec_info(message)
        else:
            raise TypeError(f"I0 메시지가 아니다: {type(message).__name__}")

    def poll(self) -> None:
        """보간 창을 넘긴 빈 번호를 손실로 확정하고 뒤 프레임을 디코딩 단계로 넘긴다."""
        for cam in self._cameras.values():
            cam.poll()

    def next_frame(self) -> InferenceTicket | None:
        """지금 추론에 넘길 프레임 한 장(한 장씩 차례로 추론할 때). 넘기는 순간에 최신성을 다시 따진다.

        넘긴 프레임의 INFERRED 기록은 지표에는 바로 들어가고, sink 에는 다음 호출이나 log.deliver() 때 간다.
        """
        self.poll()
        return self.scheduler.take_one()

    def next_batch(self) -> list[InferenceTicket]:
        """지금 한꺼번에 추론에 넘길 프레임(카메라마다 최대 한 장). 대기 상한을 넘긴 프레임은 들어 있지 않다.

        INFERRED 기록이 sink 에 가는 시점은 next_frame 과 같다.
        """
        self.poll()
        return self.scheduler.take()

    def close(self) -> None:
        """입력 끝. 디코더에 남은 그림을 대기열로 내보낸다(녹화 재생 등)."""
        for cam in self._cameras.values():
            cam.flush()
        self.log.deliver()

    def ready_cameras(self) -> tuple[str, ...]:
        """관측을 낼 수 있는(코덱 정보·키프레임 도착) 카메라."""
        return tuple(c for c in self.camera_ids if self._cameras[c].ready)

    def summary(self) -> list[dict[str, object]]:
        return [self.log.stats[c].summary() for c in self.camera_ids]
