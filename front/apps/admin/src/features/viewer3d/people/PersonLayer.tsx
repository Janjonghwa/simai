import type { SpatialDataState } from '@/features/viewer3d/data/spatialData.store';
import { useSpatialData, useSpatialDataStore } from '@/features/viewer3d/data/SpatialDataProvider';
import {
  trackPlacement,
  type TrackView,
  type WorldFrame,
} from '@/features/viewer3d/data/spatialFrame';
import { LabelSprite } from '@/features/viewer3d/LabelSprite';
import {
  instantiateAvatar,
  useAvatarModel,
  type AvatarInstance,
  type AvatarModel,
} from '@/features/viewer3d/people/avatarModel';
import {
  approach,
  approachAngle,
  headingFromSamples,
  isWalking,
  needsSnap,
  personLabel,
  personPose,
  personTone,
  POSITION_RATE,
  WALK_RATE,
  YAW_RATE,
  type PersonPose,
  type PersonTone,
} from '@/features/viewer3d/people/personMotion';
import { PersonRiskMark } from '@/features/viewer3d/risk/RiskMarks';
import { riskLabel, riskStyle } from '@/features/viewer3d/risk/riskPresentation';
import { useRiskHighlight } from '@/features/viewer3d/risk/RiskScope';
import { Selectable } from '@/features/viewer3d/Selectable';
import { useViewerStore } from '@/features/viewer3d/ViewerStoreContext';
import type { ThreePoint } from '@/features/viewer3d/worldCoordinates';
import { Line } from '@react-three/drei';
import { useFrame } from '@react-three/fiber';
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type RefObject,
} from 'react';
import type { Group, MeshStandardMaterial } from 'three';
import { useStore } from 'zustand';

// -44 사람 레이어. -194 Provider 의 World 를 같은 Canvas·Scene 안에서 world_id 마다 하나씩 그린다.
// React 는 사람 목록이 바뀔 때만 다시 그리고, 10 Hz 위치·방향·걷기 동작은 useFrame 에서 기존 Group 만 바꾼다.

/** 사람 모델 주소(XBot 같은 리깅 glTF). 없으면 캡슐 아바타. 배포 경로가 정해지기 전에는 개발 화면만 넘긴다. */
export const PersonModelContext = createContext<string | null>(null);

// WebGL 재질은 CSS 토큰을 바로 읽지 못해 장면 전용 값을 여기에만 둔다.
const COLOR = { body: 0xdde3ef, joint: 0x7d89a8, unknown: 0x6b7797, selected: 0x6886d5 };
const OPACITY: Record<PersonTone, number> = { observed: 1, predicted: 0.45, unknown: 0.3 };
const LABEL_COLOR: Record<PersonTone, string> = {
  observed: '#eef1f8',
  predicted: '#9ba7c5',
  unknown: '#9ba7c5',
};
const LABEL_Y = 2.05;
const COLUMN_TOP = 2.0;
const noRaycast = () => null;

const NO_LAST_VALID: ThreePoint | null = null;
// 렌더에서 표시 종류만 고를 때 쓰는 자리표시자. 실제 위치는 useFrame 이 마지막 유효 위치로 정한다.
const SOME_LAST_VALID = [0, 0, 0] as unknown as ThreePoint;

const trackOf = (world: WorldFrame | null, worldId: string): TrackView | undefined =>
  world?.tracks.find((track) => track.worldId === worldId);

/** 사람 목록(순서 유지). 문자열로 묶어 목록이 같으면 다시 그리지 않는다. */
const selectIds = (state: SpatialDataState) =>
  (state.world?.tracks ?? []).map((track) => track.worldId).join('\n');

/** 이 사람의 표시가 바뀌는 상태만 묶는다(관측 상태·위치 근거·연결·ID 교정). 좌표는 넣지 않는다. */
function statusKey(state: SpatialDataState, worldId: string) {
  const track = trackOf(state.world, worldId);
  if (!track) return '';
  return [
    track.observationState,
    trackPlacement(track).basis,
    state.connection?.state ?? 'NONE',
    track.identityRevision,
  ].join('|');
}

