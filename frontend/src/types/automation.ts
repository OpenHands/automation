export interface AutomationTrigger {
  type: string;
  schedule?: string;
  schedule_human?: string;
}

export interface Automation {
  id: string;
  name: string;
  description: string;
  trigger: AutomationTrigger;
  enabled: boolean;
  repository: string;
  model: string;
  created_at: string;
  updated_at: string;
  prompt?: string;
  branch?: string;
  plugins?: string[];
  notification?: string;
  timezone?: string;
  last_run_at?: string | null;
}

export interface AutomationsResponse {
  automations: Automation[];
  total: number;
}

export enum AutomationRunStatus {
  COMPLETED = "COMPLETED",
  FAILED = "FAILED",
}

export interface AutomationRun {
  id: string;
  status: AutomationRunStatus;
  conversation_id: string;
  error_detail: string | null;
  started_at: string;
  completed_at: string | null;
}

export interface AutomationRunsResponse {
  runs: AutomationRun[];
  total: number;
}
