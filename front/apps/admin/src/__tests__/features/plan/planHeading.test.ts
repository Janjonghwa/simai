import {
  bodyYaw,
  FLIP_CONFIRM_MS,
  HEADING_HOLD_MS,
  HEADING_SPEED_MIN,
  MOVE_CONFIRM_MS,
  nextHeadings,
  NO_HEADINGS,
  smoothYaw,
  velocityYaw,
  type HeadingMemory,
} from '@/features/plan/planHeading';
import type { WorldTrack } from '@simai/contracts';
import { describe, expect, it } from 'vitest';

// 2D 평면 사람 표식의 진행 방향(-800). 틀린 방향을 보이느니 보이지 않는다:
// 이어진 이동 → 엉덩이와 맞는 관측 어깨 → 2초 동안 마지막 방향 → 없음, 반대로 뒤집힘은 0.5초 확인 뒤에만.

type Skeleton = NonNullable<WorldTrack['skeleton']>;
type Joint = [number, number, number];
type Pair = readonly [Joint, Joint];

/** 앞(+Y)을 보는 사람: 왼쪽 관절이 −X, 오른쪽 관절이 +X. */
const SHOULDERS_PLUS_Y: Pair = [
  [-0.2, 0, 1.4],
  [0.2, 0, 1.4],
];
const HIPS_PLUS_Y: Pair = [
  [-0.15, 0, 0.9],
  [0.15, 0, 0.9],
];
const swap = ([l, r]: Pair): Pair => [r, l];

/** COCO17 골격. 어깨(5·6)·엉덩이(11·12)만 주고 나머지는 없음(null·UNAVAILABLE)이다. */
function skeleton(
  shoulders: Pair,
  hips: Pair | null = HIPS_PLUS_Y,
  change: { validity?: Skeleton['validity'][number]; source?: 'observed' | 'inferred' } = {},
): Skeleton {
  const joints: Skeleton['joints'] = Array.from({ length: 17 }, () => null);
  const validity: Skeleton['validity'] = Array.from({ length: 17 }, () => 'UNAVAILABLE');
  const visible = Array.from({ length: 17 }, () => false);
  const source: Skeleton['joint_source'] = Array.from({ length: 17 }, () => 'observed');
  const set = (index: number, point: Joint, shoulder: boolean) => {
    joints[index] = point;
    validity[index] = shoulder ? (change.validity ?? 'VALID') : 'VALID';
    visible[index] = true;
    source[index] = shoulder ? (change.source ?? 'observed') : 'observed';
  };
  set(5, shoulders[0], true);
  set(6, shoulders[1], true);
  if (hips) {
    set(11, hips[0], false);
    set(12, hips[1], false);
  }
  return { joint_layout: 'COCO17', joints, visible, validity, joint_source: source };
}

/** 앞(+Y)을 보는 사람의 골격과, 좌우가 통째로 바뀐(뒤에서 찍힌) 골격. */
const FACING_PLUS_Y = skeleton(SHOULDERS_PLUS_Y);
const MIRRORED = skeleton(swap(SHOULDERS_PLUS_Y), swap(HIPS_PLUS_Y));

function track(change: Partial<WorldTrack> = {}): WorldTrack {
  return {
    world_id: 'w-1',
    position_xy: [2, 3],
    position_xy_sigma: [0.1, 0.1],
    observation_state: 'OBSERVED',
    track_lifecycle: 'CONFIRMED',
    ...change,
  } as unknown as WorldTrack;
}

/** 10Hz world 를 차례로 넣고 각 시각의 보이는 방향(없으면 null)을 돌려준다. */
function run(
  frames: readonly (readonly [number, WorldTrack])[],
  start: HeadingMemory = NO_HEADINGS,
) {
  let memory = start;
  const shown: (number | null)[] = [];
  for (const [t, frame] of frames) {
    memory = nextHeadings(memory, [frame], t);
    shown.push(memory.get(frame.world_id)?.yaw ?? null);
  }
  return { memory, shown };
}

/** 같은 표본을 t0 부터 100ms 간격으로 n 번. */
const repeat = (frame: WorldTrack, n: number, t0 = 0) =>
  Array.from({ length: n }, (_, i) => [t0 + i * 100, frame] as const);