/** 이름의 문자 코드로 0~1 위상을 만든다. 사람마다 동작 시작점이 어긋나게 한다. */
const phaseOf = (id: string) => ([...id].reduce((sum, ch) => sum + ch.charCodeAt(0), 0) % 97) / 97;

export function PersonLayer() {
  const sourceId = useSpatialData((s) => s.sourceId);
  const idsKey = useSpatialData(selectIds);
  const ids = useMemo(() => (idsKey ? idsKey.split('\n') : []), [idsKey]);
  const model = useAvatarModel(useContext(PersonModelContext));
  return (
    <group name="person-layer">
      {ids.map((id) => (
        // Source 가 바뀌면 key 가 달라져 이전 Source 의 위치·방향·동작 기록을 이어받지 않는다.
        <PersonAvatar key={`${sourceId}:${id}`} worldId={id} model={model} />
      ))}
    </group>
  );
}

interface Motion {
  world: WorldFrame | null;
  pose: PersonPose | null;
  lastValid: ThreePoint | null;
  revision: number | null;
  sampleAt: number;
  observation: TrackView['observationState'];
  display: [number, number, number];
  yaw: number;
  heading: number | null;
  speed: number;
  /** 0 = idle, 1 = walk. */
  walk: number;
}

function PersonAvatar({ worldId, model }: { worldId: string; model: AvatarModel | null }) {
  const store = useSpatialDataStore();
  const selected = useStore(
    useViewerStore(),
    (s) => s.selected?.type === 'person' && s.selected.id === worldId,
  );
  // 관측 상태·위치 근거·연결이 바뀔 때만 다시 그린다.
  useSpatialData((s) => statusKey(s, worldId));
  // 위험 강조는 이벤트가 바뀔 때만 달라진다. 위치·동작 기록(motion)은 그대로 둔다.
  const risk = useRiskHighlight('person', worldId);
  const [hasLastValid, setHasLastValid] = useState(false);
  const root = useRef<Group>(null);
  // 위험 표식 자리. 선택 그룹 밖에 두어 초점 중심·클릭 경계에 끼지 않게 하고, 위치만 아바타를 따라간다.
  const riskRoot = useRef<Group>(null);
  // 표식이 새로 붙는 순간에도 원점에 한 프레임 보이지 않게 지금 아바타 위치에서 시작한다.
  const attachRisk = useCallback((group: Group | null) => {
    riskRoot.current = group;
    if (group && root.current) group.position.copy(root.current.position);
  }, []);
  const body = useRef<Group>(null);
  const motion = useRef<Motion>({
    world: null,
    pose: null,
    lastValid: null,
    revision: null,
    sampleAt: 0,
    observation: 'OBSERVED',
    display: [0, 0, 0],
    yaw: 0,
    heading: null,
    speed: 0,
    walk: 0,
  });

  useFrame((_, dt) => {
    const group = root.current;
    const data = store.getState();
    const m = motion.current;
    if (!group || !data.world) return;

    // 새 World 표본이 왔을 때만 목표를 다시 정한다.
    if (data.world !== m.world) {
      m.world = data.world;
      const track = trackOf(data.world, worldId);
      if (track) {
        const pose = personPose(track, m.lastValid);
        const at = data.world.meta.receivedAt;
        let { heading, speed } = pose;
        // 계약 velocity 가 없으면 연속한 두 관측 위치로만 방향을 구한다.
        if (heading === null && pose.fresh && pose.feet && m.pose?.fresh && m.pose.feet) {
          const derived = headingFromSamples(m.pose.feet, pose.feet, at - m.sampleAt);
          if (derived && track.observationState === 'OBSERVED') ({ heading, speed } = derived);
        }
        const snap = needsSnap(
          m.pose,
          pose,
          m.revision !== null && m.revision !== track.identityRevision,
        );
        if (pose.fresh && pose.feet) {
          m.lastValid = pose.feet;
          if (!hasLastValid) setHasLastValid(true);
        }
        m.pose = pose;
        m.revision = track.identityRevision;
        m.sampleAt = at;
        m.observation = track.observationState;
        m.speed = speed;
        if (heading !== null) m.heading = heading;
        if (snap) {
          const target = pose.feet ?? [pose.columnXz![0], 0, pose.columnXz![1]];
          m.display = [target[0], target[1], target[2]];
          if (m.heading !== null) m.yaw = m.heading;
        }
      }
    }

    const pose = m.pose;
    if (!pose) return;
    const target = pose.feet ?? [pose.columnXz![0], 0, pose.columnXz![1]];
    m.display = [
      approach(m.display[0], target[0], dt, POSITION_RATE),
      approach(m.display[1], target[1], dt, POSITION_RATE),
      approach(m.display[2], target[2], dt, POSITION_RATE),
    ];
    group.position.set(...m.display);
    riskRoot.current?.position.set(...m.display);
    if (m.heading !== null) m.yaw = approachAngle(m.yaw, m.heading, dt, YAW_RATE);
    if (body.current) body.current.rotation.y = m.yaw;
    const walking = isWalking({
      speed: m.speed,
      observationState: m.observation,
      connectionOpen: data.connection?.state === 'OPEN',
      frameAgeMs: Date.now() - m.sampleAt,
      fresh: pose.fresh,
    });
    m.walk = approach(m.walk, walking ? 1 : 0, dt, WALK_RATE);
  });

  const { world, connection } = store.getState();
  const track = trackOf(world, worldId);
  if (!track) return null;
  // 표시 종류·이름표만 정한다. 좌표는 useFrame 이 마지막 유효 위치로 채운다.
  const pose = personPose(track, hasLastValid ? SOME_LAST_VALID : NO_LAST_VALID);
  const tone = personTone(track);
  const base = personLabel(track, pose, connection?.state === 'OPEN');
  // 이름표 하나에 위험 > 대상 ID·관측 상태 순서로 적어 겹치지 않게 한다.
  const label = risk ? `${riskLabel(risk)} · ${base}` : base;

  return (
    <>
      <Selectable target={{ type: 'person', id: worldId }}>
        <group ref={root} name={`person-avatar:${worldId}`}>
          {pose.kind === 'BODY' ? (
            <group ref={body}>
              {model ? (
                <ModelBody
                  key={model.url}
                  model={model}
                  tone={tone}
                  selected={selected}
                  motion={motion}
                  worldId={worldId}
                />
              ) : (
                <CapsuleBody tone={tone} selected={selected} />
              )}
            </group>
          ) : (
            <HeightUnknownColumn selected={selected} />
          )}
          <LabelSprite
            text={label}
            y={pose.kind === 'BODY' ? LABEL_Y : COLUMN_TOP + 0.2}
            height={0.24}
            color={risk ? riskStyle(risk).labelColor : LABEL_COLOR[tone]}
          />
        </group>
      </Selectable>
      {risk && (
        <group ref={attachRisk}>
          <PersonRiskMark risk={risk} kind={pose.kind} worldId={worldId} />
        </group>
      )}
    </>
  );
}

