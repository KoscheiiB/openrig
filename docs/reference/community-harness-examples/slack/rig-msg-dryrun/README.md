# rig-msg-dryrun

Stage-A dry run driver for OpenRig messaging: the parts of the Slack layout
that need no Slack app. Python 3.9+ standard library plus the `rig` CLI.

    python3 rig-msg-dryrun.py prepare --rig my-rig
    python3 rig-msg-dryrun.py send --rig my-rig \
        --summary "frontend save button throws" --body "..."
    python3 rig-msg-dryrun.py probe --since <iso>

- `prepare` prints the Slack app manifest URL, registers a dry-run human as
  default (idempotent), and sets a hybrid channel map: the rig mapped to the
  main channel with the helpdesk seat as `--inbound` (front door), plus
  per-seat channels for direct lines.
- `send` simulates a human inbound message, resolving the seat's canonical
  session name from the daemon first.
- `probe` classifies queue rows since a timestamp (rows to humans, rows to
  seats) and flags `unroutable` or `Refused` nudge results, reporting the
  helpdesk loop's verdict.

## Why the name resolution step exists

The address shape differs per surface, and getting it wrong is silent until
the queue tells you:

- `rig slack channel-map set --inbound` validates `member@rig`
  (`help-desk@my-rig`)
- `rig queue create --destination` must resolve to a terminal transport, so
  the canonical session name with pod namespace (`core-help-desk@my-rig`)

A bare member name as a queue destination stays pending with an exact
"unroutable" nudge diagnostic naming what was checked (tmux bindings plus the
human registry). `send` resolves the canonical name from the daemon so this
trap cannot bite.

## What the loop looks like when it works (observed, OpenRig 0.7.6)

- the helpdesk seat mints an acknowledgement row addressed to the human
- it routes the work to the owning seat with a rewritten work-order summary;
  that seat claims the row and works it
- the stuck-sweep watchdog escalates undelivered wakes to the seat (terminal
  handoff) and to the default human (an outbound external row), unprompted

## Stage B: the live Socket Mode leg

`prepare` prints the manifest URL. Then: create the app, invite it to every
mapped channel, put the tokens in via `rig slack setup`, run
`rig slack verify`, and restart the daemon. Never `rig slack disable` then
`enable` for a config change: enabling marks alerts still waiting to be
posted as already seen. A daemon restart picks up channel-map changes.
