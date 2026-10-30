import { useSpatialData } from '@/features/viewer3d/data/SpatialDataProvider';
import type { ZoneView } from '@/features/viewer3d/data/spatialFrame';
import { LabelSprite } from '@/features/viewer3d/LabelSprite';
import { ZoneRiskMark } from '@/features/viewer3d/risk/RiskMarks';
import { riskLabel, riskStyle } from '@/features/viewer3d/risk/riskPresentation';
import { useRiskHighlight } from '@/features/viewer3d/risk/RiskScope';
import { useIsHovered, useScenePalette } from '@/features/viewer3d/sceneTheme';
import { Selectable } from '@/features/viewer3d/Selectable';
import { useViewerStore } from '@/features/viewer3d/ViewerStoreContext';
import { worldPoint, worldToThree } from '@/features/viewer3d/worldCoordinates';
import {
  aerialGuides,
  boxEdges,
  FLOOR_GUIDE_LIFT,
  isAerialZone,
  zoneBox,
} from '@/features/viewer3d/zones/zoneGeometry';
import {
  footprintMesh,
  labelVertex,
  prismEdges,
  prismMesh,
  ringEdges,
  type PolygonMesh,
} from '@/features/viewer3d/zones/zonePolygon';
import {
  personOnRay,
  ZoneApplyContext,
  zoneLabel,
  zoneStateLabel,
  zoneStyle,
} from '@/features/viewer3d/zones/zonePresentation';
import { Line } from '@react-three/drei';
import type { ThreeEvent } from '@react-three/fiber';
import { useContext, useEffect, useMemo } from 'react';
import { BufferAttribute, BufferGeometry, DoubleSide } from 'three';
import { useStore } from 'zustand';

// -47 구역 레이어. -194 Provider 의 구역(ZoneSnapshot)을 같은 Canvas·Scene 안에 zone_id 마다 하나씩 그린다.
// 구역 목록이 바뀔 때만 다시 그리고 10 Hz World 갱신에는 반응하지 않는다. 구역이 없거나 거부됐으면 아무것도 그리지 않는다.
// BE 정식 구역(-234)의 비직사각형은 다각형 기둥(PRISM), 바닥 판정 구역은 바닥면(FOOTPRINT)으로 그린다(둘레 상자로 넓히지 않는다).
// 레이어 값(-234): hidden 이면 그리지 않고, visible 이면 그리되 클릭을 받지 않는다.

const noRaycast = () => null;
/** 구역 이름표를 상자 하단에서 띄우는 높이(m). 바닥 격자·안내선과 겹치지 않게 조금만 띄운다. */
const ZONE_LABEL_LIFT = 0.18;
const pickPerson = (event: ThreeEvent<MouseEvent>) => personOnRay(event.intersections);

export function ZoneLayer() {
  const zones = useSpatialData((s) => s.zones.zones);
  const layer = useStore(useViewerStore(), (s) => s.layers.zones);
  if (layer === 'hidden') return null;
  const interactive = layer === 'interactive';
  return (
    <group name="zone-layer">
      {zones.map((zone) =>
        zone.shape === 'PRISM' || zone.shape === 'FOOTPRINT' ? (
          <ZonePolygon key={zone.zoneId} zone={zone} interactive={interactive} />
        ) : (
          <ZoneVolume key={zone.zoneId} zone={zone} interactive={interactive} />
        ),
      )}
    </group>
  );
}

interface ZoneEntryProps {
  zone: ZoneView;
  interactive: boolean;
}

function ZoneVolume({ zone, interactive }: ZoneEntryProps) {
  const applyState = useContext(ZoneApplyContext);
  const selected = useStore(
    useViewerStore(),
    (s) => s.selected?.type === 'zone' && s.selected.id === zone.zoneId,
  );
  const box = zoneBox(zone);
  const palette = useScenePalette();
  const hovered = useIsHovered(`zone:${zone.zoneId}`);
  // 위험 강조는 형상·적용 상태 표시와 따로 얹는다. 구역 상자·모서리·이름표는 바꾸지 않는다.
  const risk = useRiskHighlight('zone', zone.zoneId);
  const [sx, sy, sz] = box?.size ?? [0, 0, 0];
  const [, py] = box?.position ?? [0, 0, 0];
  // 같은 크기면 선 형상을 다시 만들지 않는다.
  const edges = useMemo(() => boxEdges([sx, sy, sz]), [sx, sy, sz]);
  const guides = useMemo(() => aerialGuides([sx, sy, sz], py), [sx, sy, sz, py]);
  if (!box) return null;
  const style = zoneStyle(applyState, selected, palette);

  return (
    <group name={`zone-entry:${zone.zoneId}`}>
      {/* 초점은 이 그룹(상자와 모서리)의 중심이다. 안내선·이름표는 바깥에 둬 중심을 옮기지 않는다. */}
      <Selectable
        target={{ type: 'zone', id: zone.zoneId }}
        pickInstead={pickPerson}
        interactive={interactive}
      >
        <group position={box.position}>
          <mesh name={`zone-box:${zone.zoneId}`}>
            <boxGeometry args={box.size} />
            <meshBasicMaterial
              color={style.color}
              transparent
              opacity={style.fillOpacity}
              depthWrite={false}
            />
          </mesh>
          <Line
            points={edges}
            segments
            color={style.color}
            lineWidth={selected ? 2.5 : 1.5}
            dashed={style.dashed}
            dashSize={style.dashSize}
            gapSize={style.gapSize}
            transparent
            opacity={style.edgeOpacity}
            raycast={noRaycast}
          />
        </group>
      </Selectable>
      <group position={box.position}>
        {isAerialZone(zone) && (
          <Line
            points={guides}
            segments
            color={style.color}
            lineWidth={1}
            dashed
            dashSize={0.1}
            gapSize={0.1}
            transparent
            opacity={0.45}
            raycast={noRaycast}
          />
        )}
        {/* 이름표는 상자 앞쪽(World min X·min Y, 기본 시점 쪽) 모서리의 하단 높이에 둔다.
            사람 이름표(머리 위)와 높이대가 달라 구역 안·가장자리의 사람과 겹치지 않는다.
            위험이 있으면 위험 > 구역 이름·높이·적용 상태 순서로 한 줄에 적는다. */}
        {/* 이름표는 잠깐(HOVER_LABEL_MS) 머물 때·선택·위험일 때만 보인다(관제 데모와 같다). */}
        <group position={[-sx / 2, -sy / 2, sz / 2]}>
          <LabelSprite
            text={
              risk
                ? `${riskLabel(risk)} · ${zoneLabel(zone, applyState)}`
                : zoneLabel(zone, applyState)
            }
            y={ZONE_LABEL_LIFT}
            height={0.22}
            color={risk ? riskStyle(risk, palette).labelColor : undefined}
            visible={hovered || selected || risk !== null}
          />
        </group>
        {risk && <ZoneRiskMark risk={risk} zoneId={zone.zoneId} size={[sx, sy, sz]} />}
      </group>
    </group>
  );
}

