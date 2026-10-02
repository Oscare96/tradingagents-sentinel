"""Local API-key storage.

Keys live in ``~/.config/tradingagents-scanner/keys.env`` (file mode 600,
readable only by you) and are loaded into ``os.environ`` when the dashboard
starts, so the scanner picks them up without any code changes.

Security rules:
- Keys are never logged and never printed.
- The API only ever returns masked versions (last 4 chars).
- This file is outside the repo, so keys can never be committed to git.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".config" / "tradingagents-scanner"
KEYS_FILE = CONFIG_DIR / "keys.env"

# Dashboard form field -> env var
KEY_MAP = {
    "alpaca_key": "APCA_API_KEY_ID",
    "alpaca_secret": "APCA_API_SECRET_KEY",
    "openai_key": "OPENAI_API_KEY",
    # FreeLLMAPI self-hosted gateway: the unified key from its dashboard Keys
    # page (http://localhost:3001). Routes deep-dive LLM calls across 34
    # providers' free tiers so testing doesn't burn paid tokens.
    "freellmapi_key": "FREELLMAPI_API_KEY",
}

# Deep-dive LLM provider choice. Persisted in the same local env file so the
# scanner picks it up on its next run (it reads TRADINGAGENTS_LLM_PROVIDER
# from the environment at scan time).
LLM_PROVIDER_ENV = "TRADINGAGENTS_LLM_PROVIDER"
LLM_PROVIDER_CHOICES = ("openai", "freellmapi")
# Model overrides applied when the provider is switched: FreeLLMAPI's router
# picks the backing model per request via "auto" targets.
FREELLMAPI_MODEL_ENVS = {
    "TRADINGAGENTS_DEEP_THINK_LLM": "auto",
    "TRADINGAGENTS_QUICK_THINK_LLM": "auto:fast",
}


def get_llm_provider() -> str:
    """Currently selected deep-dive LLM provider (defaults to openai)."""
    return os.environ.get(LLM_PROVIDER_ENV, "openai").lower()


def _ensure_perms() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if KEYS_FILE.exists():
        os.chmod(KEYS_FILE, 0o600)


def load_keys_into_env() -> dict[str, bool]:
    """Read the keys file and export into os.environ. Returns presence map."""
    _ensure_perms()
    present: dict[str, bool] = {}
    if KEYS_FILE.exists():
        for line in KEYS_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k and v:
                os.environ[k] = v
    for field, env in KEY_MAP.items():
        present[field] = bool(os.environ.get(env))
    return present


def save_keys(values: dict[str, str]) -> dict[str, bool]:
    """Save the provided keys (empty strings are ignored, existing kept)."""
    _ensure_perms()
    current = _read_env_file()
    for field, env in KEY_MAP.items():
        val = (values.get(field) or "").strip()
        if val:
            current[env] = val
            os.environ[env] = val
            logger.info("Updated stored key %s", env)
    _write_env_file(current)
    return {field: bool(os.environ.get(env)) for field, env in KEY_MAP.items()}


def _read_env_file() -> dict[str, str]:
    current: dict[str, str] = {}
    if KEYS_FILE.exists():
        for line in KEYS_FILE.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                current[k.strip()] = v.strip()
    return current


def _write_env_file(current: dict[str, str]) -> None:
    lines = ["# TradingAgents scanner API keys -- DO NOT SHARE OR COMMIT\n"]
    for env in KEY_MAP.values():
        if env in current:
            lines.append(f"{env}={current[env]}")
    if LLM_PROVIDER_ENV in current:
        lines.append(f"{LLM_PROVIDER_ENV}={current[LLM_PROVIDER_ENV]}")
    for env in FREELLMAPI_MODEL_ENVS:
        if env in current:
            lines.append(f"{env}={current[env]}")
    KEYS_FILE.write_text("\n".join(lines) + "\n")
    os.chmod(KEYS_FILE, 0o600)


def set_llm_provider(provider: str) -> str:
    """Persist the deep-dive LLM provider choice.

    Switching to ``freellmapi`` also sets the model overrides its router
    needs (``auto`` / ``auto:fast``); switching back removes them so the
    built-in defaults apply again. Returns the stored provider name.
    """
    provider = (provider or "").lower()
    if provider not in LLM_PROVIDER_CHOICES:
        raise ValueError(f"unknown LLM provider '{provider}'")
    _ensure_perms()
    current = _read_env_file()
    current[LLM_PROVIDER_ENV] = provider
    os.environ[LLM_PROVIDER_ENV] = provider
    if provider == "freellmapi":
        for env, val in FREELLMAPI_MODEL_ENVS.items():
            current[env] = val
            os.environ[env] = val
    else:
        for env in FREELLMAPI_MODEL_ENVS:
            current.pop(env, None)
            os.environ.pop(env, None)
    _write_env_file(current)
    logger.info("Deep-dive LLM provider set to %s", provider)
    return provider


def masked(field: str) -> str | None:
    """Masked display value, e.g. '••••abcd'. None if not set."""
    val = os.environ.get(KEY_MAP[field], "")
    if not val:
        return None
    return "•" * 8 + val[-4:]


def key_status() -> dict[str, dict]:
    return {
        field: {"connected": bool(os.environ.get(env)), "masked": masked(field)}
        for field, env in KEY_MAP.items()
    }
