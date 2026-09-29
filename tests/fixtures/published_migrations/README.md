These are byte-for-byte copies of migrations already published on `main` at
`baae0a8032470ceba014814210bd4b5c2e616d04` (September 14, 2026):

- [023_org_scoped_git_sync.py](https://github.com/OpenHands/automation/blob/baae0a8032470ceba014814210bd4b5c2e616d04/migrations/versions/023_org_scoped_git_sync.py)
- [024_add_run_sandbox_cleanup_due_at.py](https://github.com/OpenHands/automation/blob/baae0a8032470ceba014814210bd4b5c2e616d04/migrations/versions/024_add_run_sandbox_cleanup_due_at.py)

`test_migration_history.py` combines these files with the branch's migrations in
a temporary directory and checks the Alembic graph. This catches reused IDs
before rebasing, without Git remotes, network access, or a deployed database.
The `.txt` suffix keeps historical snapshots out of automatic Python rewrites.
