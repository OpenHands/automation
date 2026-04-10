import { useTranslation } from "react-i18next";
import { I18nKey } from "#/i18n/declaration";
import CheckCircleIcon from "#/icons/check-circle.svg?react";
import XCircleIcon from "#/icons/x-circle.svg?react";

interface RunStatusBadgeProps {
  status: string;
}

export function RunStatusBadge({ status }: RunStatusBadgeProps) {
  const { t } = useTranslation();
  const isSuccess = status === "successful";

  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium ${
        isSuccess
          ? "border-status-success-border bg-status-success-bg text-status-success-text"
          : "border-status-fail-border bg-status-fail-bg text-status-fail-text"
      }`}
    >
      {isSuccess ? (
        <CheckCircleIcon className="size-3.5" />
      ) : (
        <XCircleIcon className="size-3.5" />
      )}
      {isSuccess
        ? t(I18nKey.AUTOMATIONS$DETAIL$SUCCESSFUL)
        : t(I18nKey.AUTOMATIONS$DETAIL$FAILED)}
    </span>
  );
}
