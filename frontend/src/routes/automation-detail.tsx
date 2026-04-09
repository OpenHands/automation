import { useParams, Link } from "react-router";
import { useTranslation } from "react-i18next";
import { I18nKey } from "#/i18n/declaration";

export default function AutomationDetail() {
  const { automationId } = useParams();
  const { t } = useTranslation();

  return (
    <div>
      <Link
        to="/"
        className="text-sm text-blue-500 hover:text-blue-400 underline"
      >
        {t(I18nKey.AUTOMATIONS$DETAIL$BACK)}
      </Link>
      <h2 className="mt-4 text-2xl font-bold">
        {t(I18nKey.AUTOMATIONS$DETAIL$TITLE)}
      </h2>
      <p className="mt-2 text-neutral-600 dark:text-neutral-400">
        {t(I18nKey.AUTOMATIONS$DETAIL$ID_LABEL)} {automationId}
      </p>
      <p className="mt-2 text-neutral-600 dark:text-neutral-400">
        {t(I18nKey.AUTOMATIONS$DETAIL$PLACEHOLDER)}
      </p>
    </div>
  );
}
