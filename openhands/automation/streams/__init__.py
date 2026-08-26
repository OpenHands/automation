"""Stream sources: inbound events over a held-open connection rather than HTTP.

Submodules:
    base.py        StreamProvider protocol, per-source health
    slack.py       Slack Socket Mode provider
    supervisor.py  Source registry and the supervised task per source

Configuring `AUTOMATION_SLACK_APPS` is what starts a source; a deployment that
configures none keeps exactly today's behaviour, so there is no second env var
to flip. `AUTOMATION_STREAMS_ENABLED=false` is the kill switch for holding the
sockets down without unsetting the credentials. Self-hosted only either way,
for two independent reasons: Slack does not allow Socket Mode apps in its
public Marketplace, and a connection pinned to one replica does not fit a
stateless autoscaled tier the way a request does. Webhooks stay the cloud
path.

Everything past `emit()` is the ordinary event pipeline: a streamed event
routes through `accept_event()` like any webhook, against unmodified automation
definitions.
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
