import type { SocketLike } from '@/features/cctv/previewSession';
import { PREVIEW_PROTOCOL } from '@/features/cctv/types';
import { buildPps, buildSps, buildTestH264Stream } from '@/mocks/h264TestStream';

const cameras = ['cam_A', 'cam_B', 'cam_C', 'cam_D'];
const base64 = (bytes: Uint8Array) =>
  btoa(Array.from(bytes, (b) => String.fromCharCode(b)).join(''));

/** 네트워크 없이 계약 형식의 합성 영상으로 원격 시청을 흉내 낸다. */
export function openMockCctvSocket(): SocketLike {
  const stream = buildTestH264Stream({ frames: 10, keyEvery: 1, stepFrames: 1 }).data;
  const starts: number[] = [];
  for (let i = 0; i < stream.length - 4; i++) {
    if (stream[i] === 0 && stream[i + 1] === 0 && stream[i + 2] === 0 && stream[i + 3] === 1)
      starts.push(i);
  }
  const frames = starts.flatMap((start, index) =>
    (stream[start + 4]! & 31) === 5
      ? [stream.slice(start, starts[index + 1] ?? stream.length)]
      : [],
  );
  let frameIndex = 0;
  let timer: ReturnType<typeof setInterval> | undefined;
  let closed = false;
  let sequence = 0;
  const socket: SocketLike = {
    binaryType: 'arraybuffer',
    onopen: null,
    onclose: null,
    onerror: null,
    onmessage: null,
    close: () => {
      closed = true;
      clearInterval(timer);
    },
  };
  const send = (data: unknown) => socket.onmessage?.({ data });
  setTimeout(() => {
    if (closed) return;
    socket.onopen?.();
    send(JSON.stringify({ kind: 'hello', protocol: PREVIEW_PROTOCOL, cameras }));
    send(JSON.stringify({ kind: 'status', protocol: PREVIEW_PROTOCOL, state: 'live' }));
    for (const camera_id of cameras)
      send(
        JSON.stringify({
          kind: 'codec',
          protocol: PREVIEW_PROTOCOL,
          codec_string: 'avc1.42C01E',
          codec_info: {
            camera_id,
            stream_epoch: 0,
            codec: 'H264',
            sps: base64(buildSps(10, 6)),
            pps: base64(buildPps()),
            pts_timebase: '1/1000',
            width: 160,
            height: 96,
          },
        }),
      );
    const frame = () => {
      const image = frames[frameIndex++ % frames.length]!;
      for (const camera_id of cameras) {
        const header = new TextEncoder().encode(
          JSON.stringify({
            camera_id,
            stream_epoch: 0,
            frame_sequence: sequence++,
            estimated_capture_time_ms: Date.now(),
            timestamp_source: 'RECEIVE_ESTIMATED',
            timestamp_uncertainty_ms: 0,
            keyframe: true,
          }),
        );
        const bytes = new Uint8Array(4 + header.length + image.length);
        new DataView(bytes.buffer).setUint32(0, header.length);
        bytes.set(header, 4);
        bytes.set(image, 4 + header.length);
        send(bytes.buffer);
      }
    };
    frame();
    timer = setInterval(frame, 500);
  }, 0);
  return socket;
}
