# Subject-release migration upgrade evidence

The installed Git checkout moved from `c756d24c91bc12da8bb329be815824bbc77dafe5` to `a05e89370be94beb72b34fedaf129da678c294d5` on `vasco/external-conversations`. Both use Alembic022, but only the latter adds `subject_released_at`. Their1.9.1/1.10.0 distribution metadata must not be confused with the release tags, which contain no022 migration.

`tests/test_subject_release_migration.py` builds the old022 schema from the exact historical DDL, then exercises normal Alembic upgrade and current ORM queries. It also checks modern022 and fresh databases, preserves an existing completed run and existing release timestamp, and repeats startup upgrade plus downgrade-to022/re-upgrade.

| Source | Result |
| --- | --- |
| Main645f204 without the new migration, same regression tests | Legacy022 fails: `sqlite3.OperationalError: no such column: automation_runs.subject_released_at`; other2 cases pass. |
| Same source with forward migration023 | All3 cases pass. |

Ruff, pycodestyle and Pyright checks pass. Tests use disposable SQLite files only; no VM, running service, production database or Docker container was modified. PostgreSQL execution was not exercised here; the repair uses the same SQLAlchemy/Alembic column and dialect-specific index declarations as current022.

Follow-up before integrating the factory profile draft: Automation453 currently uses revision023 for agent-profile selection. Rebase it after this main-based repair lands, rename its profile migration to024, and set that migration's `down_revision` to023. Do not combine two different revision023 files. No factory branch or running factory was changed by this PR.
