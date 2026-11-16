"""Demo for #750: two members of one org enable the same template on every create endpoint."""
import os
import pytest
from tests.test_template_dedupe_per_member import (
    ENDPOINTS, allow_local_plugin_sources, FIRST, SECOND, FIRST_MEMBER, SECOND_MEMBER, sign_in, write_plugin, BASE,
)

OUT = os.environ["DEMO_OUT"]
SHORT = {str(FIRST_MEMBER): "member-A", str(SECOND_MEMBER): "member-B"}


def log(line):
    with open(OUT, "a") as f:
        f.write(line + "\n")


@ENDPOINTS
async def test_demo(async_client, agent_profiles_api, tmp_path, build):
    plugin = write_plugin(tmp_path)
    h = agent_profiles_api.caller_auth
    sign_in(FIRST_MEMBER)
    path, body = build(FIRST, plugin)
    a = await async_client.post(path, json=body, headers=h)
    sign_in(SECOND_MEMBER)
    path, body = build(SECOND, plugin)
    b = await async_client.post(path, json=body, headers=h)
    total = (await async_client.get(BASE)).json()["total"]
    log(f"POST {path}")
    for who, r, sent in (("member-A", a, FIRST), ("member-B", b, SECOND)):
        j = r.json()
        log(f"  {who} sent schedule={sent['schedule']!r:14} feeds={sent['feeds']!r}")
        log(f"     -> HTTP {r.status_code}  id={j['id'][:8]}  owner={SHORT[j['user_id']]}  "
            f"schedule={j['trigger']['schedule']!r:14} feeds={j['preset_metadata']['template']['config']['feeds']!r}")
    ok = b.status_code == 201 and b.json()["user_id"] == str(SECOND_MEMBER)
    log(("  OK: member-B got their own automation" if ok else
         "  BUG: member-B was handed member-A's automation; their schedule/config were dropped")
        + f"  (automations in org: {total})")
    log("")
