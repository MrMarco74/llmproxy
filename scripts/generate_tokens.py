#!/usr/bin/env python3
"""Generate one bearer token per client_id in config/clients.yaml directly.

Usage: python3 scripts/generate_tokens.py [--force]
  --force   regenerate tokens even for client_ids that already have one
            (existing tokens are kept by default so re-running this doesn't rotate
            everything and break already-deployed apps).

This only touches local files under config/ — it does not deploy or restart anything.
"""
import secrets
import sys
from pathlib import Path

import yaml

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def main():
    force = "--force" in sys.argv

    clients_path = CONFIG_DIR / "clients.yaml"
    if not clients_path.exists():
        print(f"Error: {clients_path} not found.")
        sys.exit(1)

    clients_cfg = yaml.safe_load(clients_path.read_text()) or {"clients": {}}
    clients = clients_cfg.get("clients", {})
    
    generated_count = 0

    for client_id, client_data in clients.items():
        if client_id == "default":
            continue  # "default" is the IP-fallback bucket, not a real app — no token
        
        has_token = "token" in client_data and client_data["token"]
        
        if not force and has_token:
            continue
            
        new_token = secrets.token_urlsafe(32)
        client_data["token"] = new_token
        print(f"{client_id}: {new_token}")
        generated_count += 1

    if generated_count > 0:
        clients_path.write_text(yaml.safe_dump(clients_cfg, default_flow_style=False))
        print(f"\nWrote {generated_count} new token(s) to {clients_path}")
    else:
        print("No new tokens needed. All clients already have tokens (use --force to rotate all).")


if __name__ == "__main__":
    main()
