import { parseEnv } from '@/lib/parseEnv';
import { describe, expect, it } from 'vitest';

const valid = {
  VITE_API_BASE_URL: 'http://localhost:8080',
  VITE_STOMP_URL: 'ws://localhost:8080/ws',
};

describe('parseEnv', () => {
  it('http(s)·ws(s) 주소를 받는다', () => {
    expect(parseEnv(valid)).toEqual(valid);
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
});
