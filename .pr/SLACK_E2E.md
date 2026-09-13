# Live Slack routing demonstration

This proof uses a real Slack Socket Mode connection and bot response, while
keeping all OpenHands state in an in-memory SQLite database. It exercises the
production `SlackStreamProvider`, `accept_event()`, subject ownership lookup,
and queued-run coalescing. No LLM, Agent Server, Docker, or public webhook is
required.

## 1. Create the Slack app

At <https://api.slack.com/apps>, choose **Create New App → From an app
manifest**, select a development workspace, and paste
`.pr/slack-demo-manifest.yaml`.

After creating it:

1. Open **Socket Mode**, create an app-level token with
   `connections:write`, and copy the resulting `xapp-...` token.
2. Open **OAuth & Permissions**, install the app to the workspace, and copy the
   `xoxb-...` bot token.
3. Invite **OpenHands Routing Demo** to a public test channel.
4. Copy the channel ID from Slack's **View channel details** dialog.

Do not paste tokens into a PR comment, terminal output, or this repository.

## 2. Export credentials locally

Run these in the same terminal that will launch the proof. The leading space
keeps them out of shell history when `HIST_IGNORE_SPACE` is enabled.

```bash
 export SLACK_APP_TOKEN='xapp-...'
 export SLACK_BOT_TOKEN='xoxb-...'
 export SLACK_CHANNEL_ID='C...'
```

## 3. Run the proof

From the repository root:

```bash
uv run python .pr/slack_e2e.py
```

The script prints a unique mention such as:

```text
<@U012345> oh-routing-e2e-a1b2c3
```

Send that exact mention in the configured channel. After the bot replies in
the thread, send a normal human reply without mentioning it.

Success ends with:

```text
PASS — live Slack thread routing is correct
  total runs:     1 (expected 1)
```

The printed `subject_key`, derived conversation ID, captured follow-up text,
and single run count are the evidence to attach to PR #446. Token values are
never printed.

## 4. Negative case

While the script is waiting for the owned-thread follow-up, reply in any other
human-rooted thread in the same test channel. The provider accepts it as a
candidate, but it does not create a run. The final count remains one.
