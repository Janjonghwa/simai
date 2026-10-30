import { z } from 'zod';

// 규칙 현황 REST(명세 12.6 GET /api/rules, FR-3·113, docs/design/server-rules-scenes.md). 로그인한 모든 역할이 읽는다.
// 화면이 쓰는 칸만 검사한다. 규칙 하나가 틀려도 나머지는 그리고, 임계값 모양은 규칙마다 달라 객체로만 받는다.

const text = z.string().nullish().catch(undefined);

/** 지금 쓰는 규칙 판. 규칙이 비활성이거나 R6 처럼 자기 판이 없으면 null. */
const currentSchema = z.object({
  rule_version: z.number().int(),
  profile: text,
  thresholds: z.record(z.string(), z.unknown()).nullish().catch(undefined),
  /** 규칙이 판정마다 정하면(R1 은 구역 등급) null. */
  default_severity: text,
});

const ruleSchema = z.object({
  rule_id: z.string().min(1),
  name: text,
  stage: text,
  active: z.boolean(),
  current: currentSchema.nullish().catch(undefined),
  /** 자기 판이 없는 규칙(R6)이 함께 적용되는 규칙. */
  applied_with: z.array(z.string()).nullish().catch(undefined),
});

export type Rule = z.infer<typeof ruleSchema>;

export const rulesSchema = z.object({
  /** OPERATION 이 운영, CLASSROOM_TEST 가 교실 시험 프로파일. */
  rule_profile: z.string(),
  config_version: text,
  /** 발행만 하고 아직 적용 전인 더 새 구성 묶음. 없으면 null. */
  pending_config_version: text,
  rules: z.array(z.unknown()).transform((items) =>
    items.flatMap((item) => {
      const rule = ruleSchema.safeParse(item);
      return rule.success ? [rule.data] : [];
    }),
  ),
});

export type RulesResponse = z.infer<typeof rulesSchema>;
