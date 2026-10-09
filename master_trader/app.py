from __future__ import annotations

import hmac
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .engine import Engine
from .models import Controls, timestamp
from .store import Store


def create_app(store=None, start_worker=True):
    engine = Engine(store or Store())

    @asynccontextmanager
    async def lifespan(app):
        if start_worker:
            engine.start()
        yield
        engine.stop()

    app = FastAPI(title="Master Trader", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.engine = engine

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    def authenticated(request: Request):
        expected = os.getenv("DASHBOARD_TOKEN", "")
        if not expected:
            raise HTTPException(
                503, "Set DASHBOARD_TOKEN in deployment secrets before accessing account data"
            )
        supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if not hmac.compare_digest(expected, supplied):
            raise HTTPException(401, "Dashboard access token required")

    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    def index():
        return FileResponse(static / "index.html")

    @app.get("/healthz")
    def health():
        return {"service": "master-trader", "status": "running", "live_execution": False}

    @app.get("/api/dashboard", dependencies=[Depends(authenticated)])
    def dashboard():
        return engine.dashboard()

    @app.put("/api/controls", dependencies=[Depends(authenticated)])
    def controls(value: Controls):
        try:
            engine.configure(value)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return engine.controls()

    @app.post("/api/scan", dependencies=[Depends(authenticated)])
    def scan():
        engine.scan_event.set()
        return {"status": "queued"}

    @app.post("/api/pause", dependencies=[Depends(authenticated)])
    def pause():
        value = engine.controls().model_copy(update={"state": "manage_only"})
        engine.configure(value)
        return {"status": "manage_only", "message": "New entries paused; exits continue"}

    @app.post("/api/close-positions", dependencies=[Depends(authenticated)])
    def close_positions():
        engine.configure(engine.controls().model_copy(update={"state": "manage_only"}))
        engine.store.set("close_requested", timestamp())
        engine.log("warning", "Owner requested position closure; waiting for executable quotes")
        return {
            "status": "queued",
            "message": "Entries paused. Owned paper positions will close when market and quotes permit.",
        }

    @app.post("/api/cancel-entries", dependencies=[Depends(authenticated)])
    def cancel_entries():
        engine.configure(engine.controls().model_copy(update={"state": "manage_only"}))
        engine.store.set("cancel_entries_requested", timestamp())
        engine.log("warning", "Owner requested cancellation of unfilled paper entries")
        return {"status": "queued"}

    @app.get("/api/export", dependencies=[Depends(authenticated)])
    def export():
        events = engine.store.list()
        # Export decision evidence, but never system prompts or provider credentials.
        events = [e for e in events if e["kind"] != "version"]
        return JSONResponse(
            {"format": "master-trader-journal-v1", "events": events},
            headers={"Content-Disposition": 'attachment; filename="master-trader-journal.json"'},
        )

    return app


app = create_app()
