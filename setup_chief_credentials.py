#!/usr/bin/env python3
"""
setup_chief_credentials.py — operator tool for Sarpanch (Chief) passwords.

The chief panel never stores or displays a plaintext password. Use this script
on the server to set or rotate one:

    python setup_chief_credentials.py --list
    python setup_chief_credentials.py --area "Rampur"          # prompts (no echo)
    python setup_chief_credentials.py --area "Rampur" --id chief_rampur
    python setup_chief_credentials.py --rotate-all             # random passwords
    python setup_chief_credentials.py --show-initial           # one-time file
    python setup_chief_credentials.py --disable-area "Rampur"

Passwords must be at least 12 characters with 3 of: upper, lower, digit,
symbol. Only PBKDF2-HMAC-SHA256 hashes are written to chief_credentials.json.
Rotating a password also invalidates existing chief sessions.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from datetime import datetime, timezone

import chief_auth
import security


def _store() -> dict:
    store = chief_auth._read_store()
    return dict(store)


def _save(store: dict) -> None:
    chief_auth._bump_version(store)
    chief_auth._write_store(store)


def list_areas(store: dict) -> None:
    print("Areas configured:", len([k for k in store if not k.startswith("_")]))
    for area, entry in sorted(store.items()):
        if area.startswith("_") or not isinstance(entry, dict):
            continue
        algo = str(entry.get("password_hash", "")).split("$")[0] or "MISSING"
        flag = " (must change)" if entry.get("must_change") else ""
        print(f"  {area:<16} id={entry.get('id', ''):<28} hash={algo}{flag}")


def set_password(area: str, password: str, uid: str | None) -> int:
    problem = security.password_problem(password)
    if problem:
        print(f"Refused: {problem}", file=sys.stderr)
        return 1
    store = _store()
    entry = store.get(area)
    if not isinstance(entry, dict):
        entry = {"id": uid or f"chief_{chief_auth._slug(area)}"}
    if uid:
        entry["id"] = uid.strip()
    entry["password_hash"] = security.hash_password(password)
    entry["must_change"] = False
    entry["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    entry.pop("password", None)
    store[area] = entry
    _save(store)
    security.audit("chief_password_changed", actor=entry["id"], outcome="rotated",
                   detail={"area": area, "via": "setup_chief_credentials.py"})
    print(f"Password updated for {area} (id={entry['id']}). Existing sessions invalidated.")
    return 0


def disable_area(area: str) -> int:
    store = _store()
    if not isinstance(store.get(area), dict):
        print(f"Refused: {area} is not configured.", file=sys.stderr)
        return 1
    store.pop(area)
    _save(store)
    security.audit("chief_account_removed", actor="", outcome="info", detail={"area": area})
    print(f"Removed {area}. Run the app once to re-issue a credential if needed.")
    return 0


def rotate_all() -> int:
    store = _store()
    issued = {}
    for area, entry in list(store.items()):
        if area.startswith("_") or not isinstance(entry, dict):
            continue
        password = security.generate_password()
        entry["password_hash"] = security.hash_password(password)
        entry["must_change"] = True
        entry["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        entry.pop("password", None)
        store[area] = entry
        issued[area] = {"id": entry.get("id", ""), "password": password}
    _save(store)
    chief_auth._append_initial_passwords(issued)
    security.audit("chief_password_changed", actor="", outcome="rotated",
                   detail={"areas": len(issued), "via": "rotate_all"})
    print(f"Rotated {len(issued)} passwords.")
    print(f"They are in {chief_auth.INITIAL_PASSWORDS_FILE} — read, then delete it.")
    return 0


def show_initial() -> int:
    path = chief_auth.INITIAL_PASSWORDS_FILE
    if not os.path.exists(path):
        print("No one-time credentials file exists.", file=sys.stderr)
        return 1
    with open(path, encoding="utf-8") as fh:
        print(fh.read())
    print("\nDelete this file once the sarpanch has the passwords.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Manage Sarpanch credentials.")
    parser.add_argument("--area", help="Area name exactly as shown in the login list")
    parser.add_argument("--id", dest="uid", help="Override the chief ID (optional)")
    parser.add_argument("--password", help="Pass on the command line (discouraged: shell history)")
    parser.add_argument("--list", action="store_true", help="List configured areas")
    parser.add_argument("--rotate-all", action="store_true", help="Rotate every password")
    parser.add_argument("--show-initial", action="store_true", help="Show the one-time file")
    parser.add_argument("--disable-area", help="Remove an area's credentials")
    args = parser.parse_args(argv)

    if args.list:
        list_areas(_store())
        return 0
    if args.rotate_all:
        return rotate_all()
    if args.show_initial:
        return show_initial()
    if args.disable_area:
        return disable_area(args.disable_area)
    if not args.area:
        parser.print_help()
        return 2

    password = args.password
    if not password:
        password = getpass.getpass(f"New password for {args.area}: ")
        confirm = getpass.getpass("Repeat the password: ")
        if password != confirm:
            print("Passwords did not match.", file=sys.stderr)
            return 1
    return set_password(args.area, password, args.uid)


if __name__ == "__main__":
    raise SystemExit(main())
