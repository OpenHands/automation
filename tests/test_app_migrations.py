"""Tests for application migration discovery."""

import pytest

from openhands.automation.app import _resolve_migrations_path


def test_resolve_migrations_path_prefers_packaged_migrations(tmp_path):
    """Packaged migrations are used when bundled inside the package."""
    package_dir = tmp_path / "openhands" / "automation"
    packaged_migrations = package_dir / "migrations"
    source_migrations = tmp_path / "migrations"
    packaged_migrations.mkdir(parents=True)
    source_migrations.mkdir()

    assert _resolve_migrations_path(package_dir) == packaged_migrations


def test_resolve_migrations_path_finds_source_checkout_repo_root(tmp_path):
    """Source checkouts use the repo-root migrations directory."""
    package_dir = tmp_path / "openhands" / "automation"
    package_dir.mkdir(parents=True)
    repo_root_migrations = tmp_path / "migrations"
    repo_root_migrations.mkdir()

    assert _resolve_migrations_path(package_dir) == repo_root_migrations


def test_resolve_migrations_path_keeps_legacy_parent_fallback(tmp_path):
    """The old parent fallback is retained for non-standard layouts."""
    package_dir = tmp_path / "openhands" / "automation"
    package_dir.mkdir(parents=True)
    legacy_migrations = tmp_path / "openhands" / "migrations"
    legacy_migrations.mkdir()

    assert _resolve_migrations_path(package_dir) == legacy_migrations


def test_resolve_migrations_path_error_lists_checked_paths(tmp_path):
    """Missing migrations raise an error with every checked location."""
    package_dir = tmp_path / "openhands" / "automation"
    package_dir.mkdir(parents=True)

    with pytest.raises(RuntimeError) as exc_info:
        _resolve_migrations_path(package_dir)

    message = str(exc_info.value)
    assert str(package_dir / "migrations") in message
    assert str(tmp_path / "migrations") in message
    assert str(tmp_path / "openhands" / "migrations") in message
