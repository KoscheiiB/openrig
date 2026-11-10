#!/usr/bin/env python3
"""Per-seat token and cost history for an OpenRig rig, from seat transcripts.

Source: Claude Code seat session transcripts under the rig instance home
(.claude/projects/<munged-cwd>/<session>.jsonl), plus each session's subagent
transcripts (<session>/subagents/*.jsonl), counted under the parent seat. The
seat is the session's agent-name record (the OpenRig session name, e.g.
dev-impl@my-rig); the cwd folder name is only a fallback, because seats that
share a working directory share a folder. Store: SQLite beside this script,
independent of the OpenRig daemon and its 14-day usage_samples prune.

Two facts about the source shape this tool relies on:

- Transcripts double-emit usage: an assistant message can appear twice with
  the same message.id and identical usage (streaming emission plus final
  emission, timestamps seconds apart). Summing per line doubles the spend,
  so every row is keyed on (session_id, message_id) and upserts are ignored.
- Cost figures are ESTIMATES from prices.json (per-million-token rates); a
  model without an entry reports no cost rather than a guess.
- With --otel, Claude Code's OpenTelemetry api_request events add the cost it
  reports per call (cost_usd), keyed on request_id. Those events also cover
  calls no transcript records (subagent summaries, WebFetch/WebSearch).

Usage:

    python3 rig-cost.py --home <rig home> collect [--otel <dir>]
    python3 rig-cost.py --home <rig home> report
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

TOKENS_PER_MILLION = 1_000_000
SEATS_MARKER = "-seats-"

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  session_id TEXT NOT NULL,
  msg_id TEXT NOT NULL,
  seat TEXT NOT NULL,
  model TEXT NOT NULL,
  day TEXT NOT NULL,
  ts TEXT,
  sidechain INTEGER NOT NULL DEFAULT 0,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read INTEGER NOT NULL DEFAULT 0,
  cache_write INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (session_id, msg_id)
);
CREATE INDEX IF NOT EXISTS idx_day_seat ON messages(day, seat);
CREATE TABLE IF NOT EXISTS otel_requests (
  request_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  seat TEXT NOT NULL,
  model TEXT NOT NULL,
  day TEXT NOT NULL,
  ts TEXT,
  query_source TEXT,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read INTEGER NOT NULL DEFAULT 0,
  cache_write INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0
);
"""
API_REQUEST_EVENTS = {"api_request", "claude_code.api_request"}


def seat_from_project_dir(project_dir_name: str) -> str:
    if SEATS_MARKER in project_dir_name:
        return project_dir_name.split(SEATS_MARKER)[-1]
    return project_dir_name.split("-")[-1]


