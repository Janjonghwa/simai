// 관제 경보음(명세 FR-8 "소리는 확인까지 반복", FR-114 시험 경보). Web Audio 로 짧은 소리를 낸다.
// 브라우저는 사용자가 누르기 전에는 소리를 막는다. 앱 시작 때 첫 클릭·키 입력에서 깨운다(installAudioUnlock).

/** 경보 한 번: 880Hz 를 0.12초씩 두 번. 반복 간격은 ALARM_PERIOD_MS. */
export const ALARM_PERIOD_MS = 1100;
const TONE_HZ = 880;
const TONE_S = 0.12;
const OFFSETS_S = [0, 0.18];
const VOLUME = 0.05;

export interface AlarmSound {
  /** 소리를 낼 수 있는지(깨운 뒤 running). */
  readonly ready: boolean;
  /** 사용자 입력 안에서 불러 소리를 깨운다. 실패해도 던지지 않는다. 결과는 ready 로 본다. */
  unlock(): Promise<void>;
  /** 경보 한 번. 깨우기 전이면 아무것도 하지 않고, 깨운 뒤 멈췄으면 다시 깨워 본다. */
  beep(): void;
  /** ready 가 바뀌면 알린다. */
  subscribe(listener: () => void): () => void;
}

export function createAlarmSound(
  createContext: () => AudioContext = () => new AudioContext(),
): AlarmSound {
  let context: AudioContext | null = null;
  const listeners = new Set<() => void>();
  const notify = () => listeners.forEach((listener) => listener());

  const ensure = () => {
    if (!context) {
      context = createContext();
      context.addEventListener('statechange', notify);
    }
    return context;
  };

  return {
    get ready() {
      return context?.state === 'running';
    },
    async unlock() {
      try {
        const ctx = ensure();
        if (ctx.state !== 'running') await ctx.resume();
      } catch {
        // 만들지 못했거나(미지원) 깨우지 못했다. ready 가 false 로 남아 경보 배너에 「소리 켜기」가 보인다.
      }
      notify();
    },
    beep() {
      const ctx = context;
      if (!ctx) return;
      if (ctx.state !== 'running') {
        // 절전 복귀·오디오 장치 변경 등으로 멈췄다. 이미 사용자가 누른 페이지라 대개 입력 없이 다시 깨워진다.
        ctx.resume().catch(() => undefined);
        return;
      }
      const start = ctx.currentTime;
      for (const offset of OFFSETS_S) {
        const oscillator = ctx.createOscillator();
        const gain = ctx.createGain();
        oscillator.type = 'square';
        oscillator.frequency.value = TONE_HZ;
        gain.gain.value = VOLUME;
        oscillator.connect(gain).connect(ctx.destination);
        oscillator.start(start + offset);
        oscillator.stop(start + offset + TONE_S);
      }
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
}

export const alarmSound = createAlarmSound();

/** 시험 경보(명세 FR-114): 이벤트를 만들지 않고 소리만 한 번. 메뉴를 누른 입력 안에서 부르므로 소리도 깨운다. */
export const playTestAlarm = (sound: AlarmSound = alarmSound) =>
  sound.unlock().then(() => sound.beep());

/**
 * 사용자 입력(로그인 버튼 포함)에서 소리를 깨운다. 리스너는 떼지 않는다.
 * 나중에 소리가 멈춰도(절전 복귀 등) 다음 입력에서 다시 깨운다. 깨어 있으면 아무것도 하지 않는다.
 */
export function installAudioUnlock(
  sound: AlarmSound = alarmSound,
  target: Pick<Window, 'addEventListener'> = window,
) {
  const unlock = () => {
    if (!sound.ready) void sound.unlock();
  };
  target.addEventListener('pointerdown', unlock);
  target.addEventListener('keydown', unlock);
}
