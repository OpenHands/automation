"""At-rest encryption for the secrets held in the git-sync config override.

`PUT /v1/git-sync/config` accepts a git access token and a repo encryption
key. Both used to be persisted verbatim in `automation_service_metadata`,
so the token sat in cleartext in every DB dump and backup -- and in local
mode, in a SQLite file on disk -- while client.py advertised that it is
"never persisted to disk or logged". They are wrapped here before storage.

The wrapping key is `AUTOMATION_KV_SECRET` when the deployment sets one, so
there's one service secret to manage rather than two. Otherwise one is
generated on first use and kept in a 0600 file under the workspace: losing
that file means re-entering the token in the UI, which is a much better
failure mode than storing it in the clear. The file deliberately does not
live in the git checkout, which would risk committing it to the synced repo.
"""

import logging
import os
import secrets
from pathlib import Path
from typing import Any, Final

from pydantic import SecretStr

from openhands.automation.config import get_config
from openhands.sdk.utils.cipher import FERNET_TOKEN_PREFIX, Cipher


logger = logging.getLogger("automation.git_sync")

SECRET_KEY_FILENAME: Final = ".git-sync-secret-key"

# GitSyncSettings fields whose values are secrets rather than configuration.
SECRET_OVERRIDE_FIELDS: Final = ("git_sync_token", "git_sync_encryption_key")


class GitSyncSecretStoreError(Exception):
    """No wrapping key is available, so a secret can't be stored safely.

    Surfaced to the caller rather than silently degrading to plaintext --
    the whole point of this module is that the token never lands in the DB
    in the clear.
    """


def _key_file_path() -> Path:
    config = get_config()
    # Deliberately the workspace root, not `git_sync_local_workdir`: that one
    # is a git checkout, and a key file there could be committed to the very
    # repo it protects.
    return Path(config.service.workspace_base) / SECRET_KEY_FILENAME


def _load_or_create_key() -> str:
    kv_secret = get_config().kv.kv_secret
    if kv_secret:
        return kv_secret

    path = _key_file_path()
    try:
        existing = path.read_text().strip()
        if existing:
            return existing
    except FileNotFoundError:
        pass
    except OSError as e:
        raise GitSyncSecretStoreError(
            f"could not read the git-sync secret key at {path}: {e}"
        ) from e

    key = secrets.token_urlsafe(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # O_EXCL so two workers racing to create it don't clobber each
        # other's key and make the loser's stored secrets undecryptable.
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return path.read_text().strip()
    except OSError as e:
        raise GitSyncSecretStoreError(
            f"could not create the git-sync secret key at {path}: {e}. Set "
            "AUTOMATION_KV_SECRET, or make that directory writable, so the "
            "token can be encrypted at rest."
        ) from e
    with os.fdopen(fd, "w") as handle:
        handle.write(key)
    logger.info("Generated a git-sync secret-wrapping key at %s", path)
    return key


def encrypt_secret_fields(overrides: dict[str, Any]) -> dict[str, Any]:
    """Return `overrides` with the secret fields wrapped for storage."""
    if not any(field in overrides for field in SECRET_OVERRIDE_FIELDS):
        return overrides

    cipher = Cipher(_load_or_create_key())
    wrapped = dict(overrides)
    for field in SECRET_OVERRIDE_FIELDS:
        value = wrapped.get(field)
        if not isinstance(value, str) or not value:
            continue
        if value.startswith(FERNET_TOKEN_PREFIX):
            # Already wrapped -- re-wrapping on every partial update would
            # nest tokens until nothing could read the value back out.
            continue
        token = cipher.encrypt(SecretStr(value))
        assert token is not None  # SecretStr is never None, so neither is this
        wrapped[field] = token
    return wrapped


def decrypt_secret_fields(overrides: dict[str, Any]) -> dict[str, Any]:
    """Return `overrides` with the secret fields unwrapped for use.

    Values that aren't Fernet tokens pass through unchanged: overrides
    written before this wrapping existed are plaintext and must stay
    readable. A value that is a token but won't decrypt (the key file was
    lost or rotated) is dropped with a warning rather than handed on as
    garbage -- that field then falls back to its environment default.
    """
    if not any(field in overrides for field in SECRET_OVERRIDE_FIELDS):
        return overrides

    unwrapped = dict(overrides)
    cipher: Cipher | None = None
    for field in SECRET_OVERRIDE_FIELDS:
        value = unwrapped.get(field)
        if not isinstance(value, str) or not value.startswith(FERNET_TOKEN_PREFIX):
            continue
        if cipher is None:
            try:
                cipher = Cipher(_load_or_create_key())
            except GitSyncSecretStoreError:
                # Reading config must not fail just because the key is
                # unreachable; drop the secret fields and carry on with the
                # environment defaults, same as an undecryptable value.
                logger.exception(
                    "Could not load the git-sync secret key; ignoring the "
                    "stored secrets for now"
                )
                for secret_field in SECRET_OVERRIDE_FIELDS:
                    unwrapped.pop(secret_field, None)
                return unwrapped
        secret = cipher.decrypt(value)
        if secret is None:
            logger.warning(
                "Could not decrypt the stored git-sync %s; ignoring it and "
                "falling back to the environment default. Re-enter it on the "
                "Git Sync page to restore it.",
                field,
            )
            unwrapped.pop(field, None)
            continue
        unwrapped[field] = secret.get_secret_value()
    return unwrapped
