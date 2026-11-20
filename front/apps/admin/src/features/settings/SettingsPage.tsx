import { useCurrentRole } from '@/features/auth/useCurrentRole';
import { useCanAck } from '@/features/events/ack';
import { useAlarmSoundState } from '@/features/events/useAlarmSound';
import { GpuSettingsSection } from '@/features/gpu/GpuSettingsSection';
import { PageTitle } from '@/features/layout/PageTitle';
import { useThemeStore, type Theme } from '@/features/layout/theme.store';
import { alarmSound } from '@/lib/alarmSound';
import { Button } from '@simai/ui/components/button';
import type { ReactNode } from 'react';

// 설정 페이지(/settings, S15P31A101-705, -824). 상단바 계정 메뉴의 「설정」 하나로 들어온다. front 에 흩어져 있던 설정을 모았다:
// 화면(다크·화이트 모드, 모든 계정, 브라우저에 저장) · 경보 소리(확인 권한이 있는 계정만, 상단바 스피커 버튼과 같은 저장값·실제 상태 표시) ·
// 고급 설정 · GPU(관리자만, 기본 접힘, 서버 GET·POST /api/gpu/settings 에 새 버전). 새 서버 기능은 없다. 계정이 쓸 수 없는 구역은 보이지 않는다.

const THEMES: { value: Theme; label: string }[] = [
  { value: 'dark', label: '다크 모드' },
  { value: 'light', label: '화이트 모드' },
];

function Section({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section
      aria-labelledby={id}
      className="flex flex-col rounded-xl bg-card text-card-foreground ring-1 ring-border"
    >
      <h2 id={id} className="border-b border-border px-5 py-3 text-base font-semibold">
        {title}
      </h2>
      <div className="flex flex-col gap-3 p-5 text-sm">{children}</div>
    </section>
  );
}

function ThemeSection() {
  const theme = useThemeStore((s) => s.theme);
  const toggle = useThemeStore((s) => s.toggle);
  return (
    <Section id="settings-display" title="화면">
      <fieldset className="flex flex-wrap gap-x-6 gap-y-2">
        <legend className="sr-only">화면 모드</legend>
        {THEMES.map((t) => (
          <label key={t.value} className="flex items-center gap-1.5">
            <input
              type="radio"
              name="settings-theme"
              checked={theme === t.value}
              onChange={() => {
                if (theme !== t.value) toggle();
              }}
            />
            {t.label}
          </label>
        ))}
      </fieldset>
    </Section>
  );
}

function SoundSection() {
  const { muted, ready } = useAlarmSoundState();
  // 상단 바 스피커 단추(SoundButton)와 같은 우선순위: 사용자가 끈 상태를 먼저 보이고, 켠 상태에서는 브라우저 차단 여부를 알린다.
  const statusText = muted ? '경보음 꺼짐' : ready ? '경보음 켜짐' : '경보음 차단됨';
  return (
    <Section id="settings-sound" title="경보 소리">
      <label className="flex items-center gap-2">
        <input
          type="checkbox"
          checked={!muted}
          onChange={(e) => {
            alarmSound.setMuted(!e.target.checked);
            if (e.target.checked) void alarmSound.unlock();
          }}
        />
        확인 대기 경보를 소리로 알리기
      </label>
      <div className="flex items-center gap-3 text-muted-foreground">
        <span data-slot="sound-status">{statusText}</span>
        {!muted && !ready && (
          <Button variant="quiet" size="sm" onClick={() => void alarmSound.unlock()}>
            소리 켜기
          </Button>
        )}
      </div>
    </Section>
  );
}

export function SettingsPage() {
  const isAdmin = useCurrentRole() === 'ADMIN';
  const canAck = useCanAck();
  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 py-2">
      <PageTitle>설정</PageTitle>
      <ThemeSection />
      {canAck && <SoundSection />}
      {isAdmin && <GpuSettingsSection />}
    </div>
  );
}
