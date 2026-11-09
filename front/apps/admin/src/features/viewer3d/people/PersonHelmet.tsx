import { SKELETON_RADIUS, type SkeletonFigure } from '@/features/viewer3d/people/skeletonPose';
import type { PersonHelmetState } from '@/features/viewer3d/types';
import { useFrame } from '@react-three/fiber';
import { useRef, type RefObject } from 'react';
import type { Group } from 'three';

const noRaycast = () => null;

/** 안전모 색(실물 표시). 장면 색표와 따로 두는 보호구 색이다. */
const HELMET_COLOR = '#f2c230';
const WARNING_COLOR = '#e5484d';
/** 머리 구 위로 띄우는 거리(m). 머리 구 반지름보다 조금 위다. */
const ABOVE_HEAD = 0.07;
/** UNKNOWN 반투명 정도. */
const UNKNOWN_OPACITY = 0.35;

/**
 * 사람 머리 위 단순 안전모 형상(명세 13.3, S15P31A101-531). 호스트가 같은 World ID·identity_revision·만료·연결을 따진 상태만 받는다.
 * - WORN: 반구 + 챙(실물 표시). UNKNOWN: 같은 형상을 반투명 + 외곽선. NOT_WORN: 형상 없음 + 머리 위 경고 표식.
 * - 위치는 Tier 0 골격 머리(얼굴 관절 가운데)를 따른다. 골격 머리가 없으면 fallbackY(캡슐 머리·높이 모름 표식 위쪽)에 둔다.
 *   아바타 메시 머리에 정밀하게 맞추지 않는다(19.1 미결). 경보가 아니며 클릭을 받지 않는다.
 */
export function PersonHelmet({
  state,
  figure,
  fallbackY,
}: {
  state: PersonHelmetState;
  /** 이번 표본의 Tier 0 골격 모양(useFrame 마다 읽는다). */
  figure: RefObject<{ figure: SkeletonFigure | null }>;
  fallbackY: number;
}) {
  const group = useRef<Group>(null);

  useFrame(() => {
    const g = group.current;
    if (!g) return;
    const head = figure.current.figure?.balls.find((b) => b.radius === SKELETON_RADIUS.head);
    if (head) g.position.set(head.at[0], head.at[1] + ABOVE_HEAD, head.at[2]);
    else g.position.set(0, fallbackY, 0);
  });

  return (
    <group ref={group} name={`person-helmet:${state}`} position-y={fallbackY}>
      {state === 'NOT_WORN' ? (
        <mesh position-y={0.22} raycast={noRaycast}>
          <octahedronGeometry args={[0.07]} />
          <meshBasicMaterial color={WARNING_COLOR} />
        </mesh>
      ) : (
        <HelmetShape outline={state === 'UNKNOWN'} />
      )}
    </group>
  );
}

function HelmetShape({ outline }: { outline: boolean }) {
  const opacity = outline ? UNKNOWN_OPACITY : 1;
  return (
    <>
      {/* 머리를 덮는 반구 */}
      <mesh raycast={noRaycast}>
        <sphereGeometry args={[0.15, 18, 10, 0, Math.PI * 2, 0, Math.PI / 2]} />
        <meshStandardMaterial
          color={HELMET_COLOR}
          transparent={outline}
          opacity={opacity}
          depthWrite={!outline}
          roughness={0.5}
        />
      </mesh>
      {/* 챙 */}
      <mesh raycast={noRaycast}>
        <cylinderGeometry args={[0.19, 0.19, 0.015, 24]} />
        <meshStandardMaterial
          color={HELMET_COLOR}
          transparent={outline}
          opacity={opacity}
          depthWrite={!outline}
          roughness={0.5}
        />
      </mesh>
      {outline && (
        <mesh raycast={noRaycast}>
          <sphereGeometry args={[0.152, 12, 6, 0, Math.PI * 2, 0, Math.PI / 2]} />
          <meshBasicMaterial color={HELMET_COLOR} wireframe />
        </mesh>
      )}
    </>
  );
}
