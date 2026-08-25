"""Stream sources: inbound events over a held-open connection rather than HTTP.

Submodules:
    base.py        StreamProvider protocol, per-source health
    slack.py       Slack Socket Mode provider
    supervisor.py  Source registry and the supervised task per source

Off unless `AUTOMATION_STREAMS_ENABLED` is set. Everything past `emit()` is the
ordinary event pipeline: a streamed event routes through `accept_event()` like
any webhook, against unmodified automation definitions.
"""

from openhands.automation.streams.base import (
    SourceHealth,
    StreamConfigError,
    StreamProvider,
    stream_health,
)
from openhands.automation.streams.supervisor import (
    BUILTIN_STREAM_SOURCES,
    build_stream_providers,
    stream_supervisor_loop,
)


__all__ = [
    "BUILTIN_STREAM_SOURCES",
    "SourceHealth",
    "StreamConfigError",
    "StreamProvider",
    "build_stream_providers",
    "stream_health",
    "stream_supervisor_loop",
]
