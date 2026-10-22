import { z } from 'zod';

// 구역 REST(GET /api/zones). 응답 형식 계약이 아직 없어(BE -120·FE -189) 3D 뷰어 데모와 같은 직사각형 형식을 가정한다
// (viewer3d contractAdapter 의 DEMO 구역). contract 표시가 있는 응답만 받는다. 실제 형식이 정해지면 이 파일과 zones.ts 를 바꾼다.

export const zonesResponseSchema = z.object({
  contract: z.literal('DEMO_UNCONFIRMED'),
  // 구역 하나가 틀려도 나머지는 그린다. 항목은 zones.ts 에서 하나씩 검사한다.
  zones: z.array(z.unknown()),
});

export type ZonesResponse = z.infer<typeof zonesResponseSchema>;

const point = z.tuple([z.number(), z.number(), z.number()]);

export const zoneItemSchema = z.object({
  // 공백만 있는 ID 는 받지 않는다. 값은 자르지 않는다(이벤트의 zone_id 와 그대로 맞춘다).
  zone_id: z.string().refine((id) => id.trim() !== ''),
  name: z.string(),
  aabb: z.object({ min_xyz: point, max_xyz: point }),
});

type Point = readonly [number, number, number];

/** 구역 하나. 축에 맞춘 상자이고 좌표는 World(m, +Z 위)다. 2D 평면은 바닥 직사각형(x·y)만 쓴다. */
export interface ZoneBox {
  zoneId: string;
  name: string;
  min: Point;
  max: Point;
}
