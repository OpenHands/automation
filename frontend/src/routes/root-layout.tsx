import { Outlet } from "react-router";

export { ErrorBoundary } from "#/components/error-boundary";

export default function RootLayout() {
  return (
    <div className="min-h-screen bg-surface text-white">
      <main className="mx-auto max-w-5xl px-8 py-8">
        <Outlet />
      </main>
    </div>
  );
}
