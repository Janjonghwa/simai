import {
  BOX_EDIT_HANDLES,
  handleRank,
  type BoxEditArrow,
  type BoxEditHandle,
} from '@/features/viewer3d/editing/boxEdit';
import type { BoxEditDrag } from '@/features/viewer3d/editing/boxEditDrag';
import {
  ARROW_OPACITY,
  ARROW_ORDER,
  ARROW_QUATERNION,
  DOT,
  DOT_OPACITY,
  GIZMO_K,
  handleOpacity,
  handleTint,
  HEAD,
  HOVER_SCALE,
  MOVE_ORDER,
  noRaycast,
  PICK_AXIS,
  PICK_AXIS_OFFSET,
  PICK_DOT,
  RING,
  RING_OPACITY,
  SHAFT,
} from '@/features/viewer3d/editing/boxEditStyle';
import { createHandleRaycast } from '@/features/viewer3d/editing/handleInput';
import type { ViewerTheme } from '@/features/viewer3d/palette';
import { useViewerStore } from '@/features/viewer3d/ViewerStoreContext';
import type { ThreeBox } from '@/features/viewer3d/worldCoordinates';
import { useFrame, useThree, type ThreeEvent } from '@react-three/fiber';
import { useRef, useState, type Dispatch, type RefObject, type SetStateAction } from 'react';
import { DoubleSide, type Color, type Group, type Mesh } from 'three';
import { useStore } from 'zustand';

// 제어형 상자 편집 조작기의 손잡이: 가운데 구(바닥 이동)와 면별 화살표. 그리기와 포인터 이벤트 연결만 맡고 끌기 계산은 boxEditDrag 가 한다.

type HandleRaycast = ReturnType<typeof createHandleRaycast>;

/** 판정 형상의 userData. pickedHandle 이 겹친 판정 중 어느 손잡이 것인지 읽는다(렌더마다 새로 만들지 않는다). */
const PICK_DATA = Object.fromEntries(
  BOX_EDIT_HANDLES.map((handle) => [handle, { boxEditHandle: handle }]),
) as Record<BoxEditHandle, { boxEditHandle: BoxEditHandle }>;

interface HandleEvents {
  onPointerDown: (event: ThreeEvent<PointerEvent>) => void;
  onPointerMove: (event: ThreeEvent<PointerEvent>) => void;
  onPointerOut: () => void;
}

/** 손잡이 하나를 그리는 데 필요한 값. */
interface HandleViewProps {
  theme: ViewerTheme;
  /** 호버 중이거나 끄는 중이다. */
  on: boolean;
  /** 다른 손잡이를 끄는 중이다. */
  faded: boolean;
  raycast: HandleRaycast;
  events: HandleEvents;
}

/** 겹친 손잡이 중 순서가 가장 앞선 것(가운데 구 → 화살표, 같으면 가까운 것). 판정 정보가 없으면 받은 손잡이 그대로다. */
function pickedHandle(event: ThreeEvent<PointerEvent>, own: BoxEditHandle): BoxEditHandle {
  let best: BoxEditHandle | null = null;
  for (const hit of event.intersections ?? []) {
    const handle = hit.object.userData['boxEditHandle'] as BoxEditHandle | undefined;
    if (handle && (best === null || handleRank(handle) < handleRank(best))) best = handle;
  }
  return best ?? own;
}

/** 카메라 거리에 비례해 키워 화면에서 늘 같은 크기로 보이게 하고, 링은 카메라를 향하게 한다. */
function useConstantScreenSize(group: RefObject<Group | null>, ring: RefObject<Mesh | null>) {
  useFrame(({ camera }) => {
    if (!group.current) return;
    group.current.scale.setScalar(camera.position.distanceTo(group.current.position) * GIZMO_K);
    ring.current?.quaternion.copy(camera.quaternion);
  });
}

interface BoxHandlesProps {
  box: ThreeBox;
  /** 보여 줄 손잡이(move 가 맨 앞). */
  handles: readonly BoxEditHandle[];
  hovered: BoxEditHandle | null;
  dragging: BoxEditHandle | null;
  drag: BoxEditDrag;
  onHover: Dispatch<SetStateAction<BoxEditHandle | null>>;
}