/** 버퍼 형상. 형상이 바뀌거나 내려가면 해제한다. */
function usePolygonGeometry(mesh: PolygonMesh) {
  const geometry = useMemo(() => {
    const g = new BufferGeometry();
    g.setAttribute('position', new BufferAttribute(mesh.positions, 3));
    g.setIndex(mesh.indices);
    g.computeBoundingSphere();
    return g;
  }, [mesh]);
  useEffect(() => () => geometry.dispose(), [geometry]);
  return geometry;
}

/** 다각형 구역 이름표 문구. 바닥 판정 구역은 높이가 판정에 쓰이지 않음을 적는다. */
export function polygonZoneLabel(zone: ZoneView, applyState: Parameters<typeof zoneLabel>[1]) {
  if (zone.shape === 'FOOTPRINT') {
    return [zone.zoneId, '바닥 판정(높이 무관)', zoneStateLabel(applyState)].join(' · ');
  }
  return zoneLabel(zone, applyState);
}

/** BE 정식 비직사각형(PRISM)·바닥 판정(FOOTPRINT) 구역. 꼭짓점은 official.polygonXy 그대로다. */
function ZonePolygon({ zone, interactive }: ZoneEntryProps) {
  const applyState = useContext(ZoneApplyContext);
  const selected = useStore(
    useViewerStore(),
    (s) => s.selected?.type === 'zone' && s.selected.id === zone.zoneId,
  );
  const risk = useRiskHighlight('zone', zone.zoneId);
  const points = zone.official!.polygonXy;
  const footprint = zone.shape === 'FOOTPRINT';
  const [zMin, zMax] = [zone.min[2], zone.max[2]];
  const mesh = useMemo(
    () => (footprint ? footprintMesh(points) : prismMesh(points, zMin, zMax)),
    [footprint, points, zMin, zMax],
  );
  const geometry = usePolygonGeometry(mesh);
  const edges = useMemo(
    () => (footprint ? ringEdges(points, FLOOR_GUIDE_LIFT) : prismEdges(points, zMin, zMax)),
    [footprint, points, zMin, zMax],
  );
  const style = zoneStyle(applyState, selected);
  const [lx, ly] = labelVertex(points);
  const labelAt = worldToThree(worldPoint(lx, ly, footprint ? 0 : zMin));
  const label = polygonZoneLabel(zone, applyState);

  return (
    <group name={`zone-entry:${zone.zoneId}`}>
      <Selectable
        target={{ type: 'zone', id: zone.zoneId }}
        pickInstead={pickPerson}
        interactive={interactive}
      >
        <mesh name={`zone-polygon:${zone.zoneId}`} geometry={geometry}>
          <meshBasicMaterial
            color={style.color}
            transparent
            opacity={footprint ? style.fillOpacity + 0.06 : style.fillOpacity}
            depthWrite={false}
            side={DoubleSide}
          />
        </mesh>
      </Selectable>
      <Line
        name={`zone-polygon-edges:${zone.zoneId}`}
        points={edges}
        segments
        color={risk ? riskStyle(risk).color : style.color}
        lineWidth={risk ? (riskStyle(risk).double ? 3.5 : 2.5) : selected ? 2.5 : 1.5}
        dashed={risk ? riskStyle(risk).dashed : style.dashed}
        dashSize={style.dashed ? style.dashSize : 0.2}
        gapSize={style.dashed ? style.gapSize : 0.12}
        transparent
        opacity={style.edgeOpacity}
        raycast={noRaycast}
      />
      <group position={labelAt}>
        <LabelSprite
          text={risk ? `${riskLabel(risk)} · ${label}` : label}
          y={ZONE_LABEL_LIFT}
          height={0.22}
          color={risk ? riskStyle(risk).labelColor : undefined}
        />
      </group>
    </group>
  );
}
