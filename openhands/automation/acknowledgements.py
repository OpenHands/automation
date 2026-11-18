"""Named, fixed webhook acknowledgements; unrelated to provider handshakes."""

from dataclasses import dataclass
from typing import Any, Final

from starlette.responses import JSONResponse


DEFAULT_ACKNOWLEDGEMENT: Final = "event_response"
TEAMS_ACKNOWLEDGEMENT: Final = "teams_message"


@dataclass(frozen=True, slots=True)
class Acknowledgement:
    """A protocol response and its request-path timing constraint.

    No operator templates or payload interpolation: responses may be shown in
    a public channel. Only the default response includes ingestion metadata.
    """

    deadline_seconds: int | None = None
    message: str | None = None

    def response(self, metadata: dict[str, Any]) -> JSONResponse:
        body = (
            {"type": "message", "text": self.message}
            if self.message is not None
            else metadata
        )
        return JSONResponse(body)


ACKNOWLEDGEMENTS = {
    DEFAULT_ACKNOWLEDGEMENT: Acknowledgement(),
    TEAMS_ACKNOWLEDGEMENT: Acknowledgement(
        deadline_seconds=5,
        message="Request received. Any matching automation will run separately.",
    ),
}


def get_acknowledgement(name: str | None) -> Acknowledgement | None:
    return ACKNOWLEDGEMENTS.get(name or DEFAULT_ACKNOWLEDGEMENT)
