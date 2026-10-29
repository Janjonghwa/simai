import { useSpatialData } from '@/features/viewer3d/data/SpatialDataProvider';
import { LabelSprite } from '@/features/viewer3d/LabelSprite';
import { useScenePalette } from '@/features/viewer3d/sceneTheme';
import { Selectable } from '@/features/viewer3d/Selectable';
import {
  obstacleCorners,
  obstacleObb,
  obstacleZRange,
  type ObstacleView,
} from '@/features/viewer3d/surfaces/obstacleGeometry';
import { useViewerStore } from '@/features/viewer3d/ViewerStoreContext';
import { worldToThree, type ThreePoint } from '@/features/viewer3d/worldCoordinates';
import { personOnRay } from '@/features/viewer3d/zones/zonePresentation';
import { Line } from '@react-three/drei';
import type { ThreeEvent } from '@react-three/fiber';
import { useMemo } from 'react';
import { useStore } from 'zustand';

// -274 등록 정적 객체(OBSTACLE) 레이어. Provider 묶음의 static_objects OBB 를 같은 Canvas·Scene 안에 object_id 마다 그린다.
// 반투명 상자는 계약 obb 그대로(3D 중심·전체 크기·yaw)이고 바닥에서 떠 있을 수 있다. 작업면과 달리 상판 가장자리 선이 없다.
// 모서리 12개는 World 꼭짓점을 worldToThree 로 그대로 옮겨 그린다(상자 회전과 별개로 꼭짓점 좌표가 정확하다).
// 보기용 glTF(Visual Geometry)는 쓰지 않는다. 충돌·위험 판정은 하지 않는다.

const noRaycast = () => null;
const pickPerson = (event: ThreeEvent<MouseEvent>) => personOnRay(event.intersections);
// WebGL 재질은 CSS 토큰을 바로 읽지 못해 장면 전용 값을 여기에만 둔다. 작업면(푸른 회색)과 구분되는 주황 계열이다.
/** 이름표를 윗면에서 띄우는 높이(m). */
const LABEL_LIFT = 0.22;

/** 이름표 글. 아랫면~윗면 높이와 운영 형상(등록 OBB)임을 적는다. */
export function obstacleLabel(obstacle: Pick<ObstacleView, 'objectId' | 'center' | 'size'>) {
  const [bottom, top] = obstacleZRange({ ...obstacle, yawRad: 0 });
  return `${obstacle.objectId} · 정적 객체 · Z ${bottom.toFixed(2)}~${top.toFixed(2)}m · 등록 OBB`;
}

/** 모서리 12개 선분(두 점씩): 아랫면 4변, 윗면 4변, 세로 4개. */
export function obstacleEdges(obstacle: ObstacleView): ThreePoint[] {
  const corners = obstacleCorners(obstacle).map(worldToThree);
  const edges: ThreePoint[] = [];
  for (let i = 0; i < 4; i += 1) {
    const next = (i + 1) % 4;
    edges.push(corners[i]!, corners[next]!);
    edges.push(corners[i + 4]!, corners[next + 4]!);
    edges.push(corners[i]!, corners[i + 4]!);
  }
  return edges;
}

export function ObstacleLayer() {
  const obstacles = useSpatialData((s) => s.workSurfaces.obstacles);
  const layer = useStore(useViewerStore(), (s) => s.layers.obstacles);
  if (layer === 'hidden') return null;
  return (
    <group name="obstacle-layer">
      {obstacles.map((obstacle) => (
        <ObstacleVolume
          key={obstacle.objectId}
          obstacle={obstacle}
          interactive={layer === 'interactive'}
        />
      ))}
    </group>
  );
}

function ObstacleVolume({
  obstacle,
  interactive,
}: {
  obstacle: ObstacleView;
  interactive: boolean;
}) {
  const selected = useStore(
    useViewerStore(),
    (s) => s.selected?.type === 'object' && s.selected.id === obstacle.objectId,
  );
  const obb = useMemo(() => obstacleObb(obstacle), [obstacle]);
  const edges = useMemo(() => obstacleEdges(obstacle), [obstacle]);
  const [w, d, h] = obb.size;
  const { obstacle: look } = useScenePalette();
  const color = selected ? look.selected : look.color;

  return (
    <group name={`obstacle-entry:${obstacle.objectId}`}>
      <Selectable
        target={{ type: 'object', id: obstacle.objectId }}
        pickInstead={pickPerson}
        interactive={interactive}
      >
        {/* Three Y 축 회전 = World +Z 축 회전(World→Three 가 +Z→+Y 라 같은 방향이다). */}
        <group position={worldToThree(obb.center)} rotation-y={obb.yaw}>
          <mesh name={`obstacle-box:${obstacle.objectId}`}>
            <boxGeometry args={[w, h, d]} />
            <meshBasicMaterial
              color={color}
              transparent
              opacity={selected ? 0.32 : 0.2}
              depthWrite={false}
            />
          </mesh>
        </group>
      </Selectable>
      <Line
        name={`obstacle-edges:${obstacle.objectId}`}
        points={edges}
        segments
        color={color}
        lineWidth={selected ? 2 : 1.2}
        transparent
        opacity={0.8}
        raycast={noRaycast}
      />
      <group position={worldToThree(obb.center)}>
        <LabelSprite text={obstacleLabel(obstacle)} y={h / 2 + LABEL_LIFT} height={0.2} />
      </group>
    </group>
  );
}
