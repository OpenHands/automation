export interface User {
  user_id: string;
  email: string;
  org_id: string;
  org_name: string;
  role: string;
  permissions: string[];
  language?: string;
}
