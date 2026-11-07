#!/usr/bin/env python3
"""Verify model tickets without printing their values.

Reads ``config.json`` and ``gemini-auth.json``, merges ``model_tickets``
(the auth file wins, matching ``load_cookie()``), parses each ticket, and
reports the ``(family, variant)`` embedded at indexes 14/15.

Exit code is 1 when one of the required Flash-Lite tickets is missing,
unparsable, or points at the wrong route. Other families are reported only
with ``--all`` and never fail the check.

Usage:
    python tools/check_tickets.py
    python tools/check_tickets.py --all
    python tools/check_tickets.py --config config.json --auth gemini-auth.json
"""
import argparse
import json
import os
import sys

EXPECTED = {
    "argon": (8, 1),
    "argon-high": (8, 2),
    "argon-medium": (8, 4),
    "flash": (1, 1),
    "flash-medium": (1, 4),
    "flash-thinking": (1, 2),
    "lite": (6, 1),
    "lite-medium": (6, 4),
    "lite-thinking": (6, 2),
    "pro": (3, 1),
    "pro-medium": (3, 4),
    "pro-thinking": (3, 2),
}

REQUIRED = ("lite", "lite-medium", "lite-thinking")


def _load_json(path):
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        print("ERROR: cannot read %s: %s" % (path, exc))
        return {}
    return data if isinstance(data, dict) else {}


def _tickets(data):
    raw = data.get("model_tickets")
    return raw if isinstance(raw, dict) else {}


def _route(value):
    """Return (family, variant) for a ticket value, or None when malformed."""
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, list) or len(parsed) < 16:
        return None
    fam, var = parsed[14], parsed[15]
    if isinstance(fam, bool) or isinstance(var, bool):
        return None
    if not isinstance(fam, int) or not isinstance(var, int):
        return None
    return fam, var


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Check gemini-web2api model tickets without printing secrets."
    )
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--auth", default="gemini-auth.json")
    ap.add_argument(
        "--all",
        action="store_true",
        help="also report non-lite families (missing is not an error)",
    )
    args = ap.parse_args(argv)

    config = _load_json(args.config)
    auth = _load_json(args.auth)

    merged = {}
    merged.update(_tickets(config))
    merged.update(_tickets(auth))

    print("config: %s %s" % (
        args.config, "found" if os.path.exists(args.config) else "MISSING"))
    print("auth:   %s %s" % (
        args.auth, "found" if os.path.exists(args.auth) else "MISSING"))

    cookie_file = config.get("cookie_file") or auth.get("cookie_file")
    if cookie_file:
        exists = "exists" if os.path.exists(cookie_file) else "MISSING"
        print("cookie_file: %r %s" % (cookie_file, exists))
    else:
        print("cookie_file: MISSING/UNSET")

    has_cookie = bool(auth.get("cookie") or config.get("cookie"))
    print("cookie: %s" % ("present" if has_cookie else "MISSING"))
    print("model_tickets:")

    keys = list(EXPECTED) if args.all else list(REQUIRED)
    extras = sorted(k for k in merged if k not in EXPECTED)
    failed = False

    for key in keys:
        want = EXPECTED[key]
        raw = merged.get(key)
        got = _route(raw) if raw else None
        if not raw:
            status = "MISSING"
        elif got is None:
            status = "BAD"
        elif got != want:
            status = "WRONG"
        else:
            status = "OK"
        if key in REQUIRED and status != "OK":
            failed = True
        route = "(%d,%d)" % got if got else "-"
        print("  %-14s %-7s route=%-8s expected=(%d,%d)" % (
            key, status, route, want[0], want[1]))

    for key in extras:
        got = _route(merged.get(key))
        route = "(%d,%d)" % got if got else "-"
        print("  %-14s EXTRA   route=%-8s (not in EXPECTED_TICKET_ROUTES)" % (
            key, route))

    if failed:
        print("RESULT: missing or invalid required lite ticket(s).")
        return 1
    print("RESULT: required lite tickets are ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
