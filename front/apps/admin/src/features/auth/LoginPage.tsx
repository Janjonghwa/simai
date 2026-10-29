import { loginFormSchema } from '@/features/auth/types';
import { useLogin } from '@/features/auth/useLogin';
import { BrandMark, Wordmark } from '@/features/layout/BrandMark';
import { zodResolver } from '@hookform/resolvers/zod';
import { Alert, AlertDescription } from '@simai/ui/components/alert';
import { Button } from '@simai/ui/components/button';
import { Card, CardContent, CardHeader } from '@simai/ui/components/card';
import { Input } from '@simai/ui/components/input';
import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupInput,
} from '@simai/ui/components/input-group';
import { Label } from '@simai/ui/components/label';
import { Eye, EyeOff } from 'lucide-react';
import { useState } from 'react';
import { useForm } from 'react-hook-form';

export function LoginPage() {
  const { login, pending, error } = useLogin();
  const [showPassword, setShowPassword] = useState(false);
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
    <main className="flex min-h-svh items-center justify-center bg-background bg-blueprint">
      <Card className="w-[400px] gap-6 rounded-[16px] shadow-float ring-border [--card-spacing:--spacing(8)]">
        <CardHeader className="gap-4">
          <div className="flex items-center gap-2.5">
            <BrandMark />
            <Wordmark />
          </div>
          <h1 className="text-[22px] font-bold">관제 로그인</h1>
        </CardHeader>
        <CardContent>
          <form
            noValidate
            className="flex flex-col gap-4"
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
              <Label htmlFor="username">아이디</Label>
              <Input
                id="username"
                autoComplete="username"
                autoCapitalize="none"
                spellCheck={false}
                autoFocus
                aria-invalid={errors.username ? true : undefined}
                aria-describedby={errors.username ? 'username-error' : undefined}
                {...register('username')}
              />
              {errors.username && (
                <p id="username-error" className="text-destructive">
                  {errors.username.message}
                </p>
              )}
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="password">비밀번호</Label>
              <InputGroup>
                <InputGroupInput
                  id="password"
                  type={showPassword ? 'text' : 'password'}
                  autoComplete="current-password"
                  aria-invalid={errors.password ? true : undefined}
                  aria-describedby={errors.password ? 'password-error' : undefined}
                  {...register('password')}
                />
                <InputGroupAddon align="inline-end">
                  <InputGroupButton
                    size="icon-xs"
                    aria-label={showPassword ? '비밀번호 숨기기' : '비밀번호 보기'}
                    aria-pressed={showPassword}
                    onClick={() => setShowPassword((shown) => !shown)}
                  >
                    {showPassword ? <EyeOff /> : <Eye />}
                  </InputGroupButton>
                </InputGroupAddon>
              </InputGroup>
              {errors.password && (
                <p id="password-error" className="text-destructive">
                  {errors.password.message}
                </p>
              )}
            </div>
            {error && (
              <Alert variant="destructive">
                <AlertDescription>{error}</AlertDescription>
              </Alert>
            )}
            <Button type="submit" size="lg" disabled={pending} className="mt-2">
              {pending ? '로그인 중…' : '로그인'}
            </Button>
          </form>
        </CardContent>
      </Card>
    </main>
  );
}
