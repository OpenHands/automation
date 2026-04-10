import type { AutomationRun } from "#/types/automation";

const daysAgo = (days: number, hour = 9) => {
  const d = new Date(Date.now() - days * 86_400_000);
  d.setHours(hour, 0, 0, 0);
  return d.toISOString();
};

function makeRun(
  id: string,
  status: "successful" | "failed",
  startedDaysAgo: number,
  hour = 9,
): AutomationRun {
  const started = daysAgo(startedDaysAgo, hour);
  return {
    id,
    status,
    conversation_id: `conv-${id}`,
    error_detail: status === "failed" ? "Process exited with code 1" : null,
    started_at: started,
    completed_at: new Date(new Date(started).getTime() + 120_000).toISOString(),
  };
}

export const MOCK_AUTOMATION_RUNS: Record<string, AutomationRun[]> = {
  "a1000000-0000-0000-0000-000000000001": [
    makeRun("r1-01", "successful", 0),
    makeRun("r1-02", "successful", 1),
    makeRun("r1-03", "failed", 2),
    makeRun("r1-04", "successful", 3),
    makeRun("r1-05", "successful", 4),
    makeRun("r1-06", "successful", 7),
    makeRun("r1-07", "failed", 8),
    makeRun("r1-08", "successful", 9),
    makeRun("r1-09", "successful", 10),
    makeRun("r1-10", "successful", 11),
  ],
  "a1000000-0000-0000-0000-000000000002": [
    makeRun("r2-01", "successful", 0, 1),
    makeRun("r2-02", "successful", 1, 1),
    makeRun("r2-03", "successful", 2, 1),
    makeRun("r2-04", "failed", 3, 1),
    makeRun("r2-05", "successful", 4, 1),
  ],
  "a1000000-0000-0000-0000-000000000003": [
    makeRun("r3-01", "successful", 1),
    makeRun("r3-02", "successful", 2),
    makeRun("r3-03", "successful", 3),
  ],
  "a1000000-0000-0000-0000-000000000004": [
    makeRun("r4-01", "failed", 14, 11),
    makeRun("r4-02", "successful", 21, 11),
  ],
  "a1000000-0000-0000-0000-000000000005": [],
};
