// viewer3d 의 입구. 바깥에서는 여기서만 import 한다(front/CONVENTION.md 「의존 방향」).
export { createExternalSource } from '@/features/viewer3d/data/spatialSource';
export type {
  ExternalSpatialSource,
  SourceConnection,
  SourceConnectionChange,
  SpatialSource,
  SpatialSourceSink,
  WorkSurfacesResult,
  ZonesResult,
} from '@/features/viewer3d/data/spatialSource';
export {
  EDIT_FACES,
  EDIT_FIELDS,
  OBJECT_EDIT_FIELDS,
  WORK_SURFACE_OBB_POLICY,
  ZONE_AABB_POLICY,
  applyEditOperation,
  applyEditOperations,
  applyZoneBoxOperation,
  boxJson,
  fieldOperation,
  isValidBox,
  objectFieldOperations,
  restoreZoneBoxSteps,
  sameBox,
} from '@/features/viewer3d/editing/editGeometry';
export type {
  BoxEditOutcome,
  EditFace,
  EditFailureReason,
  EditField,
  EditOperation,
  EditOutcome,
  EditPolicy,
  EditShape,
  ObjectEditField,
  SerializableEditBox,
  WorldBox,
  WorldObb,
} from '@/features/viewer3d/editing/editGeometry';
export type {
  ViewerEditBox,
  ViewerEditCancelReason,
  ViewerEditCommandResult,
  ViewerEditField,
  ViewerEditOverlay,
  ViewerEditOverlayInput,
  ViewerEditPreview,
  ViewerEditProvenance,
  ViewerEditRejectReason,
  ViewerEditResult,
  ViewerObjectEditField,
  ViewerObjectEditOrigin,
  ViewerObjectEditPreview,
  ViewerObjectEditProvenance,
  ViewerObjectEditResult,
  ViewerWorkSurfaceGeometry,
} from '@/features/viewer3d/editing/editTypes';
export { parseReplayLog } from '@/features/viewer3d/replay/replayLog';
export type {
  ReplayFile,
  ReplayIssue,
  ReplayIssueReason,
  ReplayLog,
} from '@/features/viewer3d/replay/replayLog';
export { REPLAY_SPEEDS, createReplaySource } from '@/features/viewer3d/replay/replaySource';
export type {
  ReplaySourceOptions,
  ReplaySpatialSource,
  ReplayStatus,
} from '@/features/viewer3d/replay/replaySource';
export {
  adaptWorkSurfaces,
  geometryProblem,
  obbWorkSurface,
  sameWorkSurface,
  surfaceCorners,
  surfaceTopEdge,
  workSurfaceItem,
  workSurfaceObb,
} from '@/features/viewer3d/surfaces/workSurfaceGeometry';
export type {
  WorkSurfaceGeometry,
  WorkSurfaceSnapshot,
  WorkSurfaceView,
} from '@/features/viewer3d/surfaces/workSurfaceGeometry';
export type {
  CommandFailureReason,
  CommandResult,
  Viewer3DHandle,
  Viewer3DProps,
  ViewerError,
  ViewerErrorCode,
  ViewerMode,
  ViewerPhase,
  ViewerStatus,
  ViewerTarget,
  ViewerTargetType,
} from '@/features/viewer3d/types';
export { Viewer3D } from '@/features/viewer3d/Viewer3D';
export { toWorldPoint, worldPoint, type WorldPoint } from '@/features/viewer3d/worldCoordinates';
