import { z } from 'zod';

// 이벤트 클립 REST(명세 12.6 GET /api/clips/{id}·PUT /api/clips/{id}/retention, FR-59·FR-99, BE 곽나혁).
// 응답 형식과 이벤트별 클립 목록 API 가 아직 정해지지 않아(노션 API 표 v12 도 경로만 있다) 아래 형식을 가정한다.
// 클립 칸 이름은 계약 media_clip(Tier 0 → EC2 기록)과 같다. contract 표시가 있는 응답만 받는다.
// 형식이 정해지면 이 파일과 mocks/clips.ts 를 바꾼다.

const DEMO = z.literal('DEMO_UNCONFIRMED');

/** 클립 한 개(카메라 하나 × 위험 구간 하나). 화면이 쓰는 칸만 검사한다. */
export const clipSchema = z.object({
  clip_id: z.string().min(1),
  camera_id: z.string().min(1),
  /** 이벤트 재개 횟수. 0 은 처음 발생이고 재개마다 새 클립이 생긴다. */
  resume_count: z.number().int().min(0),
  clip_start_capture_ms: z.number(),
  clip_end_capture_ms: z.number(),
  truncated_start: z.boolean(),
  truncated_end: z.boolean(),
  has_gaps: z.boolean(),
  retention_hold: z.boolean(),
  /** EC2 가 보관 기간(14일)이 지나 지운 시각. 지운 클립은 재생할 수 없다. */
  deleted_at_ms: z.number().nullish(),
});

export type Clip = z.infer<typeof clipSchema>;

/** GET /api/events/{id}/clips(가정, 관리자). 형식이 틀린 클립 하나는 빼고 나머지를 보인다. */
export const eventClipsSchema = z.object({
  contract: DEMO,
  event_id: z.string(),
  clips: z.array(z.unknown()).transform((items) =>
    items.flatMap((item) => {
      const clip = clipSchema.safeParse(item);
      return clip.success ? [clip.data] : [];
    }),
  ),
});

/**
 * GET /api/clips/{id}(관리자): 클립을 잠깐 받을 수 있는 임시 서명 주소.
 * Tier 0 원본은 h264-annexb 라 브라우저가 바로 재생하지 못한다. EC2 가 MP4 로 감싸 준다고 가정한다.
 */
export const clipUrlSchema = z.object({
  contract: DEMO,
  clip_id: z.string(),
  url: z.string().min(1),
  expires_at_ms: z.number(),
});

export type ClipUrl = z.infer<typeof clipUrlSchema>;
