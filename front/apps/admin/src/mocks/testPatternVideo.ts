// 개발 서버 클립 mock 이 쓰는 시험 영상. 사람이 없는 움직이는 시험 패턴을 브라우저에서 몇 초 녹화해 만든다.
// 영상 파일은 저장소에 두지 않는다(.gitignore 의 영상 규칙). 한 번 만들어 페이지가 열려 있는 동안 계속 쓴다.

const SECONDS = 4;
const WIDTH = 320;
const HEIGHT = 180;
const FPS = 15;
const BARS = ['#c0c0c0', '#c0c000', '#00c0c0', '#00c000', '#c000c0', '#c00000', '#0000c0'];

let made: Promise<string> | undefined;

/** 시험 영상의 blob 주소. 처음 부를 때 SECONDS 초 동안 녹화한다. 실패하면 다음에 다시 만든다. */
export function testPatternVideo(): Promise<string> {
  made ??= record().catch((error: unknown) => {
    made = undefined;
    throw error;
  });
  return made;
}

async function record(): Promise<string> {
  const canvas = document.createElement('canvas');
  canvas.width = WIDTH;
  canvas.height = HEIGHT;
  const context = canvas.getContext('2d');
  if (!context || typeof MediaRecorder === 'undefined') {
    throw new Error('이 브라우저에서는 시험 영상을 만들 수 없습니다');
  }
  const draw = (seconds: number) => {
    const width = WIDTH / BARS.length;
    BARS.forEach((color, i) => {
      context.fillStyle = color;
      context.fillRect(i * width, 0, width + 1, HEIGHT);
    });
    // 움직이는 사각형과 경과 시간으로 재생 중인지 알아볼 수 있게 한다.
    context.fillStyle = '#ffffff';
    context.fillRect((seconds / SECONDS) * (WIDTH - 24), 24, 24, 24);
    context.fillStyle = '#000000';
    context.fillRect(0, HEIGHT - 32, WIDTH, 32);
    context.fillStyle = '#ffffff';
    context.font = '16px monospace';
    context.fillText(`TEST ${seconds.toFixed(1)}s`, 10, HEIGHT - 10);
  };

  const stream = canvas.captureStream(FPS);
  const mimeType = ['video/webm;codecs=vp9', 'video/webm;codecs=vp8', 'video/webm'].find((type) =>
    MediaRecorder.isTypeSupported(type),
  );
  const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
  const chunks: Blob[] = [];
  recorder.ondataavailable = (event) => {
    if (event.data.size > 0) chunks.push(event.data);
  };
  const stopped = new Promise<void>((resolve) => {
    recorder.onstop = () => resolve();
  });

  const start = performance.now();
  draw(0);
  recorder.start();
  const timer = setInterval(() => draw((performance.now() - start) / 1000), 1000 / FPS);
  await new Promise((resolve) => setTimeout(resolve, SECONDS * 1000));
  clearInterval(timer);
  recorder.stop();
  await stopped;
  stream.getTracks().forEach((track) => track.stop());
  return URL.createObjectURL(new Blob(chunks, { type: recorder.mimeType || 'video/webm' }));
}
