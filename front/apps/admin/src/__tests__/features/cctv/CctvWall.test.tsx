import { CctvWall } from '@/features/cctv/CctvWall';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

afterEach(cleanup);

const IDS = ['cam1', 'cam2', 'cam3', 'cam4'];

function Wall({ ids = IDS, focusable }: { ids?: string[]; focusable?: boolean }) {
  return (
    <CctvWall
      ids={ids}
      focusable={focusable}
      tile={(id, wall) => (
        <button type="button" onClick={wall.toggle}>
          {id} {wall.focused ? '작게 보기' : '크게 보기'}
        </button>
      )}
    />
  );
}

const small = () => screen.getAllByRole('button', { name: /크게 보기$/ });
const back = () => screen.queryByRole('button', { name: '4분할로' });

describe('CctvWall', () => {
  it('처음에는 4분할이고 4분할로 버튼이 없다', () => {
    render(<Wall />);
    expect(small().map((b) => b.textContent)).toEqual([
      'cam1 크게 보기',
      'cam2 크게 보기',
      'cam3 크게 보기',
      'cam4 크게 보기',
    ]);
    expect(back()).toBeNull();
  });

  it('칸을 누르면 그 칸만 크게 보고, 같은 칸을 다시 누르면 4분할로 돌아간다', () => {
    render(<Wall />);
    fireEvent.click(screen.getByRole('button', { name: 'cam3 크게 보기' }));
    expect(screen.getByRole('button', { name: 'cam3 작게 보기' })).toBeTruthy();
    expect(small()).toHaveLength(3);
    expect(back()).not.toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'cam3 작게 보기' }));
    expect(small()).toHaveLength(4);
    expect(back()).toBeNull();
  });

  it('큰 칸을 두고 다른 칸을 누르면 큰 칸이 바뀐다', () => {
    render(<Wall />);
    fireEvent.click(screen.getByRole('button', { name: 'cam1 크게 보기' }));
    fireEvent.click(screen.getByRole('button', { name: 'cam4 크게 보기' }));
    expect(screen.getByRole('button', { name: 'cam4 작게 보기' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'cam1 크게 보기' })).toBeTruthy();
  });

  it('4분할로 버튼으로 돌아간다', () => {
    render(<Wall />);
    fireEvent.click(screen.getByRole('button', { name: 'cam2 크게 보기' }));
    fireEvent.click(back()!);
    expect(back()).toBeNull();
    expect(small()).toHaveLength(4);
  });

  it('배치가 바뀌어도 칸은 같은 요소로 남는다(캔버스가 다시 만들어지지 않는다)', () => {
    render(<Wall />);
    const before = screen.getByRole('button', { name: 'cam2 크게 보기' });
    fireEvent.click(before);
    expect(screen.getByRole('button', { name: 'cam2 작게 보기' })).toBe(before);
    fireEvent.click(before);
    expect(screen.getByRole('button', { name: 'cam2 크게 보기' })).toBe(before);
  });

  it('크게 보던 카메라가 목록에서 빠지면 4분할로 돌아간다', () => {
    const { rerender } = render(<Wall />);
    fireEvent.click(screen.getByRole('button', { name: 'cam4 크게 보기' }));
    rerender(<Wall ids={['cam1', 'cam2', 'cam3']} />);
    expect(back()).toBeNull();
    expect(small()).toHaveLength(3);
  });

  it('카메라가 하나면 크게 보기를 두지 않는다', () => {
    render(<Wall ids={['cam1']} />);
    fireEvent.click(screen.getByRole('button', { name: 'cam1 크게 보기' }));
    expect(back()).toBeNull();
    expect(screen.getByRole('button', { name: 'cam1 크게 보기' })).toBeTruthy();
  });

  it('focusable 이 false 면 눌러도 배치가 바뀌지 않는다', () => {
    render(<Wall focusable={false} />);
    fireEvent.click(screen.getByRole('button', { name: 'cam1 크게 보기' }));
    expect(back()).toBeNull();
    expect(small()).toHaveLength(4);
  });

  it('크게 볼 때 큰 칸은 첫 열 전체, 나머지는 둘째 열에 놓는다', () => {
    render(<Wall />);
    fireEvent.click(screen.getByRole('button', { name: 'cam2 크게 보기' }));
    const cell = (name: string) => screen.getByRole('button', { name }).parentElement!;
    expect(cell('cam2 작게 보기').style.gridRow).toBe('1 / span 3');
    expect(cell('cam2 작게 보기').style.gridColumn).toBe('1');
    expect(cell('cam1 크게 보기').style.gridColumn).toBe('2');
    expect(cell('cam4 크게 보기').style.gridColumn).toBe('2');
  });
});
