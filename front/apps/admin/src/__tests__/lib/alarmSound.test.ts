import { createAlarmSound, installAudioUnlock, playTestAlarm } from '@/lib/alarmSound';
import { describe, expect, it, vi } from 'vitest';

/** 예약된 소리만 기록하는 AudioContext 대역. resume 이 막히면 suspended 로 남는다. */
class FakeAudioContext extends EventTarget {
  state: AudioContextState = 'suspended';
  currentTime = 10;
  destination = {};
  tones: { type: string; hz: number; volume: number; start: number; stop: number }[] = [];

  constructor(private readonly blocked = false) {
    super();
  }

  resume() {
    if (!this.blocked) {
      this.state = 'running';
      this.dispatchEvent(new Event('statechange'));
    }
    return Promise.resolve();
  }

  /** 절전 복귀·장치 변경처럼 브라우저가 소리를 멈춘다. */
  interrupt() {
    this.state = 'suspended';
    this.dispatchEvent(new Event('statechange'));
  }

  createGain() {
    return { gain: { value: 1 }, connect: () => undefined };
  }

  createOscillator() {
    const tone = { type: '', hz: 0, volume: 0, start: 0, stop: 0 };
    this.tones.push(tone);
    return {
      set type(value: string) {
        tone.type = value;
      },
      frequency: {
        set value(hz: number) {
          tone.hz = hz;
        },
      },
      connect: (gain: { gain: { value: number } }) => {
        tone.volume = gain.gain.value;
        return gain;
      },
      start: (at: number) => {
        tone.start = at;
      },
      stop: (at: number) => {
        tone.stop = at;
      },
    };
  }
}

function setup(blocked = false) {
  const contexts: FakeAudioContext[] = [];
  const sound = createAlarmSound(() => {
    const ctx = new FakeAudioContext(blocked);
    contexts.push(ctx);
    return ctx as unknown as AudioContext;
  });
  return { sound, contexts };
}

describe('createAlarmSound', () => {
  it('깨우기 전에는 소리를 내지 않고 AudioContext 도 만들지 않는다', () => {
    const { sound, contexts } = setup();
    sound.beep();
    expect(sound.ready).toBe(false);
    expect(contexts).toHaveLength(0);
  });

  it('깨우면 ready 가 되고 구독자에게 알린다', async () => {
    const { sound } = setup();
    const listener = vi.fn();
    sound.subscribe(listener);
    await sound.unlock();
    expect(sound.ready).toBe(true);
    expect(listener).toHaveBeenCalled();
  });

  it('경보 한 번은 880Hz 를 0.12초씩 두 번 예약한다', async () => {
    const { sound, contexts } = setup();
    await sound.unlock();
    sound.beep();
    expect(contexts[0]!.tones).toEqual([
      { type: 'square', hz: 880, volume: 0.05, start: 10, stop: 10.12 },
      { type: 'square', hz: 880, volume: 0.05, start: 10.18, stop: expect.closeTo(10.3) },
    ]);
  });

  it('브라우저가 막고 있으면 깨워도 ready 가 아니고 소리를 내지 않는다', async () => {
    const { sound, contexts } = setup(true);
    await sound.unlock();
    sound.beep();
    expect(sound.ready).toBe(false);
    expect(contexts[0]!.tones).toHaveLength(0);
  });

  it('깨운 뒤 멈췄으면 그 경보 대신 다시 깨운다', async () => {
    const { sound, contexts } = setup();
    await sound.unlock();
    contexts[0]!.interrupt();
    expect(sound.ready).toBe(false);
    sound.beep();
    expect(contexts[0]!.tones).toHaveLength(0);
    expect(sound.ready).toBe(true);
  });

  it('AudioContext 를 만들 수 없어도 던지지 않고 ready 가 아니다', async () => {
    const sound = createAlarmSound(() => {
      throw new Error('unsupported');
    });
    await expect(sound.unlock()).resolves.toBeUndefined();
    expect(sound.ready).toBe(false);
  });

  it('구독을 끊으면 더 알리지 않는다', async () => {
    const { sound } = setup();
    const listener = vi.fn();
    sound.subscribe(listener)();
    await sound.unlock();
    expect(listener).not.toHaveBeenCalled();
  });
});

describe('installAudioUnlock', () => {
  it('첫 클릭에서 깨우고, 깨어 있는 동안은 다시 깨우지 않는다', async () => {
    const { sound } = setup();
    const unlock = vi.spyOn(sound, 'unlock');
    const target = new EventTarget();
    installAudioUnlock(sound, target as unknown as Window);

    target.dispatchEvent(new Event('pointerdown'));
    await unlock.mock.results[0]!.value;
    expect(sound.ready).toBe(true);
    target.dispatchEvent(new Event('keydown'));
    expect(unlock).toHaveBeenCalledTimes(1);
  });

  it('나중에 소리가 멈추면 다음 입력에서 다시 깨운다', async () => {
    const { sound, contexts } = setup();
    const unlock = vi.spyOn(sound, 'unlock');
    const target = new EventTarget();
    installAudioUnlock(sound, target as unknown as Window);

    target.dispatchEvent(new Event('pointerdown'));
    await unlock.mock.results[0]!.value;
    contexts[0]!.interrupt();
    target.dispatchEvent(new Event('keydown'));
    await unlock.mock.results[1]!.value;
    expect(sound.ready).toBe(true);
  });

  it('깨우지 못하면 다음 입력에서 다시 시도한다', async () => {
    const { sound } = setup(true);
    const unlock = vi.spyOn(sound, 'unlock');
    const target = new EventTarget();
    installAudioUnlock(sound, target as unknown as Window);

    target.dispatchEvent(new Event('keydown'));
    await unlock.mock.results[0]!.value;
    target.dispatchEvent(new Event('pointerdown'));
    expect(unlock).toHaveBeenCalledTimes(2);
  });
});

describe('playTestAlarm', () => {
  it('소리를 깨우고 경보를 한 번 낸다', async () => {
    const { sound, contexts } = setup();
    await playTestAlarm(sound);
    expect(contexts[0]!.tones).toHaveLength(2);
  });
});
