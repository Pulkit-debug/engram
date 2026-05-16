"""Spot-check the blast-radius output for hand-picked targets across the 6
benchmark DBs, so we can pin scenario expected-values to real outputs.

Usage: python tests/_scenario_spotcheck.py /tmp/engram_bench_out
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from engram.safety.blast_radius import assess


CASES = [
    # (alias, operation, target)
    ("vault-helm", "kubectl delete deployment", "postgres"),
    ("vault-helm", "helm uninstall", "vault"),
    ("vault-helm", "kubectl get pods", "nginx"),
    ("cert-manager", "helm uninstall", "cert-manager"),
    ("cert-manager", "kubectl delete deployment", "bind"),
    ("cert-manager", "kubectl apply", "cert-manager"),
    ("terraform-aws-eks", "terraform destroy", "cluster"),
    ("terraform-aws-eks", "terraform destroy", "karpenter"),
    ("terraform-aws-eks", "terraform plan", "cluster"),
    ("microservices-demo", "kubectl delete deployment", "frontend"),
    ("microservices-demo", "kubectl delete deployment", "paymentservice"),
    ("microservices-demo", "kubectl delete deployment", "redis-cart"),
    ("microservices-demo", "kubectl get pods", "frontend"),
    ("microservices-demo", "kubectl delete deployment", "checkoutservice"),
    ("awesome-compose", "docker compose down", "nginx"),
    ("awesome-compose", "docker compose down", "prometheus"),
    ("awesome-compose", "docker compose ps", "nginx"),
    ("helm-charts", "helm uninstall", "prometheus"),
    ("helm-charts", "helm uninstall", "grafana"),
]


def main() -> None:
    root = Path(sys.argv[1])
    print(f"{'alias':22s} {'op':30s} {'target':22s} -> {'tier':8s} {'action':10s} resources deps")
    print("-" * 110)
    for alias, op, target in CASES:
        db = root / alias / "engram.db"
        if not db.exists():
            print(f"{alias:22s} MISSING DB at {db}")
            continue
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        r = assess(conn, op, target)
        print(f"{alias:22s} {op:30s} {target:22s} -> "
              f"{r.risk_tier:8s} {r.action:10s} "
              f"{len(r.resolved_resources):>4} {len(r.dependents):>4}")
        conn.close()


if __name__ == "__main__":
    main()
