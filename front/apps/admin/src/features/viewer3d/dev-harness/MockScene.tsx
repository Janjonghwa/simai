import { CAMERAS, OBJECTS, PEOPLE, ZONES } from '@/features/viewer3d/dev-harness/mockUi';
import { Selectable } from '@/features/viewer3d/Selectable';
import type { ViewerTarget } from '@/features/viewer3d/types';
import { Edges } from '@react-three/drei';
import { useThree } from '@react-three/fiber';
import { useEffect, useRef } from 'react';
import type { Vector3 } from 'three';

export interface SceneColors {
  floor: string;
  person: string;
  personSelected: string;
  critical: string;
  caution: string;
  object: string;
  camera: string;
}

interface MockSceneProps {
  colors: SceneColors;
  selected: ViewerTarget | null;
}

const isSelected = (selected: ViewerTarget | null, type: ViewerTarget['type'], id: string) =>
  selected?.type === type && selected.id === id;

// 구역 부피는 클릭을 받지 않게 해 안에 있는 사람을 가리지 않는다. 구역 선택은 바닥 면으로 한다.
const noRaycast = () => null;

interface OrbitLike {
  target: Vector3;
  update: () => void;
}

/**
 * 목업에서만 처음 한 번 교실 전체가 패널 사이에 보이도록 시점을 잡는다.
 * Viewer 기본 시점은 바꾸지 않는다(World 장면 기준은 S15P31A101-38).
 */
function InitialView() {
  const camera = useThree((s) => s.camera);
  const controls = useThree((s) => s.controls) as unknown as OrbitLike | null;
  const applied = useRef(false);
  useEffect(() => {
    if (!controls || applied.current) return;
    applied.current = true;
    camera.position.set(0, 12.5, 12);
    controls.target.set(0, 0, 0.8);
    controls.update();
  }, [camera, controls]);
  return null;
}

/** 목업용 교실 장면. 기존 Viewer 의 Canvas·카메라·선택 위에 Selectable 로만 얹는다. */
export function MockScene({ colors, selected }: MockSceneProps) {
  return (
    <>
      <InitialView />
      <mesh rotation-x={-Math.PI / 2} position-y={-0.005}>
        <planeGeometry args={[10, 10]} />
        <meshStandardMaterial color={colors.floor} transparent opacity={0.55} />
      </mesh>
      <mesh position-y={0.55} raycast={noRaycast}>
        <boxGeometry args={[10, 1.1, 10]} />
        <meshBasicMaterial transparent opacity={0} depthWrite={false} />
        <Edges color={colors.camera} />
      </mesh>

      {CAMERAS.map((camera) => (
        <mesh key={camera.id} position={[camera.x, 2.6, camera.z]} raycast={noRaycast}>
          <boxGeometry args={[0.25, 0.18, 0.35]} />
          <meshStandardMaterial color={colors.camera} />
        </mesh>
      ))}

      {ZONES.map((zone) => {
        const on = isSelected(selected, 'zone', zone.id);
        const color = zone.tone === 'critical' ? colors.critical : colors.caution;
        return (
          <Selectable key={zone.id} target={{ type: 'zone', id: zone.id }}>
            <mesh rotation-x={-Math.PI / 2} position={[zone.x, 0.01, zone.z]}>
              <planeGeometry args={[zone.width, zone.depth]} />
              <meshBasicMaterial
                color={color}
                transparent
                opacity={on ? 0.45 : 0.22}
                depthWrite={false}
              />
            </mesh>
            <mesh position={[zone.x, zone.height / 2, zone.z]} raycast={noRaycast}>
              <boxGeometry args={[zone.width, zone.height, zone.depth]} />
              <meshBasicMaterial
                color={color}
                transparent
                opacity={on ? 0.16 : 0.07}
                depthWrite={false}
              />
              <Edges color={color} />
            </mesh>
          </Selectable>
        );
      })}

      {OBJECTS.map((object) => (
        <Selectable key={object.id} target={{ type: 'object', id: object.id }}>
          <mesh position={[object.x, object.height / 2, object.z]}>
            <boxGeometry args={[object.width, object.height, object.depth]} />
            <meshStandardMaterial
              color={
                isSelected(selected, 'object', object.id) ? colors.personSelected : colors.object
              }
            />
          </mesh>
        </Selectable>
      ))}

      {PEOPLE.map((person) => (
        <Selectable key={person.id} target={{ type: 'person', id: person.id }}>
          <mesh position={[person.x, 0.85, person.z]}>
            <capsuleGeometry args={[0.24, 1.1, 6, 12]} />
            <meshStandardMaterial
              color={
                isSelected(selected, 'person', person.id) ? colors.personSelected : colors.person
              }
            />
          </mesh>
        </Selectable>
      ))}
    </>
  );
}