describe('이동 방향', () => {
  it('수평 속력이 기준 이상이면 그 방향이다(+X 는 0, +Y 는 π/2)', () => {
    expect(velocityYaw(track({ velocity: [1, 0, 0] }))).toBeCloseTo(0);
    expect(velocityYaw(track({ velocity: [0, 0.5, 0] }))).toBeCloseTo(Math.PI / 2);
    expect(velocityYaw(track({ velocity: [-0.3, -0.3, 0] }))).toBeCloseTo((-3 * Math.PI) / 4);
  });

  it('느리면(서 있을 때 흔들림) 쓰지 않고, 위아래 속도는 보지 않는다', () => {
    expect(velocityYaw(track({ velocity: [HEADING_SPEED_MIN * 0.9, 0, 0] }))).toBeNull();
    expect(velocityYaw(track({ velocity: [0, 0, 2] }))).toBeNull();
    expect(velocityYaw(track())).toBeNull();
  });

  it('기준 이상 속력이 잠깐 이어진 뒤에만 이동 방향을 보인다', () => {
    const walking = track({ velocity: [1, 0, 0] });
    const { shown } = run(repeat(walking, MOVE_CONFIRM_MS / 100 + 1));
    expect(shown.slice(0, -1).every((yaw) => yaw === null)).toBe(true);
    expect(shown.at(-1)).toBeCloseTo(0);
  });

  it('한 표본만 빨랐다가 멈추면 이동 방향으로 보지 않는다', () => {
    const { shown } = run([
      [0, track({ velocity: [1, 0, 0] })],
      [100, track({ velocity: [0.05, 0, 0] })],
      [200, track({ velocity: [1, 0, 0] })],
    ]);
    expect(shown).toEqual([null, null, null]);
  });

  it('걷고 있으면 몸 방향보다 이동 방향이 먼저다', () => {
    const walking = track({ velocity: [1, 0, 0.0], skeleton: FACING_PLUS_Y });
    const { shown } = run(repeat(walking, 6));
    expect(shown.at(-1)!).toBeLessThan(Math.PI / 4);
  });
});

describe('멈췄을 때 몸 방향(어깨·엉덩이)', () => {
  it('관측한 두 어깨로 본 앞쪽이 두 엉덩이와 맞으면 그 방향이다', () => {
    expect(bodyYaw(track({ skeleton: FACING_PLUS_Y }))).toBeCloseTo(Math.PI / 2);
    // +X 를 보는 사람: 왼쪽이 +Y, 오른쪽이 −Y.
    const facingX = skeleton(
      [
        [0, 0.2, 1.4],
        [0, -0.2, 1.4],
      ],
      [
        [0, 0.15, 0.9],
        [0, -0.15, 0.9],
      ],
    );
    expect(bodyYaw(track({ skeleton: facingX }))).toBeCloseTo(0);
    // 멈춰 서면 첫 표본부터 보인다.
    expect(run([[0, track({ skeleton: FACING_PLUS_Y })]]).shown[0]).toBeCloseTo(Math.PI / 2);
  });

  it('어깨와 엉덩이가 맞지 않거나(어깨만 좌우가 바뀜) 엉덩이를 모르면 쓰지 않는다', () => {
    expect(bodyYaw(track({ skeleton: skeleton(swap(SHOULDERS_PLUS_Y)) }))).toBeNull();
    // 엉덩이를 60° 돌린 몸(어깨와 45° 넘게 다름).
    const c = Math.cos(Math.PI / 3) * 0.15;
    const s = Math.sin(Math.PI / 3) * 0.15;
    const twisted: Pair = [
      [-c, -s, 0.9],
      [c, s, 0.9],
    ];
    expect(bodyYaw(track({ skeleton: skeleton(SHOULDERS_PLUS_Y, twisted) }))).toBeNull();
    expect(bodyYaw(track({ skeleton: skeleton(SHOULDERS_PLUS_Y, null) }))).toBeNull();
    expect(run([[0, track({ skeleton: skeleton(swap(SHOULDERS_PLUS_Y)) })]]).shown).toEqual([null]);
  });

  it('불확실·추정(inferred) 어깨, 겹친 어깨, 예측 표본, 지원하지 않는 관절 배열은 쓰지 않는다', () => {
    const uncertain = skeleton(SHOULDERS_PLUS_Y, HIPS_PLUS_Y, { validity: 'UNCERTAIN' });
    const inferred = skeleton(SHOULDERS_PLUS_Y, HIPS_PLUS_Y, { source: 'inferred' });
    const overlapped = skeleton([
      [0, 0, 1.4],
      [0.05, 0, 1.4],
    ]);
    expect(bodyYaw(track({ skeleton: uncertain }))).toBeNull();
    expect(bodyYaw(track({ skeleton: inferred }))).toBeNull();
    expect(bodyYaw(track({ skeleton: overlapped }))).toBeNull();
    expect(bodyYaw(track({ skeleton: FACING_PLUS_Y, observation_state: 'PREDICTED' }))).toBeNull();
    expect(bodyYaw(track({ skeleton: { ...FACING_PLUS_Y, joint_layout: 'MHR70' } }))).toBeNull();
  });
});

