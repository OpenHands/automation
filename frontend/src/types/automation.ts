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
}

export interface AutomationsResponse {
  automations: Automation[];
  total: number;
}
