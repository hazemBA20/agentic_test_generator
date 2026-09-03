"""Resolve what credentials an operation needs, from the spec itself.

Mirrors ``plan_validation.py``/``coverage.py``: pure functions over the
operation wrapper, no I/O, no LLM. The result is backfilled onto every plan
by the builder (like method/path/content_type), and the runtime attaches the
configured credential (helpers/auth.py). Auth is satisfied, never tested:
401/403 stay in UNREACHABLE_STATUSES and the planner is told not to plan
auth scenarios.

Decision table (see tests/test_auth.py for each row):
- operation ``security: []``                      -> public, by the spec
- ``type: apiKey, in: header``                    -> api_key, header from the scheme
- ``type: apiKey, in: query/cookie``              -> unsupported in v1
- ``type: http, scheme: bearer``                  -> bearer
- ``type: http, scheme: basic``                   -> basic
- ``type: oauth2 / openIdConnect``                -> bearer via token env/login
- several of the above required together          -> multi (flags combine on the plan)
- anything else (mutualTLS, ...)                  -> unsupported
- spec silent about security                      -> api_key (the tool's original
  behavior; a redundant header on a public endpoint is harmless, omitting
  required credentials is not)
"""
from typing import Any


def _scheme_kind(scheme: dict) -> tuple[str, dict[str, Any]] | None:
    """(kind, extras) for one security scheme definition, or None if unsupported."""
    scheme_type = scheme.get("type")
    if scheme_type == "apiKey":
        if scheme.get("in") != "header":
            return None
        return "api_key", {"header": scheme.get("name") or "X-API-KEY"}
    if scheme_type == "http":
        layout = str(scheme.get("scheme") or "").lower()
        if layout == "bearer":
            return "bearer", {}
        if layout == "basic":
            return "basic", {}
        return None
    if scheme_type in {"oauth2", "openIdConnect"}:
        # Token acquisition is delegated to the runtime's static token or login
        # flow; the spec's flow metadata is not exercised in v1.
        return "bearer", {}
    return None


def resolve_operation_auth(wrapper: dict) -> dict:
    """Decide how requests to this operation must authenticate.

    Takes the operation wrapper (path/method/operation/security_schemes).
    Returns ``{"kind": "none"|"api_key"|"bearer"|"basic", ...}`` or
    ``{"kind": "unsupported", "reason": ...}``.
    """
    operation = wrapper.get("operation") or {}
    schemes = wrapper.get("security_schemes") or {}
    security = operation.get("security")

    if security is not None:
        if not isinstance(security, list):
            return {"kind": "unsupported", "reason": f"malformed security: {security!r}"}
        if not security:
            # An explicitly empty security list means the operation is public.
            return {"kind": "none"}

        reason = "no satisfiable security requirement"
        for alternative in security:
            if not isinstance(alternative, dict):
                reason = f"malformed security alternative: {alternative!r}"
                continue
            if not alternative:
                # An empty requirement object allows anonymous access.
                return {"kind": "none"}
            resolved: list[tuple[str, dict[str, Any]]] = []
            for name in alternative:
                scheme = schemes.get(name) if isinstance(schemes, dict) else None
                if not isinstance(scheme, dict):
                    reason = f"security scheme {name!r} is not defined"
                    resolved = []
                    break
                kinded = _scheme_kind(scheme)
                if kinded is None:
                    reason = (
                        f"unsupported security scheme {name!r} "
                        f"(type {scheme.get('type')!r}, in {scheme.get('in')!r})"
                    )
                    resolved = []
                    break
                resolved.append(kinded)
            if not resolved:
                continue
            if len(resolved) == 1:
                kind, extras = resolved[0]
                return {"kind": kind, **extras}
            # Several credentials required together (e.g. bearer + api key):
            # supported — the plan flags are independent booleans, and the
            # runtime attaches whichever credentials are actually configured.
            header = next(
                (extra.get("header") for _, extra in resolved if extra.get("header")),
                None,
            )
            return {
                "kind": "multi",
                "kinds": [kind for kind, _ in resolved],
                "header": header or "X-API-KEY",
            }
        return {"kind": "unsupported", "reason": reason}

    # Spec is silent: keep the tool's original behavior (attach the configured
    # API key), preferring a declared header name when one exists.
    header = "X-API-KEY"
    if isinstance(schemes, dict):
        for scheme in schemes.values():
            if isinstance(scheme, dict) and scheme.get("type") == "apiKey" and scheme.get("in") == "header":
                header = scheme.get("name") or header
                break
    return {"kind": "api_key", "header": header}
