import { Route as SetupRoute } from '@/routes/_app/setup';
import { Route as TbmRoute } from '@/routes/_app/tbm';
import { Route as WorkRoute } from '@/routes/_app/work';
import { isRedirect } from '@tanstack/react-router';
import { describe, expect, it } from 'vitest';

// /tbm 은 /work 로 넘기고, /work·/setup 의 검색 값(workUnitId)을 검사한다.
describe('작업 관리 라우트', () => {
  it('/tbm 은 /work 로 넘긴다', () => {
    const beforeLoad = TbmRoute.options.beforeLoad as (ctx: unknown) => void;
    let thrown: unknown;
    try {
      beforeLoad({});
    } catch (error) {
      thrown = error;
    }
    expect(isRedirect(thrown)).toBe(true);
    expect((thrown as { options: { to: string } }).options.to).toBe('/work');
  });

  it('/work 는 workUnitId 를 문자열로 받고 비면 없는 값이다', () => {
    const validate = WorkRoute.options.validateSearch as (s: Record<string, unknown>) => unknown;
    expect(validate({ workUnitId: '7' })).toEqual({ workUnitId: '7' });
    expect(validate({ workUnitId: 7 })).toEqual({ workUnitId: '7' });
    expect(validate({ workUnitId: '' })).toEqual({ workUnitId: undefined });
    expect(validate({})).toEqual({ workUnitId: undefined });
  });

  it('/setup 은 양의 정수 workUnitId 만 받고 없으면 없는 값이다(기본값 1 을 쓰지 않는다)', () => {
    const validate = SetupRoute.options.validateSearch as (s: Record<string, unknown>) => unknown;
    expect(validate({ workUnitId: '12' })).toEqual({ workUnitId: 12 });
    expect(validate({ workUnitId: 0 })).toEqual({ workUnitId: undefined });
    expect(validate({ workUnitId: 'abc' })).toEqual({ workUnitId: undefined });
    expect(validate({})).toEqual({ workUnitId: undefined });
  });
});
