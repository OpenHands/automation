/* eslint-disable i18next/no-literal-string */
import { Link } from "react-router";

interface ComponentRow {
  name: string;
  location: string;
  responsibility: string;
}

interface FlowStep {
  label: string;
  detail: string;
}

const componentRows: ComponentRow[] = [
  {
    name: "Frontend SPA",
    location: "frontend/src/routes/*",
    responsibility:
      "Read-only control plane for listing automations, inspecting run history, toggling enabled state, deleting definitions, and downloading tarballs.",
  },
  {
    name: "FastAPI service",
    location: "openhands/automation/app.py",
    responsibility:
      "Bootstraps auth, database sessions, routers, static frontend hosting, and the scheduler, dispatcher, and watchdog background loops.",
  },
  {
    name: "API routers",
    location: "router.py, preset_router.py, uploads.py, webhook_router.py",
    responsibility:
      "Expose CRUD, preset creation, tarball upload/download, custom webhook management, run cancellation, and completion callback endpoints.",
  },
  {
    name: "Event ingress",
    location: "event_router.py, trigger_matcher.py, filter_eval.py",
    responsibility:
      "Verifies HMAC signatures, parses built-in or custom webhook payloads, evaluates event patterns and JMESPath filters, then creates pending runs.",
  },
  {
    name: "Scheduler",
    location: "scheduler.py",
    responsibility:
      "Polls enabled cron automations fairly, uses row locking where supported, and turns due schedules into PENDING AutomationRun rows.",
  },
  {
    name: "Dispatcher",
    location: "dispatcher.py, execution.py, backends/*",
    responsibility:
      "Claims PENDING runs, marks them RUNNING, prepares execution context and environment, uploads or downloads tarballs, starts bash, and returns immediately.",
  },
  {
    name: "Watchdog",
    location: "watchdog.py, utils/agent_server.py, utils/sandbox.py",
    responsibility:
      "Finds RUNNING runs past timeout_at, verifies actual bash status in the execution backend, reconciles missed callbacks, and cleans up stale resources.",
  },
  {
    name: "Persistence",
    location: "models.py, migrations/*, storage/*",
    responsibility:
      "Stores automation definitions, runs, uploads, custom webhooks, and tarball bytes through PostgreSQL or SQLite plus GCS, S3, or local file storage.",
  },
];

const createFlow: FlowStep[] = [
  {
    label: "Preset or upload",
    detail:
      "Users either upload a custom tarball or call the prompt/plugin preset endpoints, which generate main.py, prompt/config files, and setup.sh.",
  },
  {
    label: "Store bytes",
    detail:
      "Generated or uploaded tarballs are streamed into the configured FileStore and recorded as TarballUpload rows with oh-internal:// URLs.",
  },
  {
    label: "Create definition",
    detail:
      "Automation rows bind owner, org, trigger JSON, model profile, tarball path, entrypoint, setup path, timeout, and optional prompt metadata.",
  },
];

const triggerFlow: FlowStep[] = [
  {
    label: "Cron",
    detail:
      "scheduler_loop polls enabled automations, updates last_polled_at for fair batching, checks cron due time, and creates PENDING runs.",
  },
  {
    label: "Event",
    detail:
      "receive_event verifies the source secret, parses the payload, matches trigger.on plus optional filter, and creates PENDING runs with event_payload.",
  },
  {
    label: "Manual",
    detail:
      "POST /v1/{automation_id}/dispatch validates ownership and creates a PENDING run for the same dispatcher queue.",
  },
];

const dispatchFlow: FlowStep[] = [
  {
    label: "Claim",
    detail:
      "dispatch_pending_runs selects oldest PENDING rows, using FOR UPDATE SKIP LOCKED outside SQLite, then atomically marks them RUNNING and sets timeout_at.",
  },
  {
    label: "Acquire context",
    detail:
      "get_backend chooses CloudSandboxBackend for fresh Cloud sandboxes or LocalAgentServerBackend for a persistent local agent server.",
  },
  {
    label: "Prepare inputs",
    detail:
      "Internal tarballs are downloaded from storage by the service; external HTTP tarballs are downloaded inside the execution environment with curl limits.",
  },
  {
    label: "Start bash",
    detail:
      "execute_in_context exports run env vars, extracts the tarball into the work directory, optionally runs setup.sh, starts the entrypoint, and records bash_command_id.",
  },
];

