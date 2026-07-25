#!/usr/bin/env python3
"""Generate one bearer token per client_id in config/clients.yaml and write config/tokens.yaml.

Usage: python3 scripts/generate_tokens.py [--force]
  --force   regenerate tokens even for client_ids that already have one in tokens.yaml
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
    tokens_path = CONFIG_DIR / "tokens.yaml"

    clients_cfg = yaml.safe_load(clients_path.read_text()) or {"clients": {}}
    tokens_cfg = yaml.safe_load(tokens_path.read_text()) or {"tokens": {}}
    tokens = tokens_cfg.get("tokens", {}) or {}

    existing_by_client = {v: k for k, v in tokens.items()}

    for client_id in clients_cfg.get("clients", {}):
        if client_id == "default":
            continue  # "default" is the IP-fallback bucket, not a real app — no token
        if not force and client_id in existing_by_client:
            continue
        if client_id in existing_by_client:
            del tokens[existing_by_client[client_id]]
        new_token = secrets.token_urlsafe(32)
        tokens[new_token] = client_id
        print(f"{client_id}: {new_token}")

    tokens_path.write_text(yaml.safe_dump({"tokens": tokens}, default_flow_style=False))
    print(f"\nWrote {len(tokens)} token(s) to {tokens_path}")


if __name__ == "__main__":
    main()
