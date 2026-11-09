import { PALETTE } from '@/features/viewer3d/palette';
import { useScenePalette } from '@/features/viewer3d/sceneTheme';
import { useEffect, useMemo } from 'react';
import { CanvasTexture, SRGBColorSpace } from 'three';

// 장면 안 이름표(스프라이트). DOM 이름표(drei Html)는 StrictMode 이중 마운트에서 첫 이름표가 비는 문제가 있었다.
const LABEL_PX = 48;
const FONT_PX = LABEL_PX * 0.62;
const MONO_FONT = `600 ${FONT_PX}px ui-monospace, Consolas, monospace`;
// 띠를 단 이름표(구역)는 흰 바탕에 굵은 고딕 글자이고, 왼쪽 띠가 심각도 색이다. 테마와 상관없이 같은 모양이다.
const SANS_FONT = `800 ${FONT_PX}px "Pretendard", "Noto Sans KR", "Malgun Gothic", system-ui, sans-serif`;
const TAG_BACKGROUND = '#ffffff';
const TAG_INK = PALETTE.light.label.observed;
const TAG_STRIPE_PX = 9;
const RADIUS_PX = 10;
const PADDING_PX = 12;

interface TextureStyle {
  ink: string;
  background: string;
  /** 왼쪽 띠 색. 있으면 띠를 단 이름표(고딕 글자)다. */
  accent: string | undefined;
}

function labelTexture(
  text: string,
  style: TextureStyle,
): { texture: CanvasTexture; aspect: number } {
  const { ink, background, accent } = style;
  const stripe = accent === undefined ? 0 : TAG_STRIPE_PX;
  const canvas = document.createElement('canvas');
  const context = canvas.getContext('2d');
  const font = accent === undefined ? MONO_FONT : SANS_FONT;
  if (context) context.font = font;
  const textWidth = context?.measureText(text).width ?? text.length * LABEL_PX * 0.4;
  const width = Math.ceil(textWidth + PADDING_PX * 2 + stripe);
  canvas.width = width;
  canvas.height = LABEL_PX;
  if (context) {
    const fillRound = (x: number, w: number, radii: number | number[], color: string) => {
      context.beginPath();
      context.roundRect(x, 0, w, LABEL_PX, radii);
      context.fillStyle = color;
      context.fill();
    };
    if (accent === undefined) {
      fillRound(0, width, RADIUS_PX, background);
    } else {
      // 띠 색으로 이름표 전체를 칠한 뒤, 띠 너비만 남기고 오른쪽을 바탕색으로 덮는다(왼쪽 모서리는 띠 색이 둥글게 남는다).
      fillRound(0, width, RADIUS_PX, accent);
      fillRound(stripe, width - stripe, [0, RADIUS_PX, RADIUS_PX, 0], background);
    }
    context.font = font;
    context.fillStyle = ink;
    context.textAlign = 'center';
    context.textBaseline = 'middle';
    context.fillText(text, stripe + (width - stripe) / 2, LABEL_PX / 2 + 1);
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
  /** 왼쪽 띠 색(CSS 색). 주면 흰 바탕·굵은 고딕 글자에 이 색 띠를 단 이름표가 된다. 구역 이름표가 쓴다. */
  accent?: string;
}

/**
 * 늘 카메라를 보는 이름표. 선택을 받지 않고 깊이와 상관없이 위에 그린다. 글자·색·테마가 바뀔 때만 텍스처를 새로 만든다.
 * 색을 주지 않으면 테마의 기본 글자색이고(띠를 단 이름표는 어두운 글자색), 띠 색(accent)을 주면 띠를 단 모양이 된다.
 */
export function LabelSprite({ text, y, height = 0.3, color, accent }: LabelSpriteProps) {
  const { label } = useScenePalette();
  const ink = color ?? (accent === undefined ? label.observed : TAG_INK);
  const background = accent === undefined ? label.background : TAG_BACKGROUND;
  const { texture, aspect } = useMemo(
    () => labelTexture(text, { ink, background, accent }),
    [text, ink, background, accent],
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
