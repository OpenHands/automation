"""Temporal client factory.

Provides functions to create and manage Temporal clients for connecting
to the Temporal service. Supports both self-hosted and Temporal Cloud.
"""

import logging
from functools import lru_cache

from temporalio.client import Client, TLSConfig

from automation.config import Settings, get_settings


logger = logging.getLogger(__name__)


async def create_temporal_client(settings: Settings | None = None) -> Client:
    """Create a new Temporal client.

    Args:
        settings: Application settings. If None, uses get_settings().

    Returns:
        Connected Temporal client.

    Raises:
        Exception: If connection fails.
    """
    if settings is None:
        settings = get_settings()

    logger.info(
        "Connecting to Temporal at %s (namespace=%s)",
        settings.temporal_address,
        settings.temporal_namespace,
    )

    # Build TLS config if enabled (for Temporal Cloud)
    tls_config: TLSConfig | bool = False
    if settings.temporal_tls_enabled:
        if settings.temporal_tls_cert_path and settings.temporal_tls_key_path:
            # Load cert and key from files
            with open(settings.temporal_tls_cert_path, "rb") as f:
                client_cert = f.read()
            with open(settings.temporal_tls_key_path, "rb") as f:
                client_key = f.read()

            tls_config = TLSConfig(
                client_cert=client_cert,
                client_private_key=client_key,
            )
            logger.info("Using mTLS for Temporal connection")
        else:
            # Use system TLS (for Temporal Cloud with API keys)
            tls_config = True
            logger.info("Using TLS for Temporal connection")

    client = await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        tls=tls_config,
    )

    logger.info("Connected to Temporal")
    return client


# Global client instance (created lazily)
_client: Client | None = None


async def get_temporal_client() -> Client:
    """Get or create the global Temporal client.

    This function maintains a single client instance for the application.
    The client is created on first call and reused thereafter.

    Returns:
        Connected Temporal client.
    """
    global _client
    if _client is None:
        _client = await create_temporal_client()
    return _client


async def close_temporal_client() -> None:
    """Close the global Temporal client if it exists."""
    global _client
    if _client is not None:
        await _client.close()
        _client = None
        logger.info("Temporal client closed")
