import { useScenePalette } from '@/features/viewer3d/sceneTheme';
import { useEffect, useMemo } from 'react';
import { CanvasTexture, SRGBColorSpace } from 'three';

// 장면 안 이름표(스프라이트). DOM 이름표(drei Html)는 StrictMode 이중 마운트에서 첫 이름표가 비는 문제가 있었다.
const LABEL_PX = 48;

function labelTexture(
  text: string,
  color: string,
  background: string,
): { texture: CanvasTexture; aspect: number } {
  const canvas = document.createElement('canvas');
  const context = canvas.getContext('2d');
  const font = `600 ${LABEL_PX * 0.62}px ui-monospace, Consolas, monospace`;
  if (context) context.font = font;
  const width = Math.ceil((context?.measureText(text).width ?? text.length * LABEL_PX * 0.4) + 24);
  canvas.width = width;
  canvas.height = LABEL_PX;
  if (context) {
    context.fillStyle = background;
    context.beginPath();
    context.roundRect(0, 0, width, LABEL_PX, 10);
    context.fill();
    context.font = font;
    context.fillStyle = color;
    context.textAlign = 'center';
    context.textBaseline = 'middle';
    context.fillText(text, width / 2, LABEL_PX / 2 + 1);
  }
  const texture = new CanvasTexture(canvas);
  texture.colorSpace = SRGBColorSpace;
  return { texture, aspect: width / LABEL_PX };
}

const noRaycast = () => null;

interface LabelSpriteProps {
  text: string;
  /** 위로 띄울 높이(m, 부모 기준). */
  y: number;
  /** 글자 높이(m). */
  height?: number;
  color?: string;
}

/**
 * 늘 카메라를 보는 이름표. 선택을 받지 않고 깊이와 상관없이 위에 그린다. 글자·색·테마가 바뀔 때만 텍스처를 새로 만든다.
 * 색을 주지 않으면 테마의 기본 글자색이다.
 */
export function LabelSprite({ text, y, height = 0.3, color }: LabelSpriteProps) {
  const { label } = useScenePalette();
  const ink = color ?? label.observed;
  const { texture, aspect } = useMemo(
    () => labelTexture(text, ink, label.background),
    [text, ink, label.background],
  );
  useEffect(() => () => texture.dispose(), [texture]);
  return (
    <sprite
      position-y={y}
      scale={[height * aspect, height, 1]}
      renderOrder={10}
      raycast={noRaycast}
    >
      <spriteMaterial map={texture} transparent depthTest={false} />
    </sprite>
  );
}
