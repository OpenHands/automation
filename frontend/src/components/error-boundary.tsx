import { isRouteErrorResponse, useRouteError } from "react-router";
import { useTranslation } from "react-i18next";
import { I18nKey } from "#/i18n/declaration";

export function ErrorBoundary() {
  const error = useRouteError();
  const { t } = useTranslation();

  if (isRouteErrorResponse(error)) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-white dark:bg-neutral-900">
        <div className="text-center">
          <h1 className="text-4xl font-bold text-neutral-900 dark:text-neutral-100">
            {error.status}
          </h1>
          <p className="mt-2 text-neutral-600 dark:text-neutral-400">
            {error.statusText}
          </p>
        </div>
      </div>
    );
  }

  if (error instanceof Error) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-white dark:bg-neutral-900">
        <div className="text-center">
          <h1 className="text-2xl font-bold text-neutral-900 dark:text-neutral-100">
            {t(I18nKey.ERROR$SOMETHING_WENT_WRONG)}
          </h1>
          <pre className="mt-2 text-sm text-neutral-600 dark:text-neutral-400">
            {error.message}
          </pre>
        </div>
      </div>
    );
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-white dark:bg-neutral-900">
      <div className="text-center">
        <h1 className="text-2xl font-bold text-neutral-900 dark:text-neutral-100">
          {t(I18nKey.ERROR$UNKNOWN)}
        </h1>
      </div>
    </div>
  );
}
