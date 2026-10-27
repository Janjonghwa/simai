import { sameGeometry } from '@/features/surfaces/surfaces';
import type {
  ViewerObjectEditPreview,
  ViewerObjectEditResult,
  ViewerWorkSurfaceGeometry,
} from '@/features/viewer3d';
import { create } from 'zustand';

// 3D 작업면 편집(S15P31A101-188)이 알리는 미리보기·결과를 작업면 등록 화면(-165)에 전한다. 틀(SpatialViewportHost)이 뷰어
// 콜백으로 onPreview·onResult 를 넘기고 화면이 읽는다(틀에 상태를 두지 않아 관제·구역 화면을 다시 그리지 않는다). 화면을 떠나면 비운다.

/** 작업면 편집의 미리보기·끝내기 결과. 장애물 편집이 합집합으로 더해져도(!193) 작업면 쪽만 고른다. */
export type SurfacePreview = Extract<ViewerObjectEditPreview, { draft: ViewerWorkSurfaceGeometry }>;
export type SurfaceFinished = Extract<
  ViewerObjectEditResult,
  { status: 'FINISHED'; after: ViewerWorkSurfaceGeometry }
>;

/** 장애물 편집(objectKind 'OBSTACLE')인지. objectKind 가 없으면(장애물 편집 전) 늘 작업면이다. */
const isObstacle = (value: object) => Reflect.get(value, 'objectKind') === 'OBSTACLE';

/** 화면의 '고치는 중'·저장 판단과 같은 기준(서버 float4 높이 차이는 같다고 본다)으로 바뀌었는지 본다. */
const sameShape = sameGeometry;

/** 끝난 편집. 화면이 알림·선택을 정할 때 쓴다(seq 가 바뀌면 새로 끝난 것이다). */
export interface ObjectEditEnd {
  seq: number;
  targetId: string | null;
  status: 'FINISHED' | 'CANCELLED' | 'REJECTED';
  reason: string | null;
  origin: 'REGISTERED' | 'NEW' | null;
}

export type KeptSurfaceDrafts = Readonly<Record<string, ViewerWorkSurfaceGeometry>>;

interface ObjectEditState {
  /** 지금 열린 작업면 편집의 마지막 미리보기(손잡이를 놓을 때마다 온다). 없으면 null. */
  preview: SurfacePreview | null;
  /** 끝난 편집·숫자 칸에서 남긴 미저장 형상(작업면 ID → 형상). 화면이 저장하거나 버리면 지운다. */
  kept: KeptSurfaceDrafts;
  ended: ObjectEditEnd | null;
  onPreview: (preview: ViewerObjectEditPreview) => void;
  onResult: (result: ViewerObjectEditResult) => void;
  /** 3D 편집 없이(숫자 칸) 고친 형상을 남긴다. */
  keep: (targetId: string, geometry: ViewerWorkSurfaceGeometry) => void;
  /** 그 작업면의 미저장 형상을 버린다(저장했거나 되돌렸다). */
  discard: (targetId: string) => void;
  clear: () => void;
}

const without = (kept: KeptSurfaceDrafts, id: string): KeptSurfaceDrafts => {
  if (!(id in kept)) return kept;
  const next = { ...kept };
  delete next[id];
  return next;
};

/**
 * 끝난 이유별로 남길 형상을 정한다.
 * - 끝내기(캔버스 Enter·끝내기 버튼): 바뀌었으면(새 작업면은 늘) 결과 형상을 남기고, 원래대로면 지운다.
 * - Esc·취소 버튼(USER): 이번 편집만 버린다(그 전에 남긴 형상은 그대로 둔다. 통째로 버리는 것은 화면의 되돌리기다).
 * - 빈 곳·다른 대상 선택, 2D 전환·화면 이동, 다른 편집 시작, 원본이 바뀜(STALE_SOURCE)·렌더 오류(NOT_READY):
 *   마지막 미리보기를 남긴다(다시 열어 이어 고친다).
 */
function keptAfter(
  kept: KeptSurfaceDrafts,
  preview: SurfacePreview | null,
  result: ViewerObjectEditResult,
): KeptSurfaceDrafts {
  const id = result.target?.id;
  if (!id) return kept;
  if (result.status === 'FINISHED') {
    const finished = result as SurfaceFinished;
    const changed = finished.origin === 'NEW' || !sameShape(finished.before, finished.after);
    return changed ? { ...kept, [id]: finished.after } : without(kept, id);
  }
  if (result.status === 'CANCELLED' && result.reason === 'USER') return kept;
  if (!preview || preview.target.id !== id) return kept;
  const moved = preview.origin === 'NEW' || !sameShape(preview.before, preview.draft);
  return moved ? { ...kept, [id]: preview.draft } : kept;
}

export const useObjectEditStore = create<ObjectEditState>()((set, get) => ({
  preview: null,
  kept: {},
  ended: null,
  onPreview: (preview) => {
    if (isObstacle(preview)) return;
    set({ preview: preview as SurfacePreview });
  },
  onResult: (result) => {
    if (isObstacle(result)) return;
    // 뷰어는 거부한 명령(시작·만들기)도 REJECTED 로 알리지만 열린 편집은 그대로 둔다. 열린 편집(미리보기)과 같은 대상일 때만
    // 끝난 것으로 본다(편집은 시작할 때 미리보기를 한 번 알린다). 이미 쓰는 ID(INVALID_TARGET)는 늘 거부한 명령이다.
    const preview = get().preview;
    if (
      result.status === 'REJECTED' &&
      (result.reason === 'INVALID_TARGET' ||
        !result.target ||
        preview?.target.id !== result.target.id)
    )
      return;
    set((state) => ({
      kept: keptAfter(state.kept, state.preview, result),
      preview: null,
      ended: {
        seq: (state.ended?.seq ?? 0) + 1,
        targetId: result.target?.id ?? null,
        status: result.status,
        reason: result.status === 'FINISHED' ? null : result.reason,
        origin: result.status === 'REJECTED' ? null : result.origin,
      },
    }));
  },
  keep: (targetId, geometry) => set((state) => ({ kept: { ...state.kept, [targetId]: geometry } })),
  discard: (targetId) => set((state) => ({ kept: without(state.kept, targetId) })),
  clear: () => set({ preview: null, kept: {}, ended: null }),
}));
