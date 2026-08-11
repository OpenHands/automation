"""Serialize/deserialize automations to/from a git-friendly file tree.

Each automation is stored in the sync repo under `{slug}/` as:

    automation.yaml     Editable metadata (name, trigger, model, prompt, ...)
    tarball/**           Full contents of the automation's code tarball
"""

import base64
import hashlib
import io
import logging
import re
import tarfile
import uuid
from dataclasses import dataclass
from typing import Any

import yaml
from pydantic import SecretStr

from openhands.automation.models import Automation
from openhands.automation.utils.tarball_validation import is_internal_url
from openhands.sdk.utils.cipher import FERNET_TOKEN_PREFIX, Cipher


logger = logging.getLogger("automation.git_sync")

# Filename for per-automation metadata within its slug directory.
METADATA_FILENAME = "automation.yaml"

# Subdirectory holding the extracted tarball contents.
TARBALL_DIRNAME = "tarball"

_TARBALL_PREFIX = f"{TARBALL_DIRNAME}/"

_SLUG_INVALID_RE = re.compile(r"[^a-z0-9]+")
_MAX_SLUG_BASE_LEN = 80


def compute_slug(name: str, automation_id: uuid.UUID, taken: set[str]) -> str:
    """Compute a stable, filesystem-safe directory name for an automation.

    Derived from the automation's name (lowercased, non-alphanumeric runs
    collapsed to a single hyphen). Falls back to appending a short id suffix
    when the base slug collides with another automation's slug (`taken`),
    so directory names stay unique within the sync repo.
    """
    base = _SLUG_INVALID_RE.sub("-", name.strip().lower()).strip("-")
    base = base[:_MAX_SLUG_BASE_LEN].strip("-") or "automation"
    if base not in taken:
        return base
    return f"{base}-{automation_id.hex[:8]}"


@dataclass
class DeserializedAutomation:
    """Automation fields + tarball bytes parsed back from a git directory."""

    fields: dict[str, Any]
    tarball_bytes: bytes | None


