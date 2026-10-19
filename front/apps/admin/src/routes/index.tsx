import { Button } from '@simai/ui/components/button';
import { createFileRoute } from '@tanstack/react-router';
import { useState } from 'react';

export const Route = createFileRoute('/')({
  component: HomePage,
});

function HomePage() {
  const [count, setCount] = useState(0);

  return (
    <main className="flex min-h-svh flex-col items-center justify-center gap-6">
      <h1 className="text-2xl font-semibold">simAI 관제</h1>
      <Button onClick={() => setCount((c) => c + 1)}>{count}</Button>
    </main>
  );
}
