import { LoginHero } from '@/features/auth/LoginHero';
import { loginFormSchema } from '@/features/auth/types';
import { useLogin } from '@/features/auth/useLogin';
import { useMediaQuery } from '@/features/auth/useMediaQuery';
import { BrandMark, Wordmark } from '@/features/layout/BrandMark';
import { zodResolver } from '@hookform/resolvers/zod';
import { Alert, AlertDescription } from '@simai/ui/components/alert';
import { Button } from '@simai/ui/components/button';
import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupInput,
} from '@simai/ui/components/input-group';
import { Label } from '@simai/ui/components/label';
import { Eye, EyeOff, LockKeyhole, ShieldCheck, UserRound } from 'lucide-react';
import { useState } from 'react';
import { useForm } from 'react-hook-form';

// 입력 칸은 높고 둥글게 하고 앞에 아이콘을 둔다. 아이콘을 눌러도 입력 칸으로 간다(InputGroupAddon).
const FIELD = 'h-12 rounded-xl bg-card/60 dark:bg-input/20';
const FIELD_INPUT = 'h-full text-[15px] md:text-[15px]';
const FIELD_ICON = 'pl-3.5 [&>svg:not([class*=size-])]:size-[18px]';

export function LoginPage() {
  const { login, pending, error } = useLogin();
  const [showPassword, setShowPassword] = useState(false);
  // 오른쪽 카드는 넓은 화면(lg)에서만 그린다. 좁은 화면에서 숨긴 채 영상을 받지 않게 한다.
  const wide = useMediaQuery('(min-width: 64rem)');
  const {
    register,
    handleSubmit,
    resetField,
    setFocus,
    formState: { errors },
  } = useForm({
    resolver: zodResolver(loginFormSchema),
    defaultValues: { username: '', password: '' },
  });

  return (
    <main className="grid min-h-svh bg-background px-6 py-4 lg:grid-cols-2 lg:gap-4 lg:p-5">
      <section className="mx-auto flex w-full max-w-[420px] flex-col py-6 lg:py-8">
        <div className="flex items-center gap-2.5">
          <BrandMark />
          <Wordmark />
        </div>
        <div className="flex flex-1 items-center py-12">
          <div className="flex w-full flex-col gap-10">
            <div className="flex flex-col gap-3">
              <h1 className="text-[32px] leading-tight font-bold tracking-[-0.8px]">
                다시 오신 걸 환영합니다
              </h1>
              <p className="text-[15px] break-keep text-muted-foreground">
                관제 계정으로 로그인해 현장 안전 관제를 이어 가세요.
              </p>
            </div>
            <form
              noValidate
              className="flex flex-col gap-5"
              onSubmit={(event) => {
                // 보낼 때는 다시 가린다. 비밀번호 관리자가 칸을 알아보고, 화면에 비밀번호가 남지 않게 한다.
                setShowPassword(false);
                void handleSubmit((values) => {
                  // 아이디는 남기고 비밀번호는 보내자마자 지운다. 실패하면 비밀번호 칸으로 돌아가 다시 받는다.
                  login(values, { onError: () => setFocus('password') });
                  resetField('password');
                })(event);
              }}
            >
              <div className="flex flex-col gap-2">
                <Label htmlFor="username" className="font-semibold">
                  아이디
                </Label>
                <InputGroup className={FIELD}>
                  <InputGroupAddon className={FIELD_ICON}>
                    <UserRound aria-hidden="true" />
                  </InputGroupAddon>
                  <InputGroupInput
                    id="username"
                    placeholder="아이디를 입력하세요"
                    autoComplete="username"
                    autoCapitalize="none"
                    spellCheck={false}
                    autoFocus
                    className={FIELD_INPUT}
                    aria-invalid={errors.username ? true : undefined}
                    aria-describedby={errors.username ? 'username-error' : undefined}
                    {...register('username')}
                  />
                </InputGroup>
                {errors.username && (
                  <p id="username-error" className="text-sm text-destructive">
                    {errors.username.message}
                  </p>
                )}
              </div>
              <div className="flex flex-col gap-2">
                <Label htmlFor="password" className="font-semibold">
                  비밀번호
                </Label>
                <InputGroup className={FIELD}>
                  <InputGroupAddon className={FIELD_ICON}>
                    <LockKeyhole aria-hidden="true" />
                  </InputGroupAddon>
                  <InputGroupInput
                    id="password"
                    type={showPassword ? 'text' : 'password'}
                    placeholder="비밀번호를 입력하세요"
                    autoComplete="current-password"
                    className={FIELD_INPUT}
                    aria-invalid={errors.password ? true : undefined}
                    aria-describedby={errors.password ? 'password-error' : undefined}
                    {...register('password')}
                  />
                  <InputGroupAddon align="inline-end" className="pr-3">
                    <InputGroupButton
                      size="icon-sm"
                      className="text-muted-foreground hover:text-foreground [&>svg]:size-[18px]"
                      aria-label={showPassword ? '비밀번호 숨기기' : '비밀번호 보기'}
                      aria-pressed={showPassword}
                      onClick={() => setShowPassword((shown) => !shown)}
                    >
                      {showPassword ? <EyeOff /> : <Eye />}
                    </InputGroupButton>
                  </InputGroupAddon>
                </InputGroup>
                {errors.password && (
                  <p id="password-error" className="text-sm text-destructive">
                    {errors.password.message}
                  </p>
                )}
              </div>
              {error && (
                <Alert variant="destructive">
                  <AlertDescription>{error}</AlertDescription>
                </Alert>
              )}
              <Button
                type="submit"
                size="lg"
                disabled={pending}
                className="mt-3 h-12 rounded-full text-[15px] font-semibold shadow-lg shadow-primary/20"
              >
                {pending ? '로그인 중…' : '로그인'}
              </Button>
            </form>
          </div>
        </div>
        <p className="flex items-center gap-1.5 text-[13px] text-faint">
          <ShieldCheck className="size-3.5" aria-hidden="true" />
          등록된 관제 계정으로만 로그인할 수 있습니다.
        </p>
      </section>
      {wide && <LoginHero className="hidden lg:flex" />}
    </main>
  );
}
