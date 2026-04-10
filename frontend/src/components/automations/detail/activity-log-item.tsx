import type { AutomationRun } from "#/types/automation";
import { RunStatusBadge } from "./run-status-badge";

interface ActivityLogItemProps {
  run: AutomationRun;
}

function formatRunTimestamp(dateStr: string): string {
  const date = new Date(dateStr);
  return date.toLocaleDateString("en-US", {
    weekday: "long",
    year: "numeric",
    month: "long",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

export function ActivityLogItem({ run }: ActivityLogItemProps) {
  return (
    <div className="flex items-center justify-between px-5 py-3">
      <span className="text-sm text-content">
        {formatRunTimestamp(run.started_at)}
      </span>
      <RunStatusBadge status={run.status} />
    </div>
  );
}
