import type {
  ViewerObjectEditPreview,
  ViewerObjectEditResult,
  ViewerWorkSurfaceGeometry,
} from '@/features/viewer3d';
import { useObjectEditStore } from '@/store/objectEdit.store';
import { beforeEach, describe, expect, it } from 'vitest';

const before: ViewerWorkSurfaceGeometry = {
  centerXy: [5, 4.6],
  sizeXy: [1.2, 0.6],
  yawRad: 0,
  topHeightM: 0.74,
};
const moved: ViewerWorkSurfaceGeometry = { ...before, centerXy: [6, 4.6] };
const provenance = {
  sourceId: 's-1',
  geometryContract: 'OPERATIONAL_GEOMETRY_UNCONFIRMED',
  surfacesReceivedAt: 1,
  geometryVersion: null,
  sceneVersion: 'scene-3',
  configVersion: null,
} as const;
const target = (id: string) => ({ type: 'object', id }) as const;
// 장애물 편집(!193)이 머지되면 결과에 objectKind 가 필수다. 펼쳐 넣어 머지 전(칸 없음)·후 모두 컴파일되게 한다.
const workSurface = { objectKind: 'WORK_SURFACE' } as const;

const preview = (
  id: string,
  draft: ViewerWorkSurfaceGeometry,
  origin: 'REGISTERED' | 'NEW' = 'REGISTERED',
): ViewerObjectEditPreview => ({
  ...workSurface,
  target: target(id),
  mode: 'LIVE',
  origin,
  before,
  draft,
  provenance,
  saved: false,
});
const finished = (
  id: string,
  after: ViewerWorkSurfaceGeometry,
  origin: 'REGISTERED' | 'NEW' = 'REGISTERED',
): ViewerObjectEditResult => ({
  ...workSurface,
  status: 'FINISHED',
  target: target(id),
  mode: 'LIVE',
  origin,
  before,
  after,
  afterItem: {},
  changed: after !== before,
  provenance,
  saved: false,
});
const cancelled = (
  id: string,
  reason: 'USER' | 'SELECTION_CHANGED' | 'UNMOUNTED' | 'REPLACED',
): ViewerObjectEditResult => ({
  ...workSurface,
  status: 'CANCELLED',
  target: target(id),
  mode: 'LIVE',
  origin: 'REGISTERED',
  before,
  reason,
  provenance,
  saved: false,
});

const store = () => useObjectEditStore.getState();

beforeEach(() => store().clear());

describe('3D 작업면 편집 저장소', () => {
  it('미리보기를 두었다가, 끝내면 바뀐 형상을 남기고 원래대로면 지운다', () => {
    store().onPreview(preview('ws-1', moved));
    expect(store().preview?.draft).toEqual(moved);
    store().onResult(finished('ws-1', moved));
    expect(store().preview).toBeNull();
    expect(store().kept).toEqual({ 'ws-1': moved });
    expect(store().ended).toMatchObject({ seq: 1, targetId: 'ws-1', status: 'FINISHED' });

    store().onResult(finished('ws-1', before));
    expect(store().kept).toEqual({});
    expect(store().ended?.seq).toBe(2);
  });

  it('서버 float4 높이 차이만 있으면 바뀌지 않은 것으로 본다(화면의 고치는 중 판단과 같다)', () => {
    const typed = { ...before, topHeightM: Math.fround(0.74) };
    store().onPreview(preview('ws-1', typed));
    store().onResult(finished('ws-1', typed));
    expect(store().kept).toEqual({});

    store().onPreview(preview('ws-1', typed));
    store().onResult(cancelled('ws-1', 'SELECTION_CHANGED'));
    expect(store().kept).toEqual({});
  });

  it('숫자 칸에서 고친 형상을 남기고, 버리면 그 작업면 것만 지운다', () => {
    store().keep('ws-1', moved);
    store().keep('ws-2', before);
    store().discard('ws-1');
    expect(store().kept).toEqual({ 'ws-2': before });
  });

  it('새 작업면은 끝내면 늘 형상을 남긴다', () => {
    store().onResult(finished('new-1', before, 'NEW'));
    expect(store().kept).toEqual({ 'new-1': before });
  });

  it('Esc(USER)는 이번 편집만 버리고, 선택이 바뀌거나 뷰어가 내려가거나 다른 편집이 시작되면 마지막 미리보기를 남긴다', () => {
    store().onPreview(preview('ws-1', moved));
    store().onResult(cancelled('ws-1', 'SELECTION_CHANGED'));
    expect(store().kept).toEqual({ 'ws-1': moved });

    // 남긴 형상에서 다시 열어 더 옮기다 Esc 하면 그 전에 남긴 형상으로 돌아간다.
    store().onPreview(preview('ws-1', { ...moved, centerXy: [7, 4.6] }));
    store().onResult(cancelled('ws-1', 'USER'));
    expect(store().kept).toEqual({ 'ws-1': moved });
    store().onPreview(preview('ws-3', moved));
    store().onResult(cancelled('ws-3', 'USER'));
    expect(store().kept['ws-3']).toBeUndefined();

    for (const reason of ['UNMOUNTED', 'REPLACED'] as const) {
      store().onPreview(preview('ws-2', moved));
      store().onResult(cancelled('ws-2', reason));
      expect(store().kept['ws-2']).toEqual(moved);
      store().discard('ws-2');
    }
  });

  it('원본이 바뀌거나(STALE_SOURCE) 렌더 오류(NOT_READY)로 끝나도 마지막 미리보기를 남기고, 움직이지 않았으면 남기지 않는다', () => {
    store().onPreview(preview('ws-1', moved));
    store().onResult({
      status: 'REJECTED',
      target: target('ws-1'),
      reason: 'STALE_SOURCE',
      detail: '',
      saved: false,
    });
    expect(store().kept).toEqual({ 'ws-1': moved });
    expect(store().ended).toMatchObject({
      status: 'REJECTED',
      reason: 'STALE_SOURCE',
      origin: null,
    });

    store().clear();
    store().onPreview(preview('ws-1', before));
    store().onResult(cancelled('ws-1', 'UNMOUNTED'));
    expect(store().kept).toEqual({});
  });

  it('거부한 명령(다른 작업면 시작·쓰는 ID 로 만들기)은 열린 편집을 끝내지 않는다', () => {
    const refused = (id: string, reason: 'NOT_FOUND' | 'INVALID_TARGET') =>
      ({ status: 'REJECTED', target: target(id), reason, detail: '', saved: false }) as const;
    store().onPreview(preview('ws-1', moved));
    store().onResult(refused('ws-2', 'NOT_FOUND'));
    store().onResult(refused('ws-1', 'INVALID_TARGET'));
    expect(store()).toMatchObject({ kept: {}, ended: null });
    expect(store().preview?.draft).toEqual(moved);

    store().clear();
    store().onResult(refused('ws-1', 'NOT_FOUND'));
    expect(store().ended).toBeNull();
  });

  it('장애물 편집(objectKind OBSTACLE)은 받지 않는다', () => {
    store().onPreview({ ...preview('so-1', moved), objectKind: 'OBSTACLE' } as never);
    store().onResult({ ...finished('so-1', moved), objectKind: 'OBSTACLE' } as never);
    expect(store()).toMatchObject({ preview: null, kept: {}, ended: null });
  });

  it('비우면 미리보기·남긴 형상·끝난 일을 모두 지운다', () => {
    store().onPreview(preview('ws-1', moved));
    store().onResult(finished('ws-1', moved));
    store().clear();
    expect(store()).toMatchObject({ preview: null, kept: {}, ended: null });
  });
});