const completionFlow: FlowStep[] = [
  {
    label: "Callback",
    detail:
      "The SDK script posts to /v1/runs/{run_id}/complete using the injected credentials; ownership is checked against the parent automation.",
  },
  {
    label: "Optimistic transition",
    detail:
      "Completion updates only WHERE status = RUNNING, so callbacks, cancellation, and watchdog reconciliation cannot overwrite a terminal state.",
  },
  {
    label: "Fallback verification",
    detail:
      "watchdog_loop scans stale RUNNING rows, asks the backend to verify the exact bash command result, and marks COMPLETED or FAILED if the callback was missed.",
  },
  {
    label: "Cleanup",
    detail:
      "Cloud runs delete sandboxes unless keep_alive is set; local runs keep the configured agent server and isolated workspace for inspection.",
  },
];

const envVars = [
  ["OPENHANDS_API_KEY", "Per-user key minted for Cloud SDK/API access."],
  ["OPENHANDS_CLOUD_API_URL", "Base URL for OpenHands Cloud APIs."],
  [
    "AGENT_SERVER_URL",
    "Local-mode agent server URL from inside the bash chain.",
  ],
  [
    "SESSION_API_KEY",
    "Agent-server session key for file, bash, and settings APIs.",
  ],
  [
    "AUTOMATION_CALLBACK_URL",
    "Run completion endpoint on the automation service.",
  ],
  [
    "AUTOMATION_RUN_ID",
    "The AutomationRun identifier included in callback payloads.",
  ],
  ["AUTOMATION_API_URL", "Automation service base URL for user code."],
  [
    "AUTOMATION_EVENT_PAYLOAD",
    "JSON trigger context with cron/event metadata and payload.",
  ],
  ["AUTOMATION_MODEL", "Resolved model profile name when configured."],
  ["WORKSPACE_BASE", "Local-mode run-isolated workspace root."],
];

const dataModelRows = [
  [
    "Automation",
    "Long-lived definition: owner, org, name, trigger JSON, tarball, entrypoint, timeout, prompt metadata, enabled/deleted state, and polling timestamps.",
  ],
  [
    "AutomationRun",
    "Queue item and execution record: PENDING/RUNNING/terminal status, event payload, sandbox id, bash command id, conversation id, timeout_at, and error detail.",
  ],
  [
    "TarballUpload",
    "Metadata for generated or custom tarballs, with status, storage path, size, and soft-delete state; completed uploads produce oh-internal:// URLs.",
  ],
  [
    "CustomWebhook",
    "Org-scoped custom event source with HMAC secret, signature header, source URL segment, event key expression, and enabled flag.",
  ],
];

const designBoundaries = [
  "The database is the durable queue: creating an AutomationRun in PENDING is enough for the dispatcher to pick it up.",
  "The dispatcher is fire-and-forget: it starts bash and relies on callbacks or watchdog verification instead of waiting for user code to finish.",
  "ExecutionBackend keeps Cloud sandbox lifecycle and local agent-server mode behind the same acquire, env, verify, and cleanup interface.",
  "Tarball bytes are isolated from execution: internal tarballs come from controlled storage; external tarballs are downloaded inside the sandbox to avoid proxying large untrusted files through the service.",
  "Every terminal transition uses optimistic locking so duplicate callbacks, cancellation, and watchdog scans race safely.",
  "Event triggers use HMAC verification first, typed or custom parsing second, and trigger matching last; unmatched events do not create runs.",
];

