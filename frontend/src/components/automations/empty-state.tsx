import { useTranslation } from "react-i18next";
import { I18nKey } from "#/i18n/declaration";
import DatabaseIcon from "#/icons/database.svg?react";

export function EmptyState() {
  const { t } = useTranslation();

  return (
    <div className="flex flex-col items-center justify-center py-20">
      <DatabaseIcon className="size-12 text-content-icon" />
      <p className="mt-4 text-sm text-content-muted">
        {t(I18nKey.AUTOMATIONS$EMPTY)}
      </p>
    </div>
  );
}
