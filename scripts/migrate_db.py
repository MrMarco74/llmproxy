#!/usr/bin/env python3
import sqlite3
from pathlib import Path

DB_PATH = Path("/var/lib/llmproxy/llmproxy.db")

def migrate():
    con = sqlite3.connect(str(DB_PATH))
    
    tables_to_rename = ["requests", "failures", "budgets", "client_profiles", "admin_actions"]
    
    for table in tables_to_rename:
        try:
            con.execute(f"ALTER TABLE {table} RENAME COLUMN client_ip TO token_name")
            print(f"Renamed client_ip to token_name in {table}")
        except sqlite3.OperationalError as e:
            print(f"Skipping rename in {table}: {e}")
            
    new_cols = ["project TEXT", "org_group TEXT", "user TEXT"]
    for col in new_cols:
        try:
            con.execute(f"ALTER TABLE requests ADD COLUMN {col}")
            print(f"Added column {col} to requests")
        except sqlite3.OperationalError as e:
            print(f"Skipping adding {col}: {e}")
            
    con.commit()
    con.close()
    print("Migration finished.")

if __name__ == "__main__":
    migrate()
