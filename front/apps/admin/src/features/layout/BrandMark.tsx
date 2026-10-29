// 파비콘과 같은 simAI 로고(안전모를 감싼 AI 검출 틀).
export function BrandMark({ size = 32 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 64 64" aria-hidden="true">
      <g fill="none" className="stroke-primary" strokeWidth="5" strokeLinecap="round">
        <path d="M8.5 20V13A4.5 4.5 0 0 1 13 8.5H20" />
        <path d="M44 8.5H51A4.5 4.5 0 0 1 55.5 13V20" />
        <path d="M55.5 44V51A4.5 4.5 0 0 1 51 55.5H44" />
        <path d="M20 55.5H13A4.5 4.5 0 0 1 8.5 51V44" />
      </g>
      <g className="fill-brand-hat">
        <path d="M19 39.5C19 30 24.8 23.5 32 23.5S45 30 45 39.5Z" />
        <rect x="29" y="19.5" width="6" height="12" rx="3" />
        <rect x="15" y="39" width="34" height="5.5" rx="2.75" />
      </g>
      <path
        d="M28.4 25.5V36.5M35.6 25.5V36.5"
        className="stroke-card"
        strokeWidth="2.2"
        strokeLinecap="round"
      />
    </svg>
  );
}

/** 로고 옆에 쓰는 이름. 상단 바와 로그인 화면이 같은 모양을 쓴다. */
export function Wordmark() {
  return (
    <strong className="text-[21px] font-bold tracking-[-0.4px]">
      sim<span className="text-primary">AI</span>
    </strong>
  );
}
