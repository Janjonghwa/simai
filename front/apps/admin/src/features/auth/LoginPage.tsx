import { BrandMark } from '@/features/layout/BrandMark';

// 로그인 폼은 서버 로그인 API(S15P31A101-61)가 정해지면 넣는다.
export function LoginPage() {
  return (
    <main className="flex min-h-svh items-center justify-center bg-background">
      <div className="flex w-[400px] flex-col gap-4 rounded-xl bg-card p-8 ring-1 ring-border">
        <div className="flex items-center gap-2.5">
          <BrandMark />
          <strong className="text-[21px] font-bold tracking-[-0.4px]">
            sim<span className="text-primary">AI</span>
          </strong>
        </div>
        <h1 className="text-[22px] font-bold">관제 로그인</h1>
      </div>
    </main>
  );
}
