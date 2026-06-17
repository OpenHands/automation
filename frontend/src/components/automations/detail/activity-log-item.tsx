import { useTranslation } from "react-i18next";
import { I18nKey } from "#/i18n/declaration";
import type { AutomationRun } from "#/types/automation";
import { RunStatusBadge } from "./run-status-badge";

interface ActivityLogItemProps {
  run: AutomationRun;
  timeZone?: string;
}

const RUN_TIMESTAMP_OPTIONS: Intl.DateTimeFormatOptions = {
  weekday: "long",
  year: "numeric",
  month: "long",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
};

function formatRunTimestamp(
  dateStr: string,
  locale: string,
  timeZone?: string,
): string {
  const date = new Date(dateStr);
  const options: Intl.DateTimeFormatOptions = { ...RUN_TIMESTAMP_OPTIONS };

  if (timeZone) {
    options.timeZone = timeZone;
    options.timeZoneName = "short";
  }

  try {
    return date.toLocaleDateString(locale, options);
  } catch {
    return date.toLocaleDateString(locale, RUN_TIMESTAMP_OPTIONS);
  }
}

function getConversationUrl(conversationId: string): string {
  const { hostname, origin, pathname } = window.location;
  const isLocalHost = hostname === "localhost" || hostname === "127.0.0.1";

  if (isLocalHost) {
    return `https://app.all-hands.dev/conversations/${conversationId}`;
  }

  const automationsPathIndex = pathname.indexOf("/automations");
  const prefix =
    automationsPathIndex >= 0 ? pathname.slice(0, automationsPathIndex) : "";
  return `${origin}${prefix}/conversations/${conversationId}`;
}

export function ActivityLogItem({ run, timeZone }: ActivityLogItemProps) {
  const { t, i18n } = useTranslation();
  const hasConversation = !!run.conversation_id;
  const runTimestamp = formatRunTimestamp(
    run.started_at,
    i18n.language,
    timeZone,
  );

  const content = (
    <>
      <div className="flex items-center gap-3">
        <span className="text-sm text-content">{runTimestamp}</span>
        {!hasConversation && (
          <span className="text-xs text-content-muted italic">
            ({t(I18nKey.AUTOMATIONS$DETAIL$NO_CONVERSATION)})
          </span>
        )}
      </div>
      <RunStatusBadge status={run.status} />
    </>
  );

  if (hasConversation && run.conversation_id) {
    return (
      <a
        href={getConversationUrl(run.conversation_id)}
        target="_blank"
        rel="noopener noreferrer"
        className="flex items-center justify-between px-5 py-3 transition-colors cursor-pointer hover:bg-surface-elevated focus:bg-surface-elevated focus:outline-none"
        aria-label={`View conversation for run at ${runTimestamp}`}
      >
        {content}
      </a>
    );
  }

  return (
    <div className="flex items-center justify-between px-5 py-3 cursor-default">
      {content}
    </div>
  );
}
