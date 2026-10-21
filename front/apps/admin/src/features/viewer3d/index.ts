// viewer3d 의 입구. 바깥에서는 여기서만 import 한다(front/CONVENTION.md 「의존 방향」).
export { createExternalSource } from '@/features/viewer3d/data/spatialSource';
export type {
  ExternalSpatialSource,
  SourceConnection,
  SourceConnectionChange,
  SpatialSource,
  SpatialSourceSink,
  ZonesResult,
} from '@/features/viewer3d/data/spatialSource';
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
