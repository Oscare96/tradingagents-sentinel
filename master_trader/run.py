"""One-process launch for Replit Reserved VM and local development."""

import os

import uvicorn

if __name__ == "__main__":
    if os.getenv("REPLIT_DEPLOYMENT") and not os.getenv("MASTER_DATABASE_URL", "").startswith(
        ("postgres://", "postgresql://", "postgresql+psycopg://")
    ):
        raise SystemExit(
            "Deployment requires MASTER_DATABASE_URL pointing to persistent PostgreSQL"
        )
    if not os.getenv("DASHBOARD_TOKEN"):
        raise SystemExit("Set DASHBOARD_TOKEN before starting the dashboard")
    uvicorn.run(
        "master_trader.app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), workers=1
    )