function bodyColor(tone: PersonTone, selected: boolean) {
  if (selected) return COLOR.selected;
  return tone === 'unknown' ? COLOR.unknown : COLOR.body;
}

/** 캡슐 아바타(명세 13.1 MVP). 모델이 없어도 늘 그린다. 앞쪽 표식으로 진행 방향을 보인다. */
function CapsuleBody({ tone, selected }: { tone: PersonTone; selected: boolean }) {
  const color = bodyColor(tone, selected);
  const opacity = OPACITY[tone];
  const solid = tone === 'observed';
  return (
    <>
      <mesh position-y={0.78}>
        <capsuleGeometry args={[0.2, 1.06, 6, 14]} />
        <meshStandardMaterial
          color={color}
          transparent
          opacity={opacity}
          depthWrite={solid}
          roughness={0.6}
        />
      </mesh>
      <mesh position-y={1.58}>
        <sphereGeometry args={[0.13, 16, 12]} />
        <meshStandardMaterial
          color={color}
          transparent
          opacity={opacity}
          depthWrite={solid}
          roughness={0.6}
        />
      </mesh>
      <mesh position={[0, 1.58, 0.12]} raycast={noRaycast}>
        <boxGeometry args={[0.14, 0.05, 0.04]} />
        <meshBasicMaterial color={COLOR.joint} transparent opacity={opacity} />
      </mesh>
    </>
  );
}

