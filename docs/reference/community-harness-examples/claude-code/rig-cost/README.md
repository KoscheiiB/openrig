# rig-cost

Per-seat token and cost history for an [OpenRig](https://openrig.dev) rig, read
straight from the seat session transcripts. Python 3.9+ standard library only;
nothing to install.

## Why

The daemon's own `usage_samples` table is pruned after 14 days by default
(`retention.usage_samples_days`), and `rig usage cost` is one cumulative estimate
per seat for the current session, not history you keep. The transcripts under
`<rig home>/.claude/projects/` carry per-message usage blocks that persist,
so history is built from there into a store the daemon never touches.

One measured trap in the source data (see #32 for the run): assistant
messages are emitted twice with the same `message.id` and identical usage
(49% of usage-bearing lines in the test corpus). Rows are keyed on
`(session_id, message_id)`, so re-collecting is idempotent and nothing is
counted twice.

Each seat is named by the session's `agent-name` record (the OpenRig session
name, such as `dev-impl@my-rig`), not by its working-directory folder, so seats
that share a checkout stay apart. Subagent transcripts
(`<session>/subagents/*.jsonl`) count under their parent seat.

Transcript totals are a floor. Some calls never reach a transcript: the
subagent result summaries and the small-model calls behind WebFetch and
WebSearch. Claude Code's OpenTelemetry `api_request` events include them.

## OpenTelemetry costs (optional)

If the seats export Claude Code's telemetry (`CLAUDE_CODE_ENABLE_TELEMETRY=1`,
`OTEL_LOGS_EXPORTER=otlp`, `OTEL_EXPORTER_OTLP_PROTOCOL=http/json`, set in the
seat's process environment, such as the rig spec `env`; a settings-file `env`
block did not export), point `collect --otel <dir>` at a folder of OTLP/JSON
log export lines (`*.jsonl`, one export request per line). Each `api_request`
event is stored once by `request_id`, with the `cost_usd` Claude Code reports
for that call. `report` then shows `otel $… (N calls)` for each day, seat and
model it covers, plus an OTel spend total. Those figures are Claude Code's own
estimates at API list prices, not a bill, and exist only while telemetry was on.

## Use

    python3 rig-cost.py --home <rig home> collect [--otel <dir>]
    python3 rig-cost.py --home <rig home> report

`collect` scans and upserts (safe to re-run any time); `report` prints per-seat
per-day rows: replies, input, cache read, cache write, output tokens, sidechain
counts, and a dollar column. `--store` and `--prices` override the defaults
(`cost.sqlite` and `prices.json` beside the script).

## Pricing discipline

`prices.json` ships empty on purpose. Per-million-token rates, example shape:

```json
{
  "<model name as it appears in the transcript>": {"input": 0.0, "cache_read": 0.0, "cache_write": 0.0, "output": 0.0}
}
```

A model with no entry prints `n/a` instead of a number, and every dollar figure
is labeled an ESTIMATE from configured rates. No rate is ever guessed, and the
tool never talks to a billing API.
