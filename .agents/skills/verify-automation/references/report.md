# Report contract

Every verification report, whether for one PR, a mapping session or a
maintenance pass, has the same shape so a reviewer can scan it in a minute.

## Header

- What was verified (PR number, family IDs, or "full pass") and the outcome
  (`clean`, `changed`, `blocked/partial`, or pass/fail for a PR).
- Checkout SHA(s), the Agent Server release each run paired with (`launch`
  output, or an attached server), and the run directories.
- Service and SDK versions from `/server_info` and the model(s) used, with
  approximate spend (each preset run's `cost` in the runs list).
- Date (UTC) and what was deliberately not covered.

## Results

Generate the table with `control-automation evidence report --out report.md
--print-markdown` (merge several runs with `--runs A B`). Rows are ordered
fail, blocked, not-run, pass; each row has the sub-feature ID, entry point,
expected and actual result and the evidence paths.
Each run retains its own row for a feature, with the run ID and checkout SHA;
merging baseline and fix runs preserves every attempt and its evidence paths.
Within one run, a newer result replaces its earlier row.

Below the table:

1. **Product bugs**: a bug counts as confirmed only when it reproduces twice,
   each time on a fresh run (`map run --only <ID> --fresh --repeat 2`). One
   block each, with the failing sub-feature IDs, the
   exact command sequence that reproduces it on a fresh `launch --new`, the
   observed vs expected result, the evidence paths, and the issue you filed or
   found (or "not filed: <reason>").
2. **Harness gaps fixed**: the verbs or flags added and the recipe each
   unblocked.
3. **Map drift fixed**: the entries changed and the PR or source line that
   justifies each change.
4. **Blocked**: each prerequisite (a Slack app, a cloud deployment,
   PostgreSQL) with the route attempted.

## Rules

- `pass` needs evidence a reviewer can open: a saved exchange, a run record,
  a conversation's events, a sink delivery or a transcript. A status line copied into the
  report is not evidence.
- Do not report a skipped entry point as verified through a different one: a
  REST pass does not prove what a dispatched run receives or calls back.
- Evidence is not automatically public. Read every file you attach: the CLI
  redacts the run's keys, `$DEEPSEEK_API_KEY` and encrypted values, but a
  recipe that wrote a secret into a workspace file can still leak it.