/** 높이를 모르는 사람의 세로 표식. 바닥에 세우지 않고 높이 범위를 점선으로만 보인다(Z 를 지어내지 않는다). */
function HeightUnknownColumn({ selected }: { selected: boolean }) {
  const color = selected ? COLOR.selected : COLOR.unknown;
  return (
    <>
      <Line
        points={[
          [0, 0.1, 0],
          [0, COLUMN_TOP, 0],
        ]}
        color={color}
        lineWidth={2}
        dashed
        dashSize={0.12}
        gapSize={0.08}
        raycast={noRaycast}
      />
      {/* 클릭을 받는 투명 기둥. */}
      <mesh position-y={(COLUMN_TOP + 0.1) / 2}>
        <cylinderGeometry args={[0.18, 0.18, COLUMN_TOP - 0.1, 10]} />
        <meshBasicMaterial transparent opacity={0} depthWrite={false} />
      </mesh>
    </>
  );
}

interface ModelBodyProps {
  model: AvatarModel;
  tone: PersonTone;
  selected: boolean;
  motion: RefObject<Motion>;
  worldId: string;
}

/** 리깅 모델 아바타. 사람마다 복제본·mixer 를 따로 두고 Canvas 의 useFrame 으로만 돌린다. */
function ModelBody({ model, tone, selected, motion, worldId }: ModelBodyProps) {
  const holder = useRef<Group>(null);
  const instance = useRef<AvatarInstance | null>(null);

  // 복제·해제를 효과 안에서 짝지어 StrictMode 이중 마운트에도 남는 mixer·재질이 없게 한다.
  useEffect(() => {
    const parent = holder.current;
    if (!parent) return;
    const created = instantiateAvatar(model, phaseOf(worldId));
    parent.add(created.root);
    instance.current = created;
    return () => {
      parent.remove(created.root);
      created.dispose();
      instance.current = null;
    };
  }, [model, worldId]);

  // 위 효과 다음에 돌아 새 복제본에도 바로 칠한다.
  useEffect(() => {
    if (instance.current) paint(instance.current, tone, selected);
  }, [tone, selected, model, worldId]);

  useFrame((_, dt) => {
    const avatar = instance.current;
    if (!avatar) return;
    const { walk, speed } = motion.current;
    avatar.walk.setEffectiveWeight(walk);
    avatar.idle.setEffectiveWeight(1 - walk);
    // 걸음 빠르기를 실제 속력에 맞춘다(동작은 위치를 옮기지 않는다).
    avatar.walk.timeScale = Math.min(1.6, Math.max(0.5, speed / 1.1));
    avatar.mixer.update(dt);
  });

  return <group ref={holder} />;
}

/** 상태 색·투명도. 관절(Joints) 재질은 경보색과 헷갈리지 않게 무채색으로 둔다. */
function paint(avatar: AvatarInstance, tone: PersonTone, selected: boolean) {
  for (const material of avatar.materials) {
    const m = material as MeshStandardMaterial;
    const joint = /joints/i.test(m.name);
    m.color?.setHex(
      selected
        ? COLOR.selected
        : tone === 'unknown'
          ? COLOR.unknown
          : joint
            ? COLOR.joint
            : COLOR.body,
    );
    m.opacity = OPACITY[tone];
    m.depthWrite = tone === 'observed';
    if ('metalness' in m) {
      m.metalness = 0.05;
      m.roughness = 0.65;
    }
    m.needsUpdate = true;
  }
}
