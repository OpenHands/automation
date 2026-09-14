# Reproductions for #438

Tests only; the author should fix production code to make the regressions pass.

Run:

```bash
uv run pytest tests/test_git_sync_state.py tests/test_migration_history.py -q
```

Expected on `aa6a0dea23ab411c2df910bebc7825e371c50fcc`: **4 failed, 7 passed**.

| Regression | Expected behavior | Current failure |
| --- | --- | --- |
| Git `state: INACTIVE`, no legacy `enabled` | Persist INACTIVE and exclude it from scheduling | Persists ACTIVE/true and is selected |
| Explicit conflicting state/enabled, two cases | Reject the contradiction, as the API does | Silently accepts it |
| Integration with published migrations | Unique revisions and one upgrade head | Reuses revision 023 already published on main |

Seven passing controls preserve legacy enabled-only inputs (including empty
YAML values) and consistent ACTIVE/INACTIVE/DRAFT pairs. The Git tests use the
real YAML decoder, importer, SQLite database, and scheduler query. They do not
download or execute the placeholder tarball.

The migration fixtures pin published history at main commit
`baae0a8032470ceba014814210bd4b5c2e616d04`. The test is offline and also detects
#439's reuse of 024 when run on that stacked branch. It does not merge source
changes from main or test a production database.

The #438-only ACTIVE -> DRAFT -> ACTIVE history case from the review is not
included: #439 intentionally rejects that public transition. The separate
question of draft-test failures counting toward production automatic disabling
needs a product decision before a test prescribes its desired behavior.
