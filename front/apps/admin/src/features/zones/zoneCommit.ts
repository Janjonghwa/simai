import { bodyOf as surfaceBodyOf } from '@/features/surfaces/surfaces';
import type { StaticObjectBody, StaticObjectItem, WorkSurface } from '@/features/surfaces/types';
import type { SaveSurfaceInput } from '@/features/surfaces/useSurfaceSave';
import type { ViewerEditBox } from '@/features/viewer3d';
import type { Zone, ZoneBody, ZoneItem } from '@/features/zones/types';
import type { SaveZoneInput } from '@/features/zones/useZoneSave';
import type { SurfaceDraft } from '@/features/zones/zoneEditModel';
import type { ZoneForm } from '@/features/zones/zoneForm';

// 구역 편집 저장 순서. 구역(POST·PUT) → 작업면(POST·PUT·DELETE, 하나씩 차례로) → 구성 묶음 발행. 앞 단계가 실패하면 거기서
// 멈추고(뒤는 보내지 않는다) 어디까지 저장했는지 알린다. 저장한 것이 있어야 발행한다. 요청은 ops 로 받아 순서만 이 파일이 정한다.

export interface CommitOps {
  saveZone(input: SaveZoneInput): Promise<ZoneItem>;
  createZone(body: ZoneBody): Promise<ZoneItem>;
  archiveZone(zoneId: string): Promise<unknown>;
  saveSurface(input: SaveSurfaceInput): Promise<StaticObjectItem>;
  createSurface(body: StaticObjectBody): Promise<StaticObjectItem>;
  archiveSurface(objectId: string): Promise<unknown>;
  publish(): Promise<unknown>;
}

export type CommitStep = 'zone' | 'surface' | 'publish';

export interface CommitOutcome {
  /** 저장·만들기 응답(새 버전). 하지 않았으면 null. */
  zone: ZoneItem | null;
  surface: StaticObjectItem | null;
  /** 이 단계까지 서버에 저장(보관 포함)했다. */
  saved: { zone: boolean; surface: boolean };
  /** 멈춘 단계와 오류. 끝까지 갔으면 null. */
  failed: { step: CommitStep; error: unknown } | null;
}

const emptyOutcome = (): CommitOutcome => ({
  zone: null,
  surface: null,
  saved: { zone: false, surface: false },
  failed: null,
});

/** 구역이나 작업면 한 부분을 서버에 저장했다고 적는다(새 버전 응답이 있으면 함께). */
const withSaved = (
  outcome: CommitOutcome,
  part: 'zone' | 'surface',
  response: Partial<Pick<CommitOutcome, 'zone' | 'surface'>> = {},
): CommitOutcome => ({ ...outcome, ...response, saved: { ...outcome.saved, [part]: true } });

const withFailure = (outcome: CommitOutcome, step: CommitStep, error: unknown): CommitOutcome => ({
  ...outcome,
  failed: { step, error },
});

/** 구역 단계. send 는 요청 하나를 보내고 새 버전 응답을 돌려준다(보관은 응답이 없다). */
async function zoneStep(
  outcome: CommitOutcome,
  send: () => Promise<ZoneItem | undefined>,
): Promise<CommitOutcome> {
  try {
    const zone = await send();
    return withSaved(outcome, 'zone', zone ? { zone } : {});
  } catch (error) {
    return withFailure(outcome, 'zone', error);
  }
}

async function publishStep(ops: CommitOps, outcome: CommitOutcome): Promise<CommitOutcome> {
  try {
    await ops.publish();
    return outcome;
  } catch (error) {
    return withFailure(outcome, 'publish', error);
  }
}

/** 저장한 것이 있을 때만 발행한다. */
const publishIfSaved = (ops: CommitOps, outcome: CommitOutcome): Promise<CommitOutcome> =>
  outcome.saved.zone || outcome.saved.surface
    ? publishStep(ops, outcome)
    : Promise.resolve(outcome);

/** 작업면 초안의 서버 본문. 새 작업면이면 surface 가 null 이다(상판 높이 표준편차는 받은 값을 다시 보낸다). */
const bodyOfDraft = (surface: WorkSurface | null, draft: SurfaceDraft): StaticObjectBody =>
  surfaceBodyOf(surface, draft.name ?? '', draft.geometry);

