import { chainItems } from '@/features/events/incidents';
import { describe, expect, it } from 'vitest';

const item = (value: string, person: string | null, start: number, end = start) => ({
  value,
  person,
  start,
  end,
});

describe('트랙 번호 변경에 따른 경보 연결', () => {
  it('앞 사람이 사라지고 60초 안에 새 번호가 나면 연결한다', () => {
    expect(chainItems([item('old', 'a', 0), item('new', 'b', 60_000)], new Set(['b']))).toEqual([
      ['old', 'new'],
    ]);
  });
  it('생존 여부를 모르거나 앞 사람이 살아 있으면 연결하지 않는다', () => {
    const items = [item('old', 'a', 0), item('new', 'b', 1)];
    expect(chainItems(items, null)).toEqual([['old'], ['new']]);
    expect(chainItems(items, new Set(['a', 'b']))).toEqual([['old'], ['new']]);
    expect(chainItems(items, new Set(['a']))).toEqual([['old'], ['new']]);
  });
  it('같은 번호, 사람 없는 경보, 60초 초과는 별개다', () => {
    expect(chainItems([item('a', 'a', 0), item('b', 'a', 1)], new Set())).toHaveLength(2);
    expect(chainItems([item('a', null, 0), item('b', 'b', 1)], new Set())).toHaveLength(2);
    expect(chainItems([item('a', 'a', 0), item('b', 'b', 60_001)], new Set())).toHaveLength(2);
  });
});
