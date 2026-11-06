"""New migrations must coexist with migration IDs already published on main.

The fixture snapshot makes the integration regression reproducible offline,
including before a stale feature branch has been rebased onto that history.
"""

import shutil
import warnings
from pathlib import Path

from alembic.script import ScriptDirectory


def test_migrations_do_not_reuse_published_revision_ids(tmp_path: Path):
    repository = Path(__file__).resolve().parents[1]
    combined = tmp_path / "migrations"
    shutil.copytree(repository / "migrations", combined)
    published = Path(__file__).parent / "fixtures" / "published_migrations"
    for snapshot in published.glob("*.py.txt"):
        destination = combined / "versions" / snapshot.name.removesuffix(".txt")
        # After a rebase these migrations are already present in the branch.
        if not destination.exists():
            shutil.copyfile(snapshot, destination)

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        heads = ScriptDirectory(str(combined)).get_heads()

    duplicate_revisions = [
        str(warning.message)
        for warning in captured
        if "is present more than once" in str(warning.message)
    ]
    assert not duplicate_revisions, (
        "New migrations collide with published main history: "
        + "; ".join(duplicate_revisions)
        + ". Assign unused revisions and update the down_revision chain."
    )
    assert len(heads) == 1, f"Expected one upgrade head after integration, got {heads}"
