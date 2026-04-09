import { useNavigate } from "react-router";
import { useTranslation } from "react-i18next";
import { I18nKey } from "#/i18n/declaration";
import type { Automation } from "#/types/automation";
import { ToggleSwitch } from "./toggle-switch";
import { MetadataChip } from "./metadata-chip";
import { KebabMenu } from "./kebab-menu";
import type { KebabMenuItem } from "./kebab-menu";
import FolderIcon from "#/icons/folder.svg?react";
import ClockIcon from "#/icons/clock.svg?react";
import SparkleIcon from "#/icons/sparkle.svg?react";
import PowerIcon from "#/icons/power.svg?react";
import TrashIcon from "#/icons/trash.svg?react";

interface AutomationCardProps {
  automation: Automation;
  onToggle: (id: string, enabled: boolean) => void;
  onDelete: (id: string) => void;
}

export function AutomationCard({
  automation,
  onToggle,
  onDelete,
}: AutomationCardProps) {
  const navigate = useNavigate();
  const { t } = useTranslation();

  const scheduleLabel =
    automation.trigger.schedule_human || automation.trigger.type;

  const menuItems: KebabMenuItem[] = [
    {
      label: automation.enabled
        ? t(I18nKey.AUTOMATIONS$TURN_OFF)
        : t(I18nKey.AUTOMATIONS$TURN_ON),
      icon: <PowerIcon className="size-4" />,
      onClick: () => onToggle(automation.id, automation.enabled),
    },
    {
      label: t(I18nKey.AUTOMATIONS$DELETE),
      icon: <TrashIcon className="size-4" />,
      onClick: () => onDelete(automation.id),
      variant: "danger",
    },
  ];

  return (
    <div
      role="link"
      tabIndex={0}
      onClick={() => navigate(`/${automation.id}`)}
      onKeyDown={(e) => {
        if (e.key === "Enter") navigate(`/${automation.id}`);
      }}
      className="cursor-pointer rounded-2xl border border-border bg-surface-card p-5 transition-colors hover:border-border-hover"
    >
      <div className="flex items-start justify-between">
        <div className="min-w-0 flex-1">
          <h3 className="truncate text-base font-semibold text-white">
            {automation.name}
          </h3>
          <p className="mt-1 line-clamp-2 text-sm text-content-muted">
            {automation.description}
          </p>
        </div>

        <div className="ml-4 flex shrink-0 items-center gap-2">
          <ToggleSwitch
            enabled={automation.enabled}
            label={`Toggle ${automation.name}`}
            onToggle={() => onToggle(automation.id, automation.enabled)}
          />
          <KebabMenu items={menuItems} />
        </div>
      </div>

      <div className="mt-3 flex flex-wrap gap-2">
        <MetadataChip
          icon={<FolderIcon className="size-3.5" />}
          label={automation.repository}
        />
        <MetadataChip
          icon={<ClockIcon className="size-3.5" />}
          label={scheduleLabel}
        />
        <MetadataChip
          icon={<SparkleIcon className="size-3.5" />}
          label={automation.model}
        />
      </div>
    </div>
  );
}