/** 작업면 요청 하나(또는 둘). 새 버전이나 새 작업면을 만들었으면 그 응답을, 보관만 했으면 undefined 를 돌려준다. */
async function sendSurface(
  ops: CommitOps,
  linked: WorkSurface | null,
  draft: SurfaceDraft | null,
): Promise<StaticObjectItem | undefined> {
  if (draft === null) {
    if (linked) await ops.archiveSurface(linked.objectId);
    return undefined;
  }
  if (linked !== null && linked.objectId === draft.id) {
    return ops.saveSurface({ surface: linked, body: bodyOfDraft(linked, draft) });
  }
  // 새로 만든 작업면이 이은 작업면을 대신하면(지우고 다시 추가) 옛 작업면을 먼저 보관한다. 구역에는 하나뿐이다.
  if (linked) await ops.archiveSurface(linked.objectId);
  return ops.createSurface(bodyOfDraft(null, draft));
}

/**
 * 작업면 단계. draft: undefined 는 건드리지 않음, null 은 이은 작업면을 보관(삭제), 객체는 새 작업면 만들기 또는
 * 이은 작업면의 새 버전이다. changed 가 거짓이면 보내지 않는다.
 */
async function surfaceStep(
  ops: CommitOps,
  outcome: CommitOutcome,
  linked: WorkSurface | null,
  draft: SurfaceDraft | null | undefined,
  changed: boolean,
): Promise<CommitOutcome> {
  if (draft === undefined || !changed) return outcome;
  if (draft === null && linked === null) return outcome;
  try {
    const surface = await sendSurface(ops, linked, draft);
    return withSaved(outcome, 'surface', surface ? { surface } : {});
  } catch (error) {
    return withFailure(outcome, 'surface', error);
  }
}

export interface SaveZoneCommit {
  zone: Zone;
  form: ZoneForm;
  /** 고친 구역 상자(고치지 않았으면 null). */
  box: ViewerEditBox | null;
  /** 구역 속성이나 상자를 고쳤는지. 작업면만 고쳤으면 구역은 새 버전을 만들지 않는다. */
  zoneChanged: boolean;
  linked: WorkSurface | null;
  surface: SurfaceDraft | null | undefined;
  surfaceChanged: boolean;
}

/** 기존 구역 저장: 구역 새 버전 → 작업면 → 발행. */
export async function commitSave(ops: CommitOps, input: SaveZoneCommit): Promise<CommitOutcome> {
  let outcome = emptyOutcome();
  if (input.zoneChanged) {
    const { zone, form, box } = input;
    outcome = await zoneStep(outcome, () => ops.saveZone({ zone, form, box }));
    if (outcome.failed) return outcome;
  }
  outcome = await surfaceStep(ops, outcome, input.linked, input.surface, input.surfaceChanged);
  if (outcome.failed) return outcome;
  return publishIfSaved(ops, outcome);
}

/** 새 구역 만들기: 구역(POST) → 새 작업면(POST) → 발행. 구역을 만든 뒤 작업면이 실패하면 구역은 남는다. */
export async function commitCreate(
  ops: CommitOps,
  input: { body: ZoneBody; surface: SurfaceDraft | null | undefined },
): Promise<CommitOutcome> {
  let outcome = await zoneStep(emptyOutcome(), () => ops.createZone(input.body));
  if (outcome.failed) return outcome;
  if (input.surface) {
    outcome = await surfaceStep(ops, outcome, null, input.surface, true);
    if (outcome.failed) return outcome;
  }
  return publishIfSaved(ops, outcome);
}

/** 구역 삭제(보관): 구역 → 이은 작업면 → 발행. */
export async function commitArchive(
  ops: CommitOps,
  input: { zoneId: string; linked: WorkSurface | null },
): Promise<CommitOutcome> {
  let outcome = await zoneStep(emptyOutcome(), async () => {
    await ops.archiveZone(input.zoneId);
    return undefined;
  });
  if (outcome.failed) return outcome;
  if (input.linked) {
    outcome = await surfaceStep(ops, outcome, input.linked, null, true);
    if (outcome.failed) return outcome;
  }
  return publishIfSaved(ops, outcome);
}

/** 저장 없이 발행만 다시 보낸다(저장은 했지만 발행이 실패했을 때). */
export const commitPublish = (ops: CommitOps): Promise<CommitOutcome> =>
  publishStep(ops, emptyOutcome());
