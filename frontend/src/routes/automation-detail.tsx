import { useState } from "react";
import { useParams, useNavigate } from "react-router";
import { isAxiosError } from "axios";
import { useAutomationDetail } from "#/hooks/use-automation-detail";
import {
  useToggleAutomation,
  useDeleteAutomation,
} from "#/hooks/use-automations";
import { BackLink } from "#/components/automations/detail/back-link";
import { DetailHeader } from "#/components/automations/detail/detail-header";
import { PromptSection } from "#/components/automations/detail/prompt-section";
import { ConfigurationSection } from "#/components/automations/detail/configuration-section";
import { PluginsSection } from "#/components/automations/detail/plugins-section";
import { ActivitySection } from "#/components/automations/detail/activity-section";
import { ActivityLogSection } from "#/components/automations/detail/activity-log-section";
import { DetailSkeleton } from "#/components/automations/detail/detail-skeleton";
import { NotFoundState } from "#/components/automations/detail/not-found-state";
import { ErrorState } from "#/components/automations/error-state";
import { DeleteConfirmationModal } from "#/components/automations/delete-confirmation-modal";

export default function AutomationDetail() {
  const { automationId } = useParams();
  const navigate = useNavigate();
  const [showDeleteModal, setShowDeleteModal] = useState(false);

  const {
    data: automation,
    isLoading,
    isError,
    error,
    refetch,
  } = useAutomationDetail(automationId ?? "");

  const toggleMutation = useToggleAutomation();
  const deleteMutation = useDeleteAutomation();

  const is404 =
    isError && isAxiosError(error) && error.response?.status === 404;

  if (isLoading) {
    return <DetailSkeleton />;
  }

  if (is404) {
    return <NotFoundState />;
  }

  if (isError || !automation) {
    return <ErrorState onRetry={() => refetch()} />;
  }

  const handleToggle = () => {
    toggleMutation.mutate({
      id: automation.id,
      enabled: !automation.enabled,
    });
  };

  const handleDelete = () => {
    deleteMutation.mutate(automation.id, {
      onSuccess: () => {
        navigate("/");
      },
    });
  };

  return (
    <div className="flex flex-col gap-4">
      <BackLink />
      <DetailHeader
        automation={automation}
        onToggle={handleToggle}
        onDelete={() => setShowDeleteModal(true)}
      />
      {automation.prompt && <PromptSection prompt={automation.prompt} />}
      <ConfigurationSection automation={automation} />
      {automation.plugins && automation.plugins.length > 0 && (
        <PluginsSection plugins={automation.plugins} />
      )}
      <ActivitySection
        createdAt={automation.created_at}
        lastRunAt={automation.last_run_at}
      />
      <ActivityLogSection automationId={automation.id} />
      <DeleteConfirmationModal
        automationName={automation.name}
        isOpen={showDeleteModal}
        onConfirm={handleDelete}
        onCancel={() => setShowDeleteModal(false)}
      />
    </div>
  );
}
