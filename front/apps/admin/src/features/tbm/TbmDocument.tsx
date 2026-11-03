import {
  A4_H,
  A4_PAD,
  A4_W,
  PREVIEW_FRAME_PX,
  PREVIEW_SCALE_INITIAL,
} from '@/features/tbm/constants';
import { paginateSheet, previewScale } from '@/features/tbm/paginate';
import type { TbmForm } from '@/features/tbm/tbmForm';
import { TbmFormSheet } from '@/features/tbm/TbmFormSheet';
import type { Signature } from '@/features/tbm/types';
import { Button } from '@simai/ui/components/button';
import { cn } from '@simai/ui/lib/utils';
import { useDeferredValue, useLayoutEffect, useRef, useState, type RefObject } from 'react';
import { createPortal } from 'react-dom';

// TBM 회의록 미리보기와 출력. 서식(TbmFormSheet)을 눈에 안 보이게 그려 A4 쪽으로 나눈 뒤(paginate.ts),
// 화면에는 한 장씩(쪽이 둘 이상이면 ◀ n / N ▶) 축소해 보이고, 출력할 때는 같은 쪽을 A4 로 찍는다.

/** 흰 종이. 다크 모드에서도 흰 바탕에 검은 글씨로 보이고, 쪽 안의 표는 서식이 준 클래스로 그려진다. */
const PAPER = 'paper box-border bg-white text-[12.5px] leading-normal text-black';
const NAV_BUTTON = 'h-[30px] min-w-9 bg-surface-2 px-3';

/** 출력용 사본의 작업 하나: 번호 · 받은 내용에 고친 칸을 덮은 양식 · 참석 인원. */
export interface PrintItem {
  id: string;
  form: TbmForm;
  people: number;
  signatures?: readonly Signature[];
  approvedAt?: number | string | null;
  approvedByName?: string | null;
  reportVersion?: number;
}

/** 서식을 쪽으로 나눈 결과. measure 는 나누는 데 쓰는 보이지 않는 사본이라 화면 어딘가에 그려 둬야 한다. */
function usePages(form: TbmForm, people: number) {
  // 글자를 칠 때마다 쪽을 다시 나누지 않도록, 입력이 먼저 그려진 뒤 나눈다.
  const settled = useDeferredValue(form);
  const measuring = useRef<HTMLDivElement>(null);
  const [pages, setPages] = useState<string[]>([]);
  const [error, setError] = useState(false);

  useLayoutEffect(() => {
    const sheet = measuring.current?.firstElementChild;
    if (!(sheet instanceof HTMLElement)) return undefined;
    const update = () => {
      try {
        const next = paginateSheet(sheet);
        setPages((prev) =>
          prev.length === next.length && prev.every((html, n) => html === next[n]) ? prev : next,
        );
        setError(false);
      } catch {
        setError(true);
      }
    };
    update();
    // 글꼴을 받는 중이면 다 받은 뒤 다시 잰다.
    const fonts = document.fonts;
    fonts?.addEventListener?.('loadingdone', update);
    return () => fonts?.removeEventListener?.('loadingdone', update);
  }, [settled, people]);

  const measure = (
    <div ref={measuring} hidden aria-hidden inert>
      <TbmFormSheet form={settled} editable={false} people={people} />
    </div>
  );
  return { measure, pages, error };
}