def _safe_tar_member(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
    """Sanitize a tar member against path traversal (e.g. "../../etc/passwd").

    Tarballs are user-provided content (uploaded via /v1/uploads with no
    member-name validation). `tarfile.data_filter` rejects members that
    would resolve outside the destination and rewrites absolute paths to
    relative ones. Returns `None` if the member should be skipped entirely.
    """
    try:
        return tarfile.data_filter(member, "")
    except tarfile.FilterError as e:
        logger.warning("Skipping unsafe tarball member %r: %s", member.name, e)
        return None


def _automation_yaml_fields(
    automation: Automation, *, tarball_is_internal: bool
) -> dict[str, Any]:
    return {
        "name": automation.name,
        "model": automation.model,
        "trigger": automation.trigger,
        "setup_script_path": automation.setup_script_path,
        "entrypoint": automation.entrypoint,
        "timeout": automation.timeout,
        "keep_alive": automation.keep_alive,
        "enabled": automation.enabled,
        "prompt": automation.prompt,
        "preset_metadata": automation.preset_metadata,
        "tarball_source": {
            "type": "internal" if tarball_is_internal else "external",
            "url": None if tarball_is_internal else automation.tarball_path,
        },
    }


def serialize_automation(
    automation: Automation, tarball_bytes: bytes | None
) -> dict[str, bytes]:
    """Serialize an automation to a `{relative_path: content}` file tree.

    `tarball_bytes` is already fetched by the caller (this does no I/O);
    pass `None` when there's no tarball to extract (external URL, or
    unavailable).
    """
    tarball_is_internal = is_internal_url(automation.tarball_path)
    fields = _automation_yaml_fields(
        automation, tarball_is_internal=tarball_is_internal
    )

    files: dict[str, bytes] = {
        METADATA_FILENAME: yaml.safe_dump(
            fields, sort_keys=True, default_flow_style=False
        ).encode("utf-8"),
    }

    if tarball_is_internal and tarball_bytes is not None:
        with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                safe_member = _safe_tar_member(member)
                if safe_member is None:
                    continue
                extracted = tar.extractfile(safe_member)
                if extracted is None:
                    continue
                files[f"{_TARBALL_PREFIX}{safe_member.name}"] = extracted.read()

    return files


def compute_content_hash(files: dict[str, bytes]) -> str:
    """Stable SHA-256 hash over a serialized automation's file tree.

    Takes the same `{relative_path: content}` shape `serialize_automation`
    returns. Used to detect no-op sync cycles without re-diffing the whole
    tree on disk.
    """
    hasher = hashlib.sha256()
    for name in sorted(files):
        hasher.update(name.encode("utf-8"))
        hasher.update(files[name])
    return hasher.hexdigest()


def rebuild_tarball(tarball_files: dict[str, bytes]) -> bytes:
    """Rebuild a tar.gz from `{relative_path: content}` files.

    Member order and mtimes are fixed for determinism; `compute_content_hash`
    hashes the extracted files directly, not these compressed bytes.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name in sorted(tarball_files):
            content_bytes = tarball_files[name]
            info = tarfile.TarInfo(name=name)
            info.size = len(content_bytes)
            info.mode = 0o755 if name.endswith(".sh") else 0o644
            info.mtime = 0
            tar.addfile(info, io.BytesIO(content_bytes))
    return buffer.getvalue()


def deserialize_automation(
    dir_files: dict[str, bytes],
) -> DeserializedAutomation | None:
    """Parse a git directory's files back into automation fields + tarball.

    `dir_files` maps paths relative to the automation's slug directory (as
    produced by `serialize_automation`) to their content. Returns `None` if
    no `automation.yaml` is present — not a valid automation directory, e.g.
    a stray file dropped next to the sync path.
    """
    raw_metadata = dir_files.get(METADATA_FILENAME)
    if raw_metadata is None:
        return None

    fields = yaml.safe_load(raw_metadata.decode("utf-8")) or {}
    if not isinstance(fields, dict):
        # Valid YAML but not a mapping -- e.g. leftover ciphertext read as
        # plaintext after encryption was turned off, or any other malformed
        # commit. Same "not a valid automation directory" bucket as a
        # missing file, so callers skip it the same way.
        logger.warning(
            "automation.yaml did not parse to a mapping (got %s); skipping",
            type(fields).__name__,
        )
        return None

    tarball_files = {
        path.removeprefix(_TARBALL_PREFIX): content
        for path, content in dir_files.items()
        if path.startswith(_TARBALL_PREFIX)
    }
    tarball_bytes = rebuild_tarball(tarball_files) if tarball_files else None

    return DeserializedAutomation(fields=fields, tarball_bytes=tarball_bytes)


class GitSyncDecryptionError(ValueError):
    """A synced file couldn't be decrypted with the configured key.

    Subclasses ValueError so it's caught by the same "skip this invalid
    automation directory" handling as any other malformed git content.
    """


def encrypt_file_tree(files: dict[str, bytes], key: str) -> dict[str, bytes]:
    """Encrypt every file's bytes with `key` before they're committed.

    Uses the SDK's Fernet-based Cipher (same primitive as the KV store's
    at-rest encryption). Cipher operates on text, so raw bytes are
    base64-wrapped first; the resulting token is itself ASCII-safe to write
    to disk and commit as-is.
    """
    cipher = Cipher(key)
    encrypted: dict[str, bytes] = {}
    for name, content in files.items():
        token = cipher.encrypt(SecretStr(base64.b64encode(content).decode()))
        assert token is not None  # SecretStr is never None, so neither is this
        encrypted[name] = token.encode()
    return encrypted


def decrypt_file_tree(files: dict[str, bytes], key: str) -> dict[str, bytes]:
    """Decrypt files previously written by `encrypt_file_tree`.

    Files that don't start with the Fernet token prefix pass through
    unchanged — a repo committed before encryption was turned on must stay
    readable. Raises `GitSyncDecryptionError` if a file that IS a Fernet
    token can't be decrypted with `key` (wrong/rotated key, corruption).
    """
    cipher = Cipher(key)
    decrypted: dict[str, bytes] = {}
    for name, content in files.items():
        if not content.startswith(FERNET_TOKEN_PREFIX.encode()):
            decrypted[name] = content
            continue
        secret = cipher.decrypt(content.decode(errors="replace"))
        if secret is None:
            raise GitSyncDecryptionError(
                f"could not decrypt {name!r} with the configured "
                "AUTOMATION_GIT_SYNC_ENCRYPTION_KEY"
            )
        decrypted[name] = base64.b64decode(secret.get_secret_value())
    return decrypted
