import yaml
from pathlib import Path

budget_file = Path("budget.yaml")
clients_file = Path("clients.yaml")

if budget_file.exists() and not clients_file.exists():
    with open(budget_file, "r") as f:
        budget = yaml.safe_load(f)
    clients = {"clients": {}}
    for k, v in budget.get("budgets", {}).items():
        clients["clients"][k] = {"limit": v, "models": "*", "blocked": False}
    with open(clients_file, "w") as f:
        yaml.safe_dump(clients, f)
    print("Migrated budget.yaml to clients.yaml")
else:
    print("Migration not needed or already done.")