function FlowCard({ step, index }: { step: FlowStep; index: number }) {
  return (
    <li className="rounded-2xl border border-border bg-surface-card p-4">
      <div className="flex items-center gap-3">
        <span className="flex size-7 shrink-0 items-center justify-center rounded-full border border-border bg-surface-elevated text-xs font-semibold text-content">
          {index + 1}
        </span>
        <h3 className="text-sm font-semibold text-content">{step.label}</h3>
      </div>
      <p className="mt-3 text-sm leading-6 text-content-muted">{step.detail}</p>
    </li>
  );
}

function FlowSection({ title, steps }: { title: string; steps: FlowStep[] }) {
  return (
    <section className="rounded-3xl border border-border bg-surface-card/60 p-5">
      <h2 className="text-base font-semibold text-content">{title}</h2>
      <ol className="mt-4 grid gap-3 md:grid-cols-2">
        {steps.map((step, index) => (
          <FlowCard key={step.label} step={step} index={index} />
        ))}
      </ol>
    </section>
  );
}

function CodePill({ children }: { children: React.ReactNode }) {
  return (
    <code className="rounded-md border border-border bg-surface-elevated px-1.5 py-0.5 font-mono text-[0.8em] text-content">
      {children}
    </code>
  );
}

export default function Architecture() {
  return (
    <article className="mx-auto max-w-6xl pb-12">
      <Link
        to="/"
        className="text-sm text-content-muted transition hover:text-content"
      >
        ← Automations
      </Link>

      <header className="mt-6 rounded-3xl border border-border bg-surface-card p-6 md:p-8">
        <div className="flex flex-wrap items-center gap-3">
          <span className="rounded-full border border-border bg-surface-elevated px-3 py-1 font-mono text-xs uppercase tracking-[0.2em] text-content-muted">
            architecture note
          </span>
          <span className="rounded-full border border-status-success-border bg-status-success-bg px-3 py-1 text-xs text-status-success-text">
            OpenHands Automations
          </span>
        </div>
        <h1 className="mt-5 text-3xl font-semibold tracking-tight text-content md:text-5xl">
          How automations become sandbox runs
        </h1>
        <p className="mt-4 max-w-3xl text-base leading-7 text-content-muted">
          The automation service is a control plane for scheduled and
          event-triggered work. It stores definitions, turns triggers into run
          records, dispatches tarballs into OpenHands execution environments,
          and reconciles completion through callbacks plus watchdog
          verification.
        </p>
      </header>

      <section className="mt-6 rounded-3xl border border-status-success-border bg-status-success-bg p-5">
        <p className="text-sm leading-6 text-content">
          <strong>Short version:</strong> the database is both catalog and
          queue; the scheduler and event router only create{" "}
          <CodePill>PENDING</CodePill> runs; the dispatcher starts the run
          asynchronously; user code reports back through a callback; the
          watchdog repairs missed callbacks by checking the same agent server or
          sandbox that executed the command.
        </p>
      </section>

      <section className="mt-10">
        <h2 className="text-xl font-semibold text-content">System map</h2>
        <div className="mt-4 overflow-hidden rounded-2xl border border-border">
          <table className="w-full border-collapse text-left text-sm">
            <thead className="bg-surface-elevated text-xs uppercase tracking-[0.14em] text-content-muted">
              <tr>
                <th className="px-4 py-3 font-medium">Component</th>
                <th className="px-4 py-3 font-medium">Key files</th>
                <th className="px-4 py-3 font-medium">Responsibility</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border bg-surface-card">
              {componentRows.map((row) => (
                <tr key={row.name} className="align-top">
                  <td className="px-4 py-4 font-medium text-content">
                    {row.name}
                  </td>
                  <td className="px-4 py-4 font-mono text-xs text-content-muted">
                    {row.location}
                  </td>
                  <td className="px-4 py-4 leading-6 text-content-muted">
                    {row.responsibility}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="mt-10">
        <h2 className="text-xl font-semibold text-content">Lifecycle</h2>
        <div className="mt-4 grid gap-5">
          <FlowSection title="1. Define what runs" steps={createFlow} />
          <FlowSection title="2. Decide when it runs" steps={triggerFlow} />
          <FlowSection
            title="3. Dispatch into an execution backend"
            steps={dispatchFlow}
          />
          <FlowSection title="4. Reconcile completion" steps={completionFlow} />
        </div>
      </section>

      <section className="mt-10 grid gap-5 lg:grid-cols-[1.1fr_0.9fr]">
        <div className="rounded-3xl border border-border bg-surface-card p-5">
          <h2 className="text-xl font-semibold text-content">
            Run state machine
          </h2>
          <div className="mt-5 flex flex-wrap items-center gap-2 text-sm text-content-muted">
            <CodePill>PENDING</CodePill>
            <span>→ dispatcher claims</span>
            <CodePill>RUNNING</CodePill>
            <span>→ callback, cancel, or watchdog</span>
            <CodePill>COMPLETED</CodePill>
            <CodePill>FAILED</CodePill>
            <CodePill>CANCELLED</CodePill>
            <CodePill>SKIPPED</CodePill>
          </div>
          <p className="mt-5 text-sm leading-6 text-content-muted">
            <CodePill>SKIPPED</CodePill> is reserved for dispatch-time capacity
            limits, such as organization sandbox concurrency. Permanent tarball
            errors can also disable the parent automation so it stops generating
            doomed runs.
          </p>
        </div>

        <div className="rounded-3xl border border-border bg-surface-card p-5">
          <h2 className="text-xl font-semibold text-content">
            Execution modes
          </h2>
          <dl className="mt-4 space-y-4 text-sm leading-6">
            <div>
              <dt className="font-medium text-content">Cloud sandbox</dt>
              <dd className="text-content-muted">
                Creates a fresh OpenHands sandbox, waits for the exposed
                AGENT_SERVER URL, injects a minted per-user API key, and deletes
                the sandbox after completion unless keep_alive is set.
              </dd>
            </div>
            <div>
              <dt className="font-medium text-content">Local agent server</dt>
              <dd className="text-content-muted">
                Uses a configured persistent agent server, isolates each run
                under a per-run workspace directory, and authenticates callbacks
                with the local automation API key when available.
              </dd>
            </div>
          </dl>
        </div>
      </section>

      <section className="mt-10 rounded-3xl border border-border bg-surface-card p-5">
        <h2 className="text-xl font-semibold text-content">Core data model</h2>
        <div className="mt-4 grid gap-3 md:grid-cols-2">
          {dataModelRows.map(([name, detail]) => (
            <div key={name} className="rounded-2xl border border-border p-4">
              <h3 className="font-mono text-sm text-content">{name}</h3>
              <p className="mt-2 text-sm leading-6 text-content-muted">
                {detail}
              </p>
            </div>
          ))}
        </div>
      </section>

      <section className="mt-10 rounded-3xl border border-border bg-surface-card p-5">
        <h2 className="text-xl font-semibold text-content">
          Environment injected into user code
        </h2>
        <div className="mt-4 grid gap-2 md:grid-cols-2">
          {envVars.map(([name, detail]) => (
            <div
              key={name}
              className="rounded-2xl border border-border bg-surface-elevated/40 p-4"
            >
              <CodePill>{name}</CodePill>
              <p className="mt-2 text-sm leading-6 text-content-muted">
                {detail}
              </p>
            </div>
          ))}
        </div>
      </section>

      <section className="mt-10 rounded-3xl border border-border bg-surface-card p-5">
        <h2 className="text-xl font-semibold text-content">
          Design boundaries
        </h2>
        <ul className="mt-4 grid gap-3 md:grid-cols-2">
          {designBoundaries.map((boundary) => (
            <li
              key={boundary}
              className="rounded-2xl border border-border bg-surface-elevated/40 p-4 text-sm leading-6 text-content-muted"
            >
              {boundary}
            </li>
          ))}
        </ul>
      </section>
    </article>
  );
}
