import { parseEnv } from '@/lib/parseEnv';
import { describe, expect, it } from 'vitest';

const valid = {
  VITE_API_BASE_URL: 'http://localhost:8080',
  VITE_STOMP_URL: 'ws://localhost:8080/ws',
};

describe('parseEnv', () => {
  it('http(s)·ws(s) 주소를 받는다', () => {
    expect(parseEnv(valid)).toEqual({ ...valid, VITE_API_MOCK: false });
    expect(
      parseEnv({
        VITE_API_BASE_URL: 'https://simai.example',
        VITE_STOMP_URL: 'wss://simai.example/ws',
      }),
    ).toBeTruthy();
  });

  it('값이 없으면 이름을 담아 멈춘다', () => {
    expect(() => parseEnv({ VITE_API_BASE_URL: valid.VITE_API_BASE_URL })).toThrow(
      /VITE_STOMP_URL/,
    );
  });

  it('스킴이 틀리면 멈춘다', () => {
    expect(() => parseEnv({ ...valid, VITE_STOMP_URL: 'http://localhost:8080/ws' })).toThrow(
      /ws\(s\) 주소/,
    );
    expect(() => parseEnv({ ...valid, VITE_API_BASE_URL: 'localhost:8080' })).toThrow(
      /VITE_API_BASE_URL/,
    );
  });

  it('API mock 여부는 true·false 만 받고 없으면 끈다', () => {
    expect(parseEnv({ ...valid, VITE_API_MOCK: 'true' }).VITE_API_MOCK).toBe(true);
    expect(parseEnv({ ...valid, VITE_API_MOCK: 'false' }).VITE_API_MOCK).toBe(false);
    expect(parseEnv(valid).VITE_API_MOCK).toBe(false);
    expect(() => parseEnv({ ...valid, VITE_API_MOCK: 'yes please' })).toThrow(/VITE_API_MOCK/);
  });

  it('운영 빌드에서 원격 주소는 https·wss 만 받는다', () => {
    const remote = {
      VITE_API_BASE_URL: 'http://simai.example',
      VITE_STOMP_URL: 'ws://simai.example/ws',
    };
    expect(() => parseEnv(remote, { production: true })).toThrow(/VITE_API_BASE_URL: 운영 빌드/);
    expect(() => parseEnv(remote, { production: true })).toThrow(/VITE_STOMP_URL: 운영 빌드/);
    expect(parseEnv(remote)).toBeTruthy();
    expect(
      parseEnv(
        { VITE_API_BASE_URL: 'https://simai.example', VITE_STOMP_URL: 'wss://simai.example/ws' },
        { production: true },
      ),
    ).toBeTruthy();
  });

  it('운영 빌드라도 내 PC(localhost)는 http·ws 를 받는다', () => {
    expect(parseEnv(valid, { production: true })).toBeTruthy();
    expect(
      parseEnv(
        { VITE_API_BASE_URL: 'http://127.0.0.1:8080', VITE_STOMP_URL: 'ws://[::1]:8080/ws' },
        { production: true },
      ),
    ).toBeTruthy();
  });
});