/** 상자 가운데에 놓는 손잡이 묶음. 호버·끌기 상태는 부모가 쥐고 여기서는 모양만 정한다. */
export function BoxHandles({ box, handles, hovered, dragging, drag, onHover }: BoxHandlesProps) {
  const get = useThree((s) => s.get);
  const theme = useStore(useViewerStore(), (s) => s.theme);
  const [raycast] = useState(() => createHandleRaycast(get));
  const group = useRef<Group>(null);
  const ring = useRef<Mesh>(null);
  useConstantScreenSize(group, ring);

  const eventsOf = (handle: BoxEditHandle): HandleEvents => ({
    onPointerDown: (event) => {
      if (pickedHandle(event, handle) === handle) drag.start(handle, event);
    },
    onPointerMove: (event) => {
      if (pickedHandle(event, handle) === handle) onHover(handle);
    },
    onPointerOut: () => onHover((current) => (current === handle ? null : current)),
  });

  return (
    <group ref={group} name="box-edit-handles" position={box.position}>
      {handles.map((handle) => {
        const on = dragging ? dragging === handle : hovered === handle;
        const view = {
          theme,
          on,
          faded: dragging !== null && !on,
          raycast,
          events: eventsOf(handle),
        };
        return handle === 'move' ? (
          <MoveHandle key={handle} ringRef={ring} {...view} />
        ) : (
          <ArrowHandle key={handle} handle={handle} {...view} />
        );
      })}
    </group>
  );
}

/** 가운데 구와 링. 바닥(수평 평면)에서만 옮기는 손잡이다. */
function MoveHandle({
  ringRef,
  theme,
  on,
  faded,
  raycast,
  events,
}: HandleViewProps & { ringRef: RefObject<Mesh | null> }) {
  return (
    <group name="box-edit-handle:move" scale={on ? HOVER_SCALE : 1}>
      <mesh ref={ringRef} geometry={RING} raycast={noRaycast} renderOrder={MOVE_ORDER}>
        <HandleMaterial
          color={handleTint(theme, 'ring', on)}
          opacity={handleOpacity(RING_OPACITY, on, faded)}
        />
      </mesh>
      <mesh geometry={DOT} raycast={noRaycast} renderOrder={MOVE_ORDER}>
        <HandleMaterial
          color={handleTint(theme, 'move', on)}
          opacity={handleOpacity(DOT_OPACITY, on, faded)}
        />
      </mesh>
      <mesh
        name="box-edit-pick:move"
        geometry={PICK_DOT}
        userData={PICK_DATA.move}
        raycast={raycast}
        {...events}
      >
        <meshBasicMaterial visible={false} />
      </mesh>
    </group>
  );
}

/** 한 면만 움직이는 화살표. 몸통·화살촉과 보이지 않는 판정 형상으로 이뤄진다. */
function ArrowHandle({
  handle,
  theme,
  on,
  faded,
  raycast,
  events,
}: HandleViewProps & { handle: BoxEditArrow }) {
  const color = handleTint(theme, handle[0] as 'x' | 'y' | 'z', on);
  const opacity = handleOpacity(ARROW_OPACITY, on, faded);
  return (
    <group
      name={`box-edit-handle:${handle}`}
      quaternion={ARROW_QUATERNION[handle]}
      scale={on ? HOVER_SCALE : 1}
    >
      <mesh geometry={SHAFT} raycast={noRaycast} renderOrder={ARROW_ORDER}>
        <HandleMaterial color={color} opacity={opacity} />
      </mesh>
      <mesh geometry={HEAD} raycast={noRaycast} renderOrder={ARROW_ORDER}>
        <HandleMaterial color={color} opacity={opacity} />
      </mesh>
      <mesh
        name={`box-edit-pick:${handle}`}
        geometry={PICK_AXIS}
        position={[0, PICK_AXIS_OFFSET, 0]}
        userData={PICK_DATA[handle]}
        raycast={raycast}
        {...events}
      >
        <meshBasicMaterial visible={false} />
      </mesh>
    </group>
  );
}

/** 손잡이 재질. 깊이 판정을 끄고 양면을 그려 상자·벽에 가려지지 않는다. */
function HandleMaterial({ color, opacity }: { color: Color; opacity: number }) {
  return (
    <meshBasicMaterial
      color={color}
      transparent
      opacity={opacity}
      depthTest={false}
      depthWrite={false}
      side={DoubleSide}
    />
  );
}
