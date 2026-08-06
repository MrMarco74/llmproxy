#!/usr/bin/env python3
"""
One-time auth migration for llmproxy's corporate-readiness overhaul (Phase 5
of the auth/secrets/chargeback-currency plan).

Run this ON THE PROXY HOST (worker), after Phases 1-4 have been deployed
and llmproxy.service has started at least once -- that's what creates
/opt/llmproxy/.secret_key and the users/api_keys/secrets tables this script
writes into.

What it does:
  1. Reads /opt/llmproxy/clients.yaml, moves each client's `token:` value
     UNCHANGED into the `secrets` table (Fernet-encrypted, as
     client_token.<name>), then strips the `token:` field from
     clients.yaml. Existing clients keep working with the exact same
     bearer token they already have -- nothing to redistribute or rotate.
  2. Prompts for a username/password and creates one seed `admin` user in
     the `users` table (for dashboard login).
  3. Creates one `service` API key (role=admin) for the dashboard's own
     internal calls to the proxy, printed ONCE -- put it in
     /opt/llmproxy/.env as LLMPROXY_SERVICE_KEY and restart the dashboard
     container (docker compose up -d) to pick it up.

Safe to re-run: skips any client whose token secret already exists, refuses
to create a duplicate admin username, and reuses an existing service key
for the same owner_name instead of minting a second one.

Usage:
  python3 scripts/migrate_auth.py --dry-run   # see what would happen first
  python3 scripts/migrate_auth.py             # do it for real
"""
import argparse
import getpass
import secrets as secrets_mod
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import bcrypt
import yaml
from cryptography.fernet import Fernet

DB_PATH = Path("/var/lib/llmproxy/llmproxy.db")
CLIENTS_YAML = Path("/opt/llmproxy/clients.yaml")
SECRET_KEY_PATH = Path("/opt/llmproxy/.secret_key")


def _db() -> sqlite3.Connection:
    if not DB_PATH.exists():
        sys.exit(f"DB not found at {DB_PATH} -- has llmproxy.service started at least once?")
    return sqlite3.connect(str(DB_PATH))


def _fernet() -> Fernet:
    if not SECRET_KEY_PATH.exists():
        sys.exit(f"{SECRET_KEY_PATH} not found -- has llmproxy.service started at least once?")
    return Fernet(SECRET_KEY_PATH.read_bytes().strip())


def migrate_client_tokens(con: sqlite3.Connection, fernet: Fernet, dry_run: bool) -> int:
    if not CLIENTS_YAML.exists():
        print(f"WARNING: {CLIENTS_YAML} not found, skipping client-token migration.")
        return 0
    data = yaml.safe_load(CLIENTS_YAML.read_text()) or {}
    clients = data.get("clients", {})
    migrated = 0
    for name, cfg in clients.items():
        token = (cfg or {}).get("token")
        if not token:
            continue
        secret_name = f"client_token.{name}"
        if con.execute("SELECT 1 FROM secrets WHERE name = ?", (secret_name,)).fetchone():
            print(f"  skip {name}: already migrated")
            continue
        if dry_run:
            print(f"  would migrate token for client '{name}'")
            migrated += 1
            continue
        enc = fernet.encrypt(token.encode("utf-8"))
        con.execute(
            "INSERT INTO secrets (name, value_encrypted, updated_at) VALUES (?, ?, ?)",
            (secret_name, enc, datetime.now(timezone.utc).isoformat())
        )
        del cfg["token"]
        migrated += 1
        print(f"  migrated token for client '{name}'")
    if migrated and not dry_run:
        con.commit()
        CLIENTS_YAML.write_text(yaml.safe_dump(data, sort_keys=False))
        print(f"Updated {CLIENTS_YAML} (removed migrated token: fields).")
    return migrated


def create_admin_user(con: sqlite3.Connection, username: str, dry_run: bool):
    if con.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        print(f"User '{username}' already exists, skipping.")
        return
    if dry_run:
        print(f"Would prompt to create admin user '{username}'.")
        return
    pw1 = getpass.getpass(f"Password for new admin user '{username}': ")
    pw2 = getpass.getpass("Confirm password: ")
    if pw1 != pw2:
        sys.exit("Passwords did not match, aborting.")
    if len(pw1) < 8:
        sys.exit("Password must be at least 8 characters, aborting.")
    pw_hash = bcrypt.hashpw(pw1.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    con.execute(
        "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, 'admin', ?)",
        (username, pw_hash, datetime.now(timezone.utc).isoformat())
    )
    con.commit()
    print(f"Created admin user '{username}'.")


def create_service_key(con: sqlite3.Connection, owner_name: str, dry_run: bool):
    existing = con.execute(
        "SELECT key_id FROM api_keys WHERE owner_type='service' AND owner_name=? AND disabled=0",
        (owner_name,)
    ).fetchone()
    if existing:
        print(f"Service key for '{owner_name}' already exists (key_id={existing[0]}), skipping.")
        print("(Disable it first via DELETE /admin/api_keys/<key_id> if you need a fresh one.)")
        return
    if dry_run:
        print(f"Would create a service API key for '{owner_name}'.")
        return
    key_id = "kc_" + secrets_mod.token_hex(6)
    secret = secrets_mod.token_urlsafe(32)
    secret_hash = bcrypt.hashpw(secret.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    con.execute(
        "INSERT INTO api_keys (key_id, secret_hash, owner_type, owner_name, role, created_at) "
        "VALUES (?, ?, 'service', ?, 'admin', ?)",
        (key_id, secret_hash, owner_name, datetime.now(timezone.utc).isoformat())
    )
    con.commit()
    print(f"\nCreated service API key for '{owner_name}':")
    print(f"  LLMPROXY_SERVICE_KEY={key_id}.{secret}")
    print("This is shown ONCE and cannot be recovered. Add that line to /opt/llmproxy/.env")
    print("and run `docker compose up -d` in /opt/llmproxy to pick it up.\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="Show what would happen, change nothing.")
    ap.add_argument("--admin-username", default="admin", help="Username for the seed admin account (default: admin).")
    ap.add_argument("--skip-client-tokens", action="store_true", help="Skip the clients.yaml token migration.")
    ap.add_argument("--skip-admin-user", action="store_true", help="Skip creating the seed admin user.")
    ap.add_argument("--skip-service-key", action="store_true", help="Skip creating the dashboard service key.")
    args = ap.parse_args()

    con = _db()
    fernet = _fernet()

    print("=== llmproxy auth migration ===")
    if args.dry_run:
        print("(dry run -- no changes will be made)")

    if not args.skip_client_tokens:
        print("\n-- Client bearer tokens (clients.yaml -> encrypted secrets table) --")
        migrate_client_tokens(con, fernet, args.dry_run)

    if not args.skip_admin_user:
        print("\n-- Seed admin user --")
        create_admin_user(con, args.admin_username, args.dry_run)

    if not args.skip_service_key:
        print("\n-- Dashboard service API key --")
        create_service_key(con, "dashboard-internal", args.dry_run)

    con.close()
    print("=== done ===")


if __name__ == "__main__":
    main()
