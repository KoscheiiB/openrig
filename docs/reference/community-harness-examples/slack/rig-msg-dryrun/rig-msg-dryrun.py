#!/usr/bin/env python3
"""Stage-A dry run for OpenRig messaging: layout, inbound, and loop probing.

Drives the parts of the Slack messaging layout that need no Slack app:
registering the dry-run human, setting the hybrid channel map, simulating a
human inbound message, and probing the queue for the helpdesk loop's outcome
(acknowledgement to the human, routed row to the owning seat).

Two address shapes matter and this tool never lets you mix them up:
- channel-map --inbound takes member@rig (help-desk@my-rig)
- queue --destination takes the seat's canonical session name, resolved here
  from the daemon (core-help-desk@my-rig); a wrong address stays pending with
  an "unroutable" nudge result naming what was checked

The live Socket Mode leg (stage B) needs a Slack app: `prepare` prints the
manifest URL; tokens go in via `rig slack setup`, then `rig slack verify` and
a daemon restart (never disable/enable, which marks waiting alerts as seen).
"""
import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

DAEMON_URL = "http://127.0.0.1:7433"
HUMAN_POLICY = "Dry-run participant for the messaging layout spike"
DRY_CHANNEL_IDS = {"main": "CDRYRUNMAIN", "dev": "CDRYRUNDEV", "ui": "CDRYRUNUI"}
EXTERNAL_SUFFIX = "@external"
PROBE_INTERVAL_SECONDS = 15


def run_rig(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["rig", *args], capture_output=True, text=True, timeout=120)


def api(path: str):
    with urllib.request.urlopen(DAEMON_URL.rstrip("/") + path, timeout=10) as response:
        return json.loads(response.read())


def canonical_session(rig_name: str, seat: str) -> str:
    """Resolve a seat to its canonical session name (core-<seat>@rig shape)."""
    for summary in api("/api/ps"):
        if summary.get("name") != rig_name:
            continue
        for node in api(f"/api/rigs/{summary['rigId']}/nodes"):
            logical = (node.get("logicalId") or "").split(".", 1)[-1]
            if logical == seat:
                return node["canonicalSessionName"]
    sys.exit(f"seat {seat} not found on rig {rig_name}; check `rig ps --nodes`")


def cmd_prepare(args: argparse.Namespace) -> None:
    manifest = run_rig(["slack", "manifest", "--url"])
    print("Slack app manifest URL (stage B, create the app when ready):")
    print(manifest.stdout.strip(), "\n")

    participants = run_rig(["gateway", "participants"])
    if args.human in participants.stdout:
        print(f"human {args.human} already registered")
    else:
        result = run_rig([
            "gateway", "human", "add", args.human,
            "--display-name", "Dry-run Owner",
            "--delivery-class", "B",
            "--binding", f"slack:{args.connector}:file:{args.secrets_file}:primary",
            "--policy", HUMAN_POLICY, "--default",
            "--reason", "messaging dry run",
        ])
        print(result.stdout.strip() or result.stderr.strip())

    inbound_seat = canonical_session(args.rig, args.inbound_seat).split("@")[0]
    entries = [
        (args.rig, DRY_CHANNEL_IDS["main"], inbound_seat, "main front door"),
        (f"{args.dev_seat}@{args.rig}", DRY_CHANNEL_IDS["dev"], args.dev_seat, "backend direct line"),
        (f"{args.ui_seat}@{args.rig}", DRY_CHANNEL_IDS["ui"], args.ui_seat, "frontend direct line"),
    ]
    for match, channel, seat_member, label in entries:
        result = run_rig([
            "slack", "channel-map", "set", match, channel,
            "--inbound", f"{seat_member}@{args.rig}",
            "--reason", f"dry run: {label}",
        ])
        verdict = "set" if result.returncode == 0 else result.stderr.strip().splitlines()[-1]
        print(f"channel-map {match:<28} -> {channel}  inbound {seat_member}@{args.rig}  [{verdict}]")
    print("\nnext (stage B): create the app from the URL, invite it to the mapped channels,")
    print("`rig slack setup` with the tokens, `rig slack verify`, then a daemon restart.")


def cmd_send(args: argparse.Namespace) -> None:
    destination = canonical_session(args.rig, args.seat)
    source = f"{args.human}{EXTERNAL_SUFFIX}"
    result = run_rig([
        "queue", "create",
        "--source", source,
        "--destination", destination,
        "--summary", args.summary,
        "--body", args.body,
        "--priority", args.priority,
        *(["--mission", args.mission] if args.mission else []),
        *(["--slice", args.slice] if args.slice else []),
    ])
    for line in result.stdout.splitlines():
        if "request ID" in line:
            print(line.strip())
    if result.returncode != 0:
        print(result.stderr.strip())
    print(f"inbound simulated: {source} -> {destination}")


def cmd_probe(args: argparse.Namespace) -> None:
    import time
    deadline = time.time() + args.timeout
    while True:
        rows = api("/api/queue/list")
        fresh = [r for r in rows if (r.get("tsCreated") or "") >= args.since]
        to_human = [r for r in fresh if (r.get("destinationSession") or "").endswith(EXTERNAL_SUFFIX)]
        to_seats = {}
        for row in fresh:
            dest = row.get("destinationSession") or ""
            if not dest.endswith(EXTERNAL_SUFFIX):
                to_seats.setdefault(dest, []).append(row)
        for row in fresh:
            nudge = (row.get("lastNudgeResult") or "")
            flag = f"  !! {nudge[:70]}" if ("unroutable" in nudge or "Refused" in nudge) else ""
            print(row.get("qitemId", "?")[-8:], (row.get("state") or "?")[:11],
                  "->", (row.get("destinationSession") or "?")[:26],
                  "|", (row.get("summary") or "")[:46], flag)
        verdict = {
            "rows since": len(fresh),
            "ack/outbound to human": len(to_human),
            "seats addressed": {seat: len(items) for seat, items in to_seats.items()},
        }
        print(json.dumps(verdict, indent=1))
        if not args.wait or to_human or time.time() > deadline:
            break
        time.sleep(PROBE_INTERVAL_SECONDS)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="manifest URL, dry-run human, hybrid channel map")
    prepare.add_argument("--rig", required=True)
    prepare.add_argument("--human", default="dryrun.owner")
    prepare.add_argument("--connector", default="openrig-personal")
    prepare.add_argument("--secrets-file", type=Path, default=Path("slack-dryrun.env"))
    prepare.add_argument("--inbound-seat", default="help-desk")
    prepare.add_argument("--dev-seat", default="dev-build")
    prepare.add_argument("--ui-seat", default="dev-ui")
    prepare.set_defaults(func=cmd_prepare)

    send = sub.add_parser("send", help="simulate a human inbound message")
    send.add_argument("--rig", required=True)
    send.add_argument("--human", default="dryrun.owner")
    send.add_argument("--seat", default="help-desk")
    send.add_argument("--summary", required=True)
    send.add_argument("--body", required=True)
    send.add_argument("--priority", default="routine", choices=["routine", "urgent", "critical"])
    send.add_argument("--mission")
    send.add_argument("--slice")
    send.set_defaults(func=cmd_send)

    probe = sub.add_parser("probe", help="classify queue rows and report the loop verdict")
    probe.add_argument("--since", required=True, help="ISO timestamp; rows created at or after it")
    probe.add_argument("--wait", action="store_true", help="poll until a human-addressed row appears")
    probe.add_argument("--timeout", type=int, default=300, help="max seconds for --wait")
    probe.set_defaults(func=cmd_probe)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
