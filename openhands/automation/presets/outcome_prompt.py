"""Shared outcome instructions and secret injection for conversation presets."""

from collections.abc import Mapping


def outcome_secret_env(environ: Mapping[str, str]) -> dict[str, str]:
    if not environ.get("AUTOMATION_OUTCOME_URL") or not environ.get(
        "AUTOMATION_OUTCOME_TOKEN"
    ):
        return {}
    return {
        key: environ[key]
        for key in (
            "AUTOMATION_OUTCOME_URL",
            "AUTOMATION_OUTCOME_TOKEN",
            "AUTOMATION_OUTCOME_SCHEMA",
        )
        if environ.get(key)
    }


def outcome_context_section(environ: Mapping[str, str]) -> str | None:
    if not outcome_secret_env(environ):
        return None
    section = """## Outcome Reporting API

This automation belongs to a factory. Report structured outcome values as they
become available, not only at the end of the run. POST a JSON body like
`{"values":[{"key":"score","type":"number","value":4.5}]}` to
`$AUTOMATION_OUTCOME_URL` with `Authorization: Bearer $AUTOMATION_OUTCOME_TOKEN`.
Use only keys and value types registered by your factory. Each successful POST
stores a separate set of values tagged to this run; there is no requirement to
wait for the final result. For example:

```bash
curl --fail-with-body -X POST "$AUTOMATION_OUTCOME_URL" \\
  -H "Authorization: Bearer $AUTOMATION_OUTCOME_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{"values":[{"key":"score","type":"number","value":4.5}]}'
```"""
    if schema := environ.get("AUTOMATION_OUTCOME_SCHEMA"):
        section += f"""

For a final nested result, you may instead POST `{{"payload": <your result>}}`.
The configured parser extracts registered typed values from the nested payload.
All configured expressions must return a value; an invalid or missing result
rejects that entire POST. The submission schema is:

```json
{schema}
```"""
    else:
        section += """

For a final result without a configured parser, you may instead POST a strict
flat JSON object such as `{"payload":{"score":4.5}}`. Nested objects are not
accepted in this mode; use `values[]` for incremental reporting."""
    return section
