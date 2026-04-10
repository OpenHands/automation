import { useTranslation } from "react-i18next";
import type { AutomationRun } from "#/types/automation";
import { RunStatusBadge } from "./run-status-badge";

interface ActivityLogItemProps {
  run: AutomationRun;
}

function formatRunTimestamp(dateStr: string, locale: string): string {
  const date = new Date(dateStr);
  return date.toLocaleDateString(locale, {
    weekday: "long",
    year: "numeric",
    month: "long",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

export function ActivityLogItem({ run }: ActivityLogItemProps) {
  const { i18n } = useTranslation();

  return (
    <div className="flex items-center justify-between px-5 py-3">
      <span className="text-sm text-content">
        {formatRunTimestamp(run.started_at, i18n.language)}
      </span>
      <RunStatusBadge status={run.status} />
    </div>
  );
}
