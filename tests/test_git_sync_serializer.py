"""Tests for the git sync file-tree (de)serializer."""

import io
import tarfile
import uuid

import pytest
import yaml

from openhands.automation.git_sync.serializer import (
    GitSyncDecryptionError,
    GitSyncMetadataError,
    canonical_tarball_bytes,
    compute_content_hash,
    compute_slug,
    decrypt_file_tree,
    deserialize_automation,
    encrypt_file_tree,
    rebuild_tarball,
    serialize_automation,
)
from openhands.automation.models import Automation


def _make_tarball(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def _make_tarball_with_modes(files: dict[str, tuple[bytes, int]]) -> bytes:
    """Like `_make_tarball`, but with an explicit mode per member."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, (content, mode) in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            info.mode = mode
            tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def _make_automation(**overrides) -> Automation:
    defaults = dict(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        name="My Automation",
        model="claude",
        trigger={"type": "cron", "schedule": "0 9 * * 1"},
        tarball_path=f"oh-internal://uploads/{uuid.uuid4()}",
        setup_script_path="setup.sh",
        entrypoint="python main.py",
        timeout=300,
        keep_alive=None,
        enabled=True,
        prompt=None,
        preset_metadata=None,
    )
    defaults.update(overrides)
    return Automation(**defaults)


class TestComputeSlug:
    def test_basic_slug(self):
        automation_id = uuid.uuid4()
        assert compute_slug("My Automation!!", automation_id, set()) == "my-automation"

    def test_collision_appends_id_suffix(self):
        automation_id = uuid.uuid4()
        slug = compute_slug("My Automation", automation_id, taken={"my-automation"})
        assert slug == f"my-automation-{automation_id.hex[:8]}"

    def test_empty_name_falls_back(self):
        automation_id = uuid.uuid4()
        assert compute_slug("!!!", automation_id, set()) == "automation"

    def test_truncates_long_names(self):
        automation_id = uuid.uuid4()
        slug = compute_slug("x" * 200, automation_id, set())
        assert len(slug) <= 80


class TestSerializeAutomation:
    def test_internal_tarball_extracted_under_tarball_dir(self):
        automation = _make_automation()
        tarball_bytes = _make_tarball(
            {"main.py": b"print(1)", "setup.sh": b"#!/bin/sh\n"}
        )

        files = serialize_automation(automation, tarball_bytes)

        assert set(files) == {"automation.yaml", "tarball/main.py", "tarball/setup.sh"}
        assert files["tarball/main.py"] == b"print(1)"

    def test_automation_yaml_fields(self):
        automation = _make_automation(prompt="do the thing")
        files = serialize_automation(automation, _make_tarball({"main.py": b"x"}))

        import yaml

        fields = yaml.safe_load(files["automation.yaml"])
        assert fields["name"] == "My Automation"
        assert fields["prompt"] == "do the thing"
        assert fields["tarball_source"] == {"type": "internal", "url": None}

    def test_external_url_skips_tarball_dir(self):
        automation = _make_automation(tarball_path="https://example.com/x.tar.gz")
        files = serialize_automation(automation, None)

        assert set(files) == {"automation.yaml"}
        import yaml

        fields = yaml.safe_load(files["automation.yaml"])
        assert fields["tarball_source"] == {
            "type": "external",
            "url": "https://example.com/x.tar.gz",
        }

    def test_path_traversal_members_are_rejected(self):
        automation = _make_automation()
        tarball_bytes = _make_tarball(
            {
                "main.py": b"print(1)",
                "../../../etc/passwd": b"pwned",
                "../../.git/hooks/post-checkout": b"#!/bin/sh\nrm -rf /\n",
            }
        )

        files = serialize_automation(automation, tarball_bytes)

        assert set(files) == {"automation.yaml", "tarball/main.py"}
        assert not any(".." in name for name in files)

    def test_absolute_path_member_is_rewritten_relative(self):
        automation = _make_automation()
        tarball_bytes = _make_tarball({"/etc/passwd": b"pwned"})

        files = serialize_automation(automation, tarball_bytes)

        assert set(files) == {"automation.yaml", "tarball/etc/passwd"}


class TestDeserializeAutomation:
    def test_roundtrip(self):
        automation = _make_automation()
        tarball_bytes = _make_tarball({"main.py": b"print(1)"})
        files = serialize_automation(automation, tarball_bytes)

        result = deserialize_automation(files)

        assert result is not None
        assert result.fields["name"] == "My Automation"
        assert result.fields["entrypoint"] == "python main.py"
        assert result.tarball_bytes is not None
        with tarfile.open(fileobj=io.BytesIO(result.tarball_bytes)) as tar:
            main_py = tar.extractfile("main.py")
            assert main_py is not None
            assert main_py.read() == b"print(1)"

    def test_no_metadata_file_returns_none(self):
        assert deserialize_automation({"tarball/foo.txt": b"x"}) is None

    def test_non_mapping_yaml_returns_none_instead_of_crashing(self):
        # Valid YAML that isn't a dict -- e.g. leftover ciphertext read as
        # plaintext after encryption was turned off. Must not hand callers
        # a DeserializedAutomation whose .fields isn't a dict.
        assert deserialize_automation({"automation.yaml": b"just a string"}) is None
        assert deserialize_automation({"automation.yaml": b"- a\n- list\n"}) is None

    def test_external_automation_has_no_tarball_bytes(self):
        automation = _make_automation(tarball_path="https://example.com/x.tar.gz")
        files = serialize_automation(automation, None)

        result = deserialize_automation(files)

        assert result is not None
        assert result.tarball_bytes is None


class TestComputeContentHash:
    def test_stable_for_identical_input(self):
        files = {"automation.yaml": b"a: 1", "tarball/main.py": b"print(1)"}
        assert compute_content_hash(files) == compute_content_hash(dict(files))

    def test_changes_when_content_changes(self):
        files_a = {"automation.yaml": b"a: 1"}
        files_b = {"automation.yaml": b"a: 2"}
        assert compute_content_hash(files_a) != compute_content_hash(files_b)

    def test_independent_of_dict_insertion_order(self):
        files_a = {"a": b"1", "b": b"2"}
        files_b = {"b": b"2", "a": b"1"}
        assert compute_content_hash(files_a) == compute_content_hash(files_b)


class TestEncryptDecryptFileTree:
    def test_round_trip(self):
        files = {"automation.yaml": b"name: x", "tarball/main.py": b"print(1)"}
        encrypted = encrypt_file_tree(files, "the-key")
        assert decrypt_file_tree(encrypted, "the-key") == files

    def test_encrypted_content_is_opaque(self):
        files = {"automation.yaml": b"prompt: a very secret prompt"}
        encrypted = encrypt_file_tree(files, "the-key")
        assert b"secret" not in encrypted["automation.yaml"]

    def test_legacy_plaintext_passes_through_unchanged(self):
        files = {"automation.yaml": b"name: x"}
        assert decrypt_file_tree(files, "the-key") == files

    def test_wrong_key_raises(self):
        encrypted = encrypt_file_tree({"automation.yaml": b"name: x"}, "key-a")
        with pytest.raises(GitSyncDecryptionError):
            decrypt_file_tree(encrypted, "key-b")

    def test_round_trip_survives_arbitrary_binary_content(self):
        # Tarball members can be non-UTF8 binaries (compiled artifacts,
        # images, ...), not just text -- prove the base64 wrapping handles
        # every byte value, not just printable ASCII.
        binary = bytes(range(256)) * 4
        files = {"tarball/blob.bin": binary}
        encrypted = encrypt_file_tree(files, "the-key")
        assert decrypt_file_tree(encrypted, "the-key") == files

    def test_empty_file_round_trips(self):
        files = {"tarball/empty.txt": b""}
        encrypted = encrypt_file_tree(files, "the-key")
        assert decrypt_file_tree(encrypted, "the-key") == files


class TestRebuildTarball:
    def test_rebuild_matches_original_members(self):
        tarball_bytes = rebuild_tarball(
            {"main.py": b"print(1)", "setup.sh": b"echo hi"}
        )
        with tarfile.open(fileobj=io.BytesIO(tarball_bytes)) as tar:
            assert sorted(tar.getnames()) == ["main.py", "setup.sh"]
            main_py = tar.extractfile("main.py")
            assert main_py is not None
            assert main_py.read() == b"print(1)"

    def test_setup_scripts_get_executable_mode(self):
        tarball_bytes = rebuild_tarball({"setup.sh": b"echo hi"})
        with tarfile.open(fileobj=io.BytesIO(tarball_bytes)) as tar:
            assert tar.getmember("setup.sh").mode == 0o755


class TestExecutableModeRoundTrip:
    """Regression: modes were dropped for anything not named `*.sh`.

    The only mode source was `rebuild_tarball`'s filename heuristic, so an
    executable helper without that suffix (`bin/run`, a `main.py` invoked as
    `./main.py`) came back 0644 and every subsequent run failed with
    "Permission denied", with nothing in the git diff to explain it.
    """

    def test_executable_members_are_recorded_in_metadata(self):
        automation = _make_automation()
        files = serialize_automation(
            automation,
            _make_tarball_with_modes(
                {"bin/run": (b"#!/bin/sh\n", 0o755), "lib.py": (b"x = 1", 0o644)}
            ),
        )
        fields = yaml.safe_load(files["automation.yaml"].decode())
        assert fields["tarball_executables"] == ["bin/run"]

    def test_executable_bit_survives_a_round_trip(self):
        automation = _make_automation()
        files = serialize_automation(
            automation,
            _make_tarball_with_modes(
                {"bin/run": (b"#!/bin/sh\n", 0o755), "lib.py": (b"x = 1", 0o644)}
            ),
        )

        deserialized = deserialize_automation(files)
        assert deserialized is not None
        assert deserialized.tarball_bytes is not None
        with tarfile.open(fileobj=io.BytesIO(deserialized.tarball_bytes)) as tar:
            assert tar.getmember("bin/run").mode == 0o755
            assert tar.getmember("lib.py").mode == 0o644

    def test_no_executables_key_when_nothing_is_executable(self):
        automation = _make_automation()
        files = serialize_automation(
            automation, _make_tarball_with_modes({"lib.py": (b"x = 1", 0o644)})
        )
        fields = yaml.safe_load(files["automation.yaml"].decode())
        assert "tarball_executables" not in fields

    def test_missing_metadata_falls_back_to_the_sh_heuristic(self):
        """A repo synced before `tarball_executables` existed keeps its modes."""
        tarball_bytes = rebuild_tarball({"setup.sh": b"echo hi", "run": b"echo hi"})
        with tarfile.open(fileobj=io.BytesIO(tarball_bytes)) as tar:
            assert tar.getmember("setup.sh").mode == 0o755
            assert tar.getmember("run").mode == 0o644

    def test_empty_metadata_list_is_trusted_over_the_heuristic(self):
        tarball_bytes = rebuild_tarball({"setup.sh": b"echo hi"}, [])
        with tarfile.open(fileobj=io.BytesIO(tarball_bytes)) as tar:
            assert tar.getmember("setup.sh").mode == 0o644

    def test_malformed_metadata_list_is_ignored(self):
        automation = _make_automation()
        files = serialize_automation(automation, _make_tarball({"run": b"x"}))
        files["automation.yaml"] = yaml.safe_dump(
            {
                **yaml.safe_load(files["automation.yaml"].decode()),
                "tarball_executables": "not-a-list",
            }
        ).encode()

        deserialized = deserialize_automation(files)
        assert deserialized is not None
        assert deserialized.tarball_bytes is not None


class TestDotSlashMemberNames:
    """Regression: `tar -czf archive.tgz .` produces "./main.py" members.

    `tarfile.data_filter` leaves the prefix in place, so the same file was
    committed as `tarball/./main.py` and never matched the plain `main.py`
    an unprefixed tarball produces for identical content.
    """

    def test_leading_dot_slash_is_stripped_on_serialize(self):
        automation = _make_automation()
        files = serialize_automation(
            automation, _make_tarball({"./main.py": b"print(1)", "./sub/a.txt": b"a"})
        )
        assert "tarball/main.py" in files
        assert "tarball/sub/a.txt" in files
        assert not any("/./" in name for name in files)

    def test_dot_slash_and_plain_tarballs_hash_the_same(self):
        automation = _make_automation()
        dotted = serialize_automation(automation, _make_tarball({"./main.py": b"x"}))
        plain = serialize_automation(automation, _make_tarball({"main.py": b"x"}))
        assert compute_content_hash(dotted) == compute_content_hash(plain)


class TestMalformedMetadata:
    def test_invalid_yaml_raises_a_valueerror(self):
        """Regression: yaml.YAMLError is not a ValueError, so it escaped the
        import's per-directory skip and aborted the whole sync cycle -- every
        other automation stopped syncing until that one file was fixed."""
        with pytest.raises(GitSyncMetadataError) as excinfo:
            deserialize_automation({"automation.yaml": b"name: [unclosed\n"})
        assert isinstance(excinfo.value, ValueError)


class TestCanonicalTarballBytes:
    def test_repacking_is_stable_across_framings(self):
        """Identical content packed differently canonicalizes identically."""
        first = _make_tarball_with_modes({"a.py": (b"x", 0o644), "b.py": (b"y", 0o755)})
        second = _make_tarball_with_modes(
            {"b.py": (b"y", 0o755), "a.py": (b"x", 0o644)}
        )
        assert canonical_tarball_bytes(first) == canonical_tarball_bytes(second)
