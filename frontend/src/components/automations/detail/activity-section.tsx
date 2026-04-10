import { useTranslation } from "react-i18next";
import { I18nKey } from "#/i18n/declaration";
import ActivityIcon from "#/icons/activity.svg?react";
import CalendarIcon from "#/icons/calendar.svg?react";
import ClockIcon from "#/icons/clock.svg?react";
import { SectionCard } from "./section-card";
import { ConfigField } from "./config-field";

interface ActivitySectionProps {
  createdAt: string;
  lastRunAt: string | null | undefined;
}

function formatDate(dateStr: string): string {
  const date = new Date(dateStr);
  return date.toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function formatRelativeTime(dateStr: string): string {
  const now = Date.now();
  const then = new Date(dateStr).getTime();
  const diffMs = now - then;
  const diffMins = Math.floor(diffMs / 60_000);
  const diffHours = Math.floor(diffMs / 3_600_000);
  const diffDays = Math.floor(diffMs / 86_400_000);

  if (diffMins < 1) return "Just now";
  if (diffMins < 60) return `${diffMins}m ago`;
  if (diffHours < 24) return `${diffHours}h ago`;
  if (diffDays === 1) return "Yesterday";
  if (diffDays < 7) return `${diffDays}d ago`;
  return formatDate(dateStr);
}

export function ActivitySection({
  createdAt,
  lastRunAt,
}: ActivitySectionProps) {
  const { t } = useTranslation();

  return (
    <SectionCard
      icon={<ActivityIcon className="size-4" />}
      title={t(I18nKey.AUTOMATIONS$DETAIL$ACTIVITY)}
    >
      <div className="grid grid-cols-2 gap-x-4">
        <ConfigField
          icon={<CalendarIcon className="size-3.5" />}
          label={t(I18nKey.AUTOMATIONS$DETAIL$CREATED)}
        >
          {formatDate(createdAt)}
        </ConfigField>

        <ConfigField
          icon={<ClockIcon className="size-3.5" />}
          label={t(I18nKey.AUTOMATIONS$DETAIL$LAST_RUN)}
        >
          {lastRunAt ? formatRelativeTime(lastRunAt) : "Never"}
        </ConfigField>
      </div>
    </SectionCard>
  );
}
