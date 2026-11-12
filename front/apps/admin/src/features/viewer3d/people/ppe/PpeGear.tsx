import { usePpeAssets, type GearAsset } from '@/features/viewer3d/people/ppe/ppeAssets';
import {
  CAPSULE_HELMET,
  CAPSULE_VEST,
  skeletonHelmetPlacement,
  skeletonVestPlacement,
  type GearPlacement,
} from '@/features/viewer3d/people/ppe/ppeFit';
import type { SkeletonAnchors, SkeletonFigure } from '@/features/viewer3d/people/skeletonPose';
import type { PersonHelmetState } from '@/features/viewer3d/types';
import { useFrame } from '@react-three/fiber';
import { useLayoutEffect, useMemo, useRef, type RefObject } from 'react';
import {
  DoubleSide,
  Matrix4,
  MeshStandardMaterial,
  Vector3,
  type Group,
  type Object3D,
} from 'three';

// 사람 한 명에게 안전모·조끼를 붙인다(명세 13.3). 착용(WORN)만 실물로 그린다. 모름·미착용·상태 없음은 그리지 않는다
// (모름을 반투명 장비로 그리면 착용과 구분되지 않았다, S15P31A101-656. 모름·미착용은 호스트가 이름표 글로 알린다).
// 모델은 클릭·초점에 끼지 않는다(raycast 없음). 골격 막대는 표본마다 관절에서 자리를 다시 구하고, 캡슐은 고정 자리다.

const noRaycast = () => null;

const GEAR_ROUGHNESS = 0.85;

interface SkeletonMotion {
  figure: SkeletonFigure | null;
}

type Target =
  | { kind: 'FIXED'; placement: GearPlacement }
  | {
      kind: 'SKELETON';
      fit: (anchors: SkeletonAnchors) => GearPlacement | null;
      motion: RefObject<SkeletonMotion>;
    };

const CAPSULE_TARGETS = {
  helmet: { kind: 'FIXED', placement: CAPSULE_HELMET },
  vest: { kind: 'FIXED', placement: CAPSULE_VEST },
} as const satisfies Record<string, Target>;

const skeletonTargets = (motion: RefObject<SkeletonMotion>) =>
  ({
    helmet: { kind: 'SKELETON', fit: skeletonHelmetPlacement, motion },
    vest: { kind: 'SKELETON', fit: skeletonVestPlacement, motion },
  }) as const satisfies Record<string, Target>;

// 재질은 장비·불투명도마다 하나를 사람끼리 함께 쓴다.
const materials = new WeakMap<GearAsset, Map<number, MeshStandardMaterial>>();

function solidMaterial(asset: GearAsset, opacity: number): MeshStandardMaterial {
  let byOpacity = materials.get(asset);
  if (!byOpacity) materials.set(asset, (byOpacity = new Map()));
  let material = byOpacity.get(opacity);
  if (!material) {
    material = new MeshStandardMaterial({
      map: asset.map,
      roughness: GEAR_ROUGHNESS,
      metalness: 0,
      side: DoubleSide,
      transparent: opacity < 1,
      opacity,
      depthWrite: opacity >= 1,
    });
    byOpacity.set(opacity, material);
  }
  return material;
}

const basis = new Matrix4();
const axisX = new Vector3();
const axisY = new Vector3();
const axisZ = new Vector3();

function applyPlacement(object: Object3D, placement: GearPlacement) {
  object.position.set(...placement.position);
  object.quaternion.setFromRotationMatrix(
    basis.makeBasis(
      axisX.set(...placement.xAxis),
      axisY.set(...placement.yAxis),
      axisZ.set(...placement.zAxis),
    ),
  );
  object.scale.set(...placement.size);
}

interface GearPieceProps {
  kind: 'helmet' | 'vest';
  asset: GearAsset;
  target: Target;
  opacity: number;
}

function GearPiece({ kind, asset, target, opacity }: GearPieceProps) {
  const group = useRef<Group>(null);
  const appliedFigure = useRef<SkeletonFigure | null | undefined>(undefined);

  useLayoutEffect(() => {
    if (target.kind === 'FIXED' && group.current) applyPlacement(group.current, target.placement);
  }, [target]);

  // 골격 막대는 표본(10 Hz)마다 새 관절로 자리를 다시 구한다. 자리를 구하지 못하면 그 표본에는 그리지 않는다.
  useFrame(() => {
    if (target.kind !== 'SKELETON' || !group.current) return;
    const figure = target.motion.current.figure;
    if (appliedFigure.current === figure) return;
    appliedFigure.current = figure;
    const placement = figure ? target.fit(figure.anchors) : null;
    group.current.visible = placement !== null;
    if (placement) applyPlacement(group.current, placement);
  });

  return (
    <group ref={group} name={`ppe-${kind}`} visible={target.kind === 'FIXED'}>
      <mesh
        geometry={asset.geometry}
        material={solidMaterial(asset, opacity)}
        raycast={noRaycast}
        dispose={null}
      />
    </group>
  );
}

interface PpeGearProps {
  /** 호스트가 따진 안전모·조끼 상태. WORN 만 그린다. */
  helmet: PersonHelmetState | null;
  vest: PersonHelmetState | null;
  /** 골격 막대 사람의 표본별 골격. 없으면 캡슐 자리(캡슐·아바타 메시 사람)다. */
  motion?: RefObject<SkeletonMotion>;
  /** 사람 몸의 불투명도. 장비도 같이 흐려진다. */
  opacity: number;
}

/**
 * 호스트가 넘긴 상태(personHelmets·personVests)대로 착용한 안전모·조끼만 붙인다. 모름·미착용·상태 없음은 그리지 않는다
 * (미착용은 PpeWarningMark 가 알린다). 같은 World ID·identity_revision·만료·연결은 호스트가 이미 따졌다.
 */
export function PpeGear({ helmet, vest, motion, opacity }: PpeGearProps) {
  const helmetWorn = helmet === 'WORN';
  const vestWorn = vest === 'WORN';
  // 착용한 장비가 있는 사람이 생길 때까지는 모델 파일을 받지 않는다.
  const assets = usePpeAssets(helmetWorn || vestWorn);
  const targets = useMemo(() => (motion ? skeletonTargets(motion) : CAPSULE_TARGETS), [motion]);
  if (!assets) return null;
  return (
    <>
      {helmetWorn && (
        <GearPiece kind="helmet" asset={assets.helmet} target={targets.helmet} opacity={opacity} />
      )}
      {vestWorn && (
        <GearPiece kind="vest" asset={assets.vest} target={targets.vest} opacity={opacity} />
      )}
    </>
  );
}
