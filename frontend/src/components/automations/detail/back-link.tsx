import { Link } from "react-router";
import { useTranslation } from "react-i18next";
import { I18nKey } from "#/i18n/declaration";
import ChevronLeftIcon from "#/icons/chevron-left.svg?react";

export function BackLink() {
  const { t } = useTranslation();

  return (
    <Link
      to="/"
      className="inline-flex items-center gap-1.5 text-sm text-content-muted hover:text-content"
    >
      <ChevronLeftIcon className="size-4" />
      {t(I18nKey.AUTOMATIONS$DETAIL$BACK_TO_LIST)}
    </Link>
  );
}