/** 보이는 영역(viewport)에 A4 한 장이 들어가는 축소율. 영역 크기가 바뀌면 다시 맞춘다. */
function useFitScale(viewport: RefObject<HTMLElement | null>) {
  const [scale, setScale] = useState(PREVIEW_SCALE_INITIAL);

  useLayoutEffect(() => {
    const el = viewport.current;
    if (!el) return undefined;
    const fit = () =>
      setScale(previewScale(el.clientWidth - PREVIEW_FRAME_PX, el.clientHeight - PREVIEW_FRAME_PX));
    fit();
    if (typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(fit);
    observer.observe(el);
    return () => observer.disconnect();
  }, [viewport]);

  return scale;
}

/** 쪽이 둘 이상일 때의 ◀ n / N ▶. */
function PageNav({
  shown,
  count,
  onChange,
}: {
  shown: number;
  count: number;
  onChange: (page: number) => void;
}) {
  return (
    <div className="flex flex-none items-center justify-center gap-4 text-[13px] text-muted-foreground">
      <Button
        variant="secondary"
        size="sm"
        aria-label="이전 장"
        disabled={shown === 0}
        onClick={() => onChange(shown - 1)}
        className={NAV_BUTTON}
      >
        ◀
      </Button>
      <span aria-live="polite" className="tabular-nums">
        {shown + 1} / {count}
      </span>
      <Button
        variant="secondary"
        size="sm"
        aria-label="다음 장"
        disabled={shown >= count - 1}
        onClick={() => onChange(shown + 1)}
        className={NAV_BUTTON}
      >
        ▶
      </Button>
    </div>
  );
}

/** 회의록 미리보기: A4 한 장을 보이는 영역에 맞춰 줄여 보인다. */
export function TbmPreview({
  form,
  people,
  receiving,
}: {
  form: TbmForm;
  people: number;
  /** 받는 중에는 빈 양식을 흐리게 보인다. */
  receiving: boolean;
}) {
  const { measure, pages, error } = usePages(form, people);
  const viewport = useRef<HTMLDivElement>(null);
  const scale = useFitScale(viewport);
  const [page, setPage] = useState(0);
  const shown = Math.min(page, Math.max(0, pages.length - 1));

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-2">
      {measure}
      {pages.length > 1 && <PageNav shown={shown} count={pages.length} onChange={setPage} />}
      {error && (
        <p role="alert" className="text-[13px] text-muted-foreground">
          회의록 쪽을 나누지 못했습니다
        </p>
      )}
      <div ref={viewport} className="flex min-h-0 flex-1 justify-center [contain:size]">
        {pages[shown] !== undefined && (
          <div
            role="group"
            aria-label="TBM 회의록 미리보기"
            className="h-fit rounded-xl bg-surface-2 p-3"
          >
            <div className="relative" style={{ width: A4_W * scale, height: A4_H * scale }}>
              <div
                className={cn(
                  PAPER,
                  'absolute top-0 left-0 origin-top-left overflow-hidden shadow-float',
                  receiving && '[&_td]:text-faint',
                )}
                style={{
                  width: A4_W,
                  height: A4_H,
                  padding: A4_PAD,
                  transform: `scale(${scale})`,
                }}
                dangerouslySetInnerHTML={{ __html: pages[shown] }}
              />
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function PrintPages({ form, people }: { form: TbmForm; people: number }) {
  const { measure, pages } = usePages(form, people);
  return (
    <>
      {measure}
      {pages.map((html, n) => (
        <div
          key={n}
          className={cn(PAPER, 'h-[296mm] w-[210mm] break-before-page overflow-hidden')}
          style={{ padding: A4_PAD }}
          dangerouslySetInnerHTML={{ __html: html }}
        />
      ))}
    </>
  );
}

/**
 * 출력용 사본. 인쇄 창을 열 때만 그린다(앱의 다른 화면을 출력할 때 쪽 여백을 바꾸지 않게).
 * 작업마다 쪽을 나눠 이어 찍고, 출력할 때는 이 사본만 보인다.
 */
export function TbmPrint({ items }: { items: readonly PrintItem[] }) {
  return createPortal(
    <div className="hidden print:block print:[body:has(>&)]:bg-white print:[body:has(>&)>*:not(&)]:hidden!">
      <style>{'@page { size: A4; margin: 0 }'}</style>
      {items.map((item) => (
        <div key={item.id}>
          <PrintPages form={item.form} people={item.people} />
          <div className="break-before-page bg-white p-8 text-black">
            <p>보고서 버전: {item.reportVersion != null ? `v${item.reportVersion}` : '—'}</p>
            <h2>참석자 서명</h2>
            <p>공인 전자서명 아님, 내부 기록용 전자 서명</p>
            {item.signatures
              ?.filter((signature) => signature.signer_role === 'PARTICIPANT')
              .map((signature) => (
                <div key={signature.signature_id}>
                  {signature.signer_name} · {new Date(signature.signed_at).toLocaleString()}{' '}
                  <img src={signature.image_url} alt={`${signature.signer_name} 서명`} />
                </div>
              ))}
            <h2>관리자 결재</h2>
            <p>
              {item.approvedAt != null
                ? `결재 완료 · ${item.approvedByName ?? '관리자'} · ${new Date(item.approvedAt).toLocaleString()}`
                : '미결재'}
            </p>
            {item.signatures
              ?.filter((signature) => signature.signer_role === 'APPROVER')
              .map((signature) => (
                <div key={signature.signature_id}>
                  {signature.signer_name} · {new Date(signature.signed_at).toLocaleString()}
                  <img src={signature.image_url} alt={`${signature.signer_name} 결재 서명`} />
                </div>
              ))}
          </div>
        </div>
      ))}
    </div>,
    document.body,
  );
}