def open_store(store_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(store_path)
    conn.executescript(SCHEMA)
    return conn


def load_entries(transcript: Path) -> tuple[list[tuple[int, dict]], int]:
    entries, skipped = [], 0
    for line_number, line in enumerate(transcript.read_text(errors="replace").splitlines()):
        if not line.strip():
            continue
        try:
            entries.append((line_number, json.loads(line)))
        except json.JSONDecodeError:
            skipped += 1
    return entries, skipped


def seat_from_entries(entries: list[tuple[int, dict]], fallback: str) -> str:
    for _, entry in entries:
        if entry.get("type") == "agent-name" and entry.get("agentName"):
            return entry["agentName"]
    return fallback


def ingest(conn: sqlite3.Connection, transcript: Path, session_id: str, seat: str,
           entries: list[tuple[int, dict]]) -> None:
    for line_number, entry in entries:
        if entry.get("type") != "assistant":
            continue
        message = entry.get("message") or {}
        usage = message.get("usage") or {}
        if not usage:
            continue
        ts = entry.get("timestamp") or ""
        conn.execute(
            "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(session_id, msg_id) DO UPDATE SET seat = excluded.seat",
            (
                session_id,
                message.get("id") or f"{transcript.stem}:{line_number}",
                seat,
                message.get("model") or "unknown",
                ts[:10] or "unknown-day",
                ts,
                int(bool(entry.get("isSidechain"))),
                usage.get("input_tokens") or 0,
                usage.get("cache_read_input_tokens") or 0,
                usage.get("cache_creation_input_tokens") or 0,
                usage.get("output_tokens") or 0,
            ),
        )


def otlp_value(value: dict):
    return next(iter(value.values()), None)


def collect_otel(conn: sqlite3.Connection, otel_dir: Path) -> int:
    """Upsert Claude Code api_request events from OTLP/JSON log export lines (*.jsonl)."""
    seats = dict(conn.execute("SELECT session_id, seat FROM messages GROUP BY session_id"))
    for export in sorted(otel_dir.glob("*.jsonl")):
        for line in export.read_text(errors="replace").splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            for resource_logs in payload.get("resourceLogs") or []:
                resource = {a["key"]: otlp_value(a["value"]) for a in (resource_logs.get("resource") or {}).get("attributes") or []}
                for scope_logs in resource_logs.get("scopeLogs") or []:
                    for record in scope_logs.get("logRecords") or []:
                        attrs = {a["key"]: otlp_value(a["value"]) for a in record.get("attributes") or []}
                        if attrs.get("event.name") not in API_REQUEST_EVENTS or not attrs.get("request_id"):
                            continue
                        session_id = attrs.get("session.id") or ""
                        ts = attrs.get("event.timestamp") or ""
                        conn.execute(
                            "INSERT INTO otel_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
                            " ON CONFLICT(request_id) DO UPDATE SET seat = excluded.seat",
                            (
                                attrs["request_id"],
                                session_id,
                                seats.get(session_id) or resource.get("seat") or "unknown",
                                attrs.get("model") or "unknown",
                                ts[:10] or "unknown-day",
                                ts,
                                attrs.get("query_source"),
                                int(attrs.get("input_tokens") or 0),
                                int(attrs.get("cache_read_tokens") or 0),
                                int(attrs.get("cache_creation_tokens") or 0),
                                int(attrs.get("output_tokens") or 0),
                                float(attrs.get("cost_usd") or 0),
                            ),
                        )
    return conn.execute("SELECT count(*) FROM otel_requests").fetchone()[0]


def collect(home: Path, store_path: Path, otel_dir: Path | None = None) -> dict[str, int]:
    projects_dir = home / ".claude" / "projects"
    if not projects_dir.is_dir():
        sys.exit(f"no transcript projects dir at {projects_dir}")
    conn = open_store(store_path)
    scanned = skipped = 0
    seats: dict[str, str] = {}
    sessions = sorted(projects_dir.glob("*/*.jsonl"))
    subagents = sorted(projects_dir.glob("*/*/subagents/*.jsonl"))
    for transcript in sessions + subagents:
        is_subagent = transcript.parent.name == "subagents"
        session_id = transcript.parents[1].name if is_subagent else transcript.stem
        project_dir = transcript.parents[2] if is_subagent else transcript.parent
        entries, bad = load_entries(transcript)
        seat = seats.get(session_id) or seat_from_entries(entries, seat_from_project_dir(project_dir.name))
        seats.setdefault(session_id, seat)
        ingest(conn, transcript, session_id, seat, entries)
        scanned += 1
        skipped += bad
    total_rows = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
    result = {"transcripts": scanned, "store_rows": total_rows, "unparseable_lines": skipped}
    if otel_dir is not None:
        result["otel_requests"] = collect_otel(conn, otel_dir)
    conn.commit()
    conn.close()
    return result


def load_prices(prices_path: Path) -> dict[str, dict[str, float]]:
    if not prices_path.exists():
        return {}
    return json.loads(prices_path.read_text())


def estimate_cost_usd(
    prices: dict[str, dict[str, float]], model: str, row: dict[str, int]
) -> float | None:
    rates = prices.get(model)
    if not rates:
        return None
    cost = 0.0
    for token_field, rate_key in (
        ("input_tokens", "input"),
        ("cache_read", "cache_read"),
        ("cache_write", "cache_write"),
        ("output_tokens", "output"),
    ):
        rate = rates.get(rate_key)
        if rate is None:
            return None
        cost += row[token_field] / TOKENS_PER_MILLION * rate
    return cost


def fmt_tokens(count: int) -> str:
    if count >= TOKENS_PER_MILLION:
        return f"{count / TOKENS_PER_MILLION:.2f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}K"
    return str(count)


def report(store_path: Path, prices_path: Path) -> None:
    prices = load_prices(prices_path)
    conn = open_store(store_path)
    groups: dict[tuple[str, str, str], tuple] = {}
    for day, seat, model, *counts in conn.execute(
        """SELECT day, seat, model, count(*), sum(input_tokens), sum(cache_read),
                  sum(cache_write), sum(output_tokens), sum(sidechain)
           FROM messages GROUP BY day, seat, model"""
    ):
        groups[(day, seat, model)] = tuple(counts)
    otel = {
        (day, seat, model): (calls, cost)
        for day, seat, model, calls, cost in conn.execute(
            "SELECT day, seat, model, count(*), sum(cost_usd) FROM otel_requests GROUP BY day, seat, model"
        )
    }
    grand_tokens = 0
    grand_cost = 0.0
    otel_cost = 0.0
    unpriced_models: set[str] = set()
    current_day = None
    for day, seat, model in sorted(set(groups) | set(otel)):
        replies, inp, cread, cwrite, outp, side = groups.get((day, seat, model), (0, 0, 0, 0, 0, 0))
        if day != current_day:
            if current_day is not None:
                print()
            print(f"== {day}")
            current_day = day
        total_tokens = (inp or 0) + (cread or 0) + (cwrite or 0) + (outp or 0)
        grand_tokens += total_tokens
        row = {
            "input_tokens": inp or 0,
            "cache_read": cread or 0,
            "cache_write": cwrite or 0,
            "output_tokens": outp or 0,
        }
        cost = estimate_cost_usd(prices, model, row)
        if (day, seat, model) in otel:
            calls, reported = otel[(day, seat, model)]
            otel_cost += reported
            cost_text = f"otel ${reported:.4f} ({calls} calls)"
        elif cost is None:
            cost_text = "n/a"
            unpriced_models.add(model)
        else:
            grand_cost += cost
            cost_text = f"${cost:.4f}"
        side_note = f" (+{side} sidechain)" if side else ""
        print(
            f"  {seat:<26} {model:<22} replies {replies:>3}{side_note}"
            f" | in {fmt_tokens(row['input_tokens']):>8}"
            f" cache_r {fmt_tokens(row['cache_read']):>8}"
            f" cache_w {fmt_tokens(row['cache_write']):>8}"
            f" out {fmt_tokens(row['output_tokens']):>8}"
            f" | {cost_text}"
        )
    conn.close()
    print()
    print(f"total tokens (all classes): {fmt_tokens(grand_tokens)}")
    if otel:
        print(f"OTel-reported spend: ${otel_cost:.4f}  [Claude Code's cost_usd at API list prices, not a charge;"
              " covers only calls made while telemetry was on, including calls no transcript records]")
    if unpriced_models:
        print(f"cost ESTIMATE only for priced models; unpriced: {', '.join(sorted(unpriced_models))}")
        print(f"add per-million rates to {prices_path} to price them")
    if prices:
        print(f"estimated spend (priced models): ${grand_cost:.4f}  [ESTIMATE from configured rates, not a charge]")
    else:
        print("no prices configured; token counts only")


def main() -> None:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--home", type=Path, required=True, help="OpenRig instance home (transcripts under .claude/projects)")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("collect", "scan transcripts, upsert into the store (idempotent)"),
        ("report", "print per-seat per-day history with cost estimates"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--store", type=Path, default=here / "cost.sqlite")
        command.add_argument("--prices", type=Path, default=here / "prices.json")
        if name == "collect":
            command.add_argument("--otel", type=Path, help="folder of OTLP/JSON log export lines (*.jsonl) to read api_request costs from")
    args = parser.parse_args()
    if args.command == "collect":
        print(json.dumps(collect(args.home, args.store, args.otel)))
    else:
        report(args.store, args.prices)


if __name__ == "__main__":
    main()
