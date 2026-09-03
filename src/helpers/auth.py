"""Credential resolution for plan and generated-test execution.

Credentials live in the environment, never in plans or generated files:
- DIGIEXPERT_API_KEY                     API key (name kept for compatibility)
- AUTH_TOKEN (or legacy API_JWT /
  DIGIEXPERT_JWT)                        static bearer token
- AUTH_TOKEN_URL + AUTH_USERNAME +
  AUTH_PASSWORD                          one login per process when no static
                                         token is set; the token is read from
                                         access_token / token / jwt in the reply
- AUTH_BASIC_USERNAME + AUTH_BASIC_PASSWORD    HTTP basic auth

Environment lookups happen at request time, not import time, so a variable
exported after this module loads still counts.
"""
import os

import requests
from dotenv import load_dotenv

from pathlib import Path

# This module can be imported before the workflow's own load_dotenv() runs, so
# it loads .env itself — otherwise a variable set only in .env is invisible.
load_dotenv(Path(__file__).resolve().parents[2] / ".env")
load_dotenv()

_cached_bearer: str | None = None


def api_key() -> str | None:
    return os.environ.get("DIGIEXPERT_API_KEY")


def _static_bearer() -> str | None:
    for name in ("AUTH_TOKEN", "API_JWT", "DIGIEXPERT_JWT"):
        value = os.environ.get(name)
        if value:
            return value
    return None


def bearer_token() -> str:
    """A bearer token: static from the env if set, else one login per process."""
    global _cached_bearer
    if _cached_bearer:
        return _cached_bearer

    static = _static_bearer()
    if static:
        _cached_bearer = static
        return static

    token_url = os.environ.get("AUTH_TOKEN_URL")
    username = os.environ.get("AUTH_USERNAME")
    password = os.environ.get("AUTH_PASSWORD")
    if not (token_url and username and password):
        raise RuntimeError(
            "A bearer-protected operation needs a token: set AUTH_TOKEN, or "
            "AUTH_TOKEN_URL with AUTH_USERNAME and AUTH_PASSWORD to log in."
        )

    response = requests.request(
        "POST", token_url, json={"username": username, "password": password}, timeout=15,
    )
    if response.status_code // 100 != 2:
        raise RuntimeError(
            f"Bearer login failed: HTTP {response.status_code} from {token_url}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Bearer login response from {token_url} is not JSON; cannot extract a token"
        ) from exc
    token = next(
        (payload[name] for name in ("access_token", "token", "jwt") if payload.get(name)),
        None,
    )
    if not token:
        raise RuntimeError(
            f"Bearer login response from {token_url} has no access_token/token/jwt field"
        )
    _cached_bearer = str(token)
    return _cached_bearer


def basic_credentials() -> tuple[str, str]:
    username = os.environ.get("AUTH_BASIC_USERNAME")
    password = os.environ.get("AUTH_BASIC_PASSWORD")
    if not (username and password):
        raise RuntimeError(
            "Basic-auth-protected operations need AUTH_BASIC_USERNAME and "
            "AUTH_BASIC_PASSWORD."
        )
    return username, password
