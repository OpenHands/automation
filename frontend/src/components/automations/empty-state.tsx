import { useTranslation } from "react-i18next";
import { I18nKey } from "#/i18n/declaration";
import DatabaseIcon from "#/icons/database.svg?react";
import TerminalIcon from "#/icons/terminal.svg?react";
import SparkleIcon from "#/icons/sparkle.svg?react";

const DOCS_URL =
  "https://docs.openhands.dev/openhands/usage/automations/overview";
const NEW_CONVERSATION_URL = "/";
const PLUGIN_COMMAND = "/openhands-automation create";

export function EmptyState() {
  const { t } = useTranslation();

  return (
    <div className="flex flex-col items-center justify-center py-12">
      <DatabaseIcon className="size-12 text-content-icon" />
      <p className="mt-4 text-sm text-content-muted">
        {t(I18nKey.AUTOMATIONS$EMPTY)}
      </p>

      {/* How to create section */}
      <div className="mt-8 w-full max-w-2xl">
        <h3 className="text-center text-sm font-medium text-content">
          {t(I18nKey.AUTOMATIONS$EMPTY_HOW_TO_CREATE_TITLE)}
        </h3>

        <div className="mt-6 grid gap-4 sm:grid-cols-2">
          {/* Option 1: Claude Code / Codex */}
          <div className="rounded-lg border border-border bg-surface-card p-4">
            <div className="flex items-center gap-2">
              <TerminalIcon className="size-5 text-content-muted" />
              <span className="text-sm font-medium text-content">
                {t(I18nKey.AUTOMATIONS$EMPTY_OPTION_PLUGIN_TITLE)}
              </span>
            </div>
            <p className="mt-2 text-sm text-content-muted">
              {t(I18nKey.AUTOMATIONS$EMPTY_OPTION_PLUGIN_DESC)}
            </p>
            <code className="mt-2 block rounded bg-surface-elevated px-3 py-2 font-mono text-xs text-content">
              {PLUGIN_COMMAND}
            </code>
          </div>

          {/* Option 2: OpenHands Cloud conversation */}
          <div className="rounded-lg border border-border bg-surface-card p-4">
            <div className="flex items-center gap-2">
              <SparkleIcon className="size-5 text-content-muted" />
              <span className="text-sm font-medium text-content">
                {t(I18nKey.AUTOMATIONS$EMPTY_OPTION_CONVERSATION_TITLE)}
              </span>
            </div>
            <p className="mt-2 text-sm text-content-muted">
              {t(I18nKey.AUTOMATIONS$EMPTY_OPTION_CONVERSATION_DESC)}
            </p>
            <a
              href={NEW_CONVERSATION_URL}
              className="mt-2 inline-flex items-center gap-1 rounded-md bg-surface-elevated px-3 py-2 text-xs font-medium text-content hover:bg-border transition-colors"
            >
              {t(I18nKey.AUTOMATIONS$EMPTY_START_CONVERSATION)}
              <span aria-hidden="true">→</span>
            </a>
          </div>
        </div>

        {/* Documentation link */}
        <p className="mt-6 text-center text-sm text-content-muted">
          <a
            href={DOCS_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="underline hover:text-content transition-colors"
          >
            {t(I18nKey.AUTOMATIONS$EMPTY_LEARN_MORE)}
          </a>
        </p>
      </div>
    </div>
  );
}
