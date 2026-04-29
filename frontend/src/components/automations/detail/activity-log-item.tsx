import { useTranslation } from "react-i18next";
import { I18nKey } from "#/i18n/declaration";
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

function getConversationUrl(conversationId: string): string {
  const { hostname } = window.location;
  let baseHost: string;

  if (hostname.includes("staging.all-hands.dev")) {
    baseHost = "staging.all-hands.dev";
  } else if (hostname.includes("app.all-hands.dev")) {
    baseHost = "app.all-hands.dev";
  } else {
    // For localhost development, default to app.all-hands.dev
    baseHost = "app.all-hands.dev";
  }

  return `https://${baseHost}/conversations/${conversationId}`;
}

export function ActivityLogItem({ run }: ActivityLogItemProps) {
  const { t, i18n } = useTranslation();
  const hasConversation = !!run.conversation_id;

  const handleClick = () => {
    if (hasConversation && run.conversation_id) {
      window.open(getConversationUrl(run.conversation_id), "_blank");
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if ((e.key === "Enter" || e.key === " ") && hasConversation) {
      e.preventDefault();
      handleClick();
    }
  };

  return (
    <div
      role="button"
      tabIndex={hasConversation ? 0 : -1}
      onClick={handleClick}
      onKeyDown={handleKeyDown}
      className={`flex items-center justify-between px-5 py-3 transition-colors ${
        hasConversation
          ? "cursor-pointer hover:bg-surface-elevated focus:bg-surface-elevated focus:outline-none"
          : "cursor-default"
      }`}
      aria-label={
        hasConversation
          ? `View conversation for run at ${formatRunTimestamp(run.started_at, i18n.language)}`
          : t(I18nKey.AUTOMATIONS$DETAIL$NO_CONVERSATION)
      }
    >
      <div className="flex items-center gap-3">
        <span className="text-sm text-content">
          {formatRunTimestamp(run.started_at, i18n.language)}
        </span>
        {!hasConversation && (
          <span className="text-xs text-content-muted italic">
            ({t(I18nKey.AUTOMATIONS$DETAIL$NO_CONVERSATION)})
          </span>
        )}
      </div>
      <RunStatusBadge status={run.status} />
    </div>
  );
}
