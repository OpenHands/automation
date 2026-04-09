import { Outlet } from "react-router";
import { useTranslation } from "react-i18next";
import { I18nKey } from "#/i18n/declaration";

export { ErrorBoundary } from "#/components/error-boundary";

export default function RootLayout() {
  const { t } = useTranslation();

  return (
    <div className="min-h-screen bg-white dark:bg-neutral-900 text-neutral-900 dark:text-neutral-100">
      <header className="border-b border-neutral-200 dark:border-neutral-700 px-6 py-4">
        <h1 className="text-xl font-semibold">
          {t(I18nKey.AUTOMATIONS$TITLE)}
        </h1>
      </header>
      <main className="p-6">
        <Outlet />
      </main>
    </div>
  );
}