describe('뒤집힘 막기', () => {
  const facing = track({ skeleton: FACING_PLUS_Y });
  const mirrored = track({ skeleton: MIRRORED });

  it('좌우가 바뀐 관절이 한두 표본 섞여도 방향을 뒤집지 않는다', () => {
    const { shown } = run([
      ...repeat(facing, 3),
      [300, mirrored],
      [400, mirrored],
      [500, facing],
      [600, facing],
    ]);
    for (const yaw of shown) expect(yaw).toBeCloseTo(Math.PI / 2);
  });

  it(`반대 방향이 ${FLIP_CONFIRM_MS}ms 연속으로 이어지면 그때 바꾼다`, () => {
    const { shown } = run([...repeat(facing, 3), ...repeat(mirrored, 6, 300)]);
    // 300~700ms 는 지금 방향을 둔다.
    for (const yaw of shown.slice(0, 8)) expect(yaw).toBeCloseTo(Math.PI / 2);
    // 300 + 500ms 에 바꾼다.
    expect(shown[8]).toBeCloseTo(-Math.PI / 2);
  });

  it('반대 방향 사이에 한 표본이라도 끊기면 처음부터 다시 센다', () => {
    const { shown } = run([
      ...repeat(facing, 3),
      ...repeat(mirrored, 4, 300),
      [700, track()],
      ...repeat(mirrored, 4, 800),
    ]);
    for (const yaw of shown) expect(yaw).toBeCloseTo(Math.PI / 2);
  });
});

describe('방향 기억', () => {
  it('각도는 짧은 쪽으로 돌아 따라간다(170° → −170° 는 180° 를 지난다)', () => {
    const from = (170 * Math.PI) / 180;
    const to = (-170 * Math.PI) / 180;
    expect(Math.abs(smoothYaw(from, to, 100))).toBeGreaterThan(from);
  });

  it('근거가 끊기면 2초 동안 마지막 방향을 두고, 그 뒤에는 방향을 지운다(점만)', () => {
    const { memory } = run([[0, track({ skeleton: FACING_PLUS_Y })]]);
    const held = run([[HEADING_HOLD_MS, track()]], memory);
    expect(held.shown[0]).toBeCloseTo(Math.PI / 2);
    const gone = run([[HEADING_HOLD_MS + 100, track()]], held.memory);
    expect(gone.shown).toEqual([null]);
  });

  it('확인 불가가 되거나 사라진 사람의 방향은 버리고, 병합된 이전 ID 의 방향은 이어받는다', () => {
    const { memory } = run([[0, track({ skeleton: FACING_PLUS_Y })]]);
    expect(nextHeadings(memory, [track({ observation_state: 'UNKNOWN' })], 100).size).toBe(0);
    expect(nextHeadings(memory, [], 100).size).toBe(0);
    const merged = nextHeadings(memory, [track({ world_id: 'w-2', aliases: ['w-1'] })], 100);
    expect(merged.get('w-2')?.yaw).toBeCloseTo(Math.PI / 2);
  });
});
