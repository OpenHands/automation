import { useTranslation } from "react-i18next";
import { I18nKey } from "#/i18n/declaration";

export default function AutomationsList() {
  const { t } = useTranslation();

  return (
    <div>
      <h2 className="text-2xl font-bold">{t(I18nKey.AUTOMATIONS$TITLE)}</h2>
      <p className="mt-4 text-neutral-600 dark:text-neutral-400">
        {t(I18nKey.AUTOMATIONS$EMPTY)}
      </p>
    </div>
  );
}
