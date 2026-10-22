import { useSpatialData } from '@/features/viewer3d/data/SpatialDataProvider';
import type { ZoneView } from '@/features/viewer3d/data/spatialFrame';
import { LabelSprite } from '@/features/viewer3d/LabelSprite';
import { ZoneRiskMark } from '@/features/viewer3d/risk/RiskMarks';
import { riskLabel, riskStyle } from '@/features/viewer3d/risk/riskPresentation';
import { useRiskHighlight } from '@/features/viewer3d/risk/RiskScope';
import { Selectable } from '@/features/viewer3d/Selectable';
import { useViewerStore } from '@/features/viewer3d/ViewerStoreContext';
import {
  aerialGuides,
  boxEdges,
  isAerialZone,
  zoneBox,
} from '@/features/viewer3d/zones/zoneGeometry';
import {
  personOnRay,
  ZoneApplyContext,
  zoneLabel,
  zoneStyle,
} from '@/features/viewer3d/zones/zonePresentation';
import { Line } from '@react-three/drei';
import type { ThreeEvent } from '@react-three/fiber';
import { useContext, useMemo } from 'react';
import { useStore } from 'zustand';

// -47 구역 레이어. -194 Provider 의 구역(ZoneSnapshot)을 같은 Canvas·Scene 안에 zone_id 마다 하나씩 그린다.
// 구역 목록이 바뀔 때만 다시 그리고 10 Hz World 갱신에는 반응하지 않는다. 구역이 없거나 거부됐으면 아무것도 그리지 않는다.

const noRaycast = () => null;
/** 구역 이름표를 상자 하단에서 띄우는 높이(m). 바닥 격자·안내선과 겹치지 않게 조금만 띄운다. */
const ZONE_LABEL_LIFT = 0.18;
const pickPerson = (event: ThreeEvent<MouseEvent>) => personOnRay(event.intersections);

export function ZoneLayer() {
  const zones = useSpatialData((s) => s.zones.zones);
  return (
    <group name="zone-layer">
      {zones.map((zone) => (
        <ZoneVolume key={zone.zoneId} zone={zone} />
      ))}
    </group>
  );
}

function ZoneVolume({ zone }: { zone: ZoneView }) {
  const applyState = useContext(ZoneApplyContext);
  const selected = useStore(
    useViewerStore(),
    (s) => s.selected?.type === 'zone' && s.selected.id === zone.zoneId,
  );
  const box = zoneBox(zone);
  // 위험 강조는 형상·적용 상태 표시와 따로 얹는다. 구역 상자·모서리·이름표는 바꾸지 않는다.
  const risk = useRiskHighlight('zone', zone.zoneId);
  const [sx, sy, sz] = box?.size ?? [0, 0, 0];
  const [, py] = box?.position ?? [0, 0, 0];
  // 같은 크기면 선 형상을 다시 만들지 않는다.
  const edges = useMemo(() => boxEdges([sx, sy, sz]), [sx, sy, sz]);
  const guides = useMemo(() => aerialGuides([sx, sy, sz], py), [sx, sy, sz, py]);
  if (!box) return null;
  const style = zoneStyle(applyState, selected);

  return (
    <group name={`zone-entry:${zone.zoneId}`}>
      {/* 초점은 이 그룹(상자와 모서리)의 중심이다. 안내선·이름표는 바깥에 둬 중심을 옮기지 않는다. */}
      <Selectable target={{ type: 'zone', id: zone.zoneId }} pickInstead={pickPerson}>
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
        <group position={[-sx / 2, -sy / 2, sz / 2]}>
          <LabelSprite
            text={
              risk
                ? `${riskLabel(risk)} · ${zoneLabel(zone, applyState)}`
                : zoneLabel(zone, applyState)
            }
            y={ZONE_LABEL_LIFT}
            height={0.22}
            color={risk ? riskStyle(risk).labelColor : undefined}
          />
        </group>
        {risk && <ZoneRiskMark risk={risk} zoneId={zone.zoneId} size={[sx, sy, sz]} />}
      </group>
    </group>
  );
}
