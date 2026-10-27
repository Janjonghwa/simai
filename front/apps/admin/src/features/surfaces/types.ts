import { z } from 'zod';

// 작업면·정적 객체 REST(GET·POST·PUT /api/static-objects, BE -120 의 geometry 도메인). 서버 형식 그대로다.
// 이 화면은 작업면(WORK_SURFACE)만 다룬다. 장애물(OBSTACLE)은 목록에서 뺀다.

/** 목록. 항목 하나가 틀려도 나머지는 그린다. 항목은 surfaces.ts 에서 하나씩 검사한다. */
export const staticObjectsResponseSchema = z.array(z.unknown());
export type StaticObjectsResponse = z.infer<typeof staticObjectsResponseSchema>;

export const STATIC_OBJECT_KINDS = ['WORK_SURFACE', 'OBSTACLE'] as const;
export type StaticObjectKind = (typeof STATIC_OBJECT_KINDS)[number];

const vec3 = z.tuple([z.number(), z.number(), z.number()]);

export const staticObjectItemSchema = z.object({
  // 공백만 있는 ID 는 받지 않는다. 값은 자르지 않는다(world_state support_surface_id 와 그대로 맞춘다).
  object_id: z.string().refine((id) => id.trim() !== ''),
  object_version: z.number().int().positive(),
  kind: z.enum(STATIC_OBJECT_KINDS),
  name: z.string().nullable(),
  // 가운데 [x, y, z], 가로·세로·높이 전체 길이, +Z 축 회전(rad). 작업면은 바닥(Z=0)부터 상판까지다.
  obb: z.object({ center: vec3, size: vec3, yaw_rad: z.number() }),
  // 상판 높이(m). 서버가 real(float4)로 저장해 0.74 가 0.7400000095367432 처럼 돌아온다.
  top_height_m: z.number().nullable(),
  // 상판 높이 표준편차(m). 화면에서 고치지 않고 받은 값을 그대로 다시 보낸다(없으면 Tier 0 가 0.02 로 읽는다).
  top_height_sigma_m: z.number().nullish(),
  // 이 버전이 유효해진 시각(epoch ms, 서버가 저장할 때 정한다). 지금 버전은 valid_to 가 없다.
  valid_from: z.number(),
  valid_to: z.number().nullable(),
});
export type StaticObjectItem = z.infer<typeof staticObjectItemSchema>;

/** 만들기(POST)·새 버전(PUT) 본문. 서버가 ID·버전·유효 시각을 정한다. 응답은 새로 생긴 버전 하나다. */
export const staticObjectBodySchema = z.object({
  kind: z.enum(STATIC_OBJECT_KINDS),
  name: z.string().nullable(),
  obb: z.object({ center: vec3, size: vec3, yaw_rad: z.number() }),
  top_height_m: z.number().nullable(),
  top_height_sigma_m: z.number().nullable(),
});
export type StaticObjectBody = z.infer<typeof staticObjectBodySchema>;

/** 작업면 형상(World m·rad). 3D 뷰어의 WorkSurfaceGeometry 와 같은 모양이다. */
export interface SurfaceGeometry {
  centerXy: [number, number];
  sizeXy: [number, number];
  yawRad: number;
  topHeightM: number;
}

/** 서버 작업면의 지금 버전. */
export interface WorkSurface {
  objectId: string;
  version: number;
  name: string | null;
  geometry: SurfaceGeometry;
  /** 받은 상판 높이 표준편차. 없으면 null. */
  sigma: number | null;
  /** 이 버전이 유효해진 시각(ms). 마지막 묶음보다 늦으면 아직 발행하지 않은 것이다. */
  validFrom: number;
}

/** 3D 뷰어 Source 가 받는 작업면 묶음(서버 구성 묶음 operational_geometry 의 work_surfaces 와 같은 항목). */
export interface ViewerWorkSurfaces {
  work_surfaces: {
    surface_id: string;
    top_height_m: number;
    center_xy: [number, number];
    size_xy: [number, number];
    yaw_rad: number;
    top_height_sigma_m?: number;
    name?: string;
  }[];
}
