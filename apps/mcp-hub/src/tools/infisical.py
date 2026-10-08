"""Server-side Infisical secret writes (Plaid plan PR 6, Option A).

/finance/exchange uses this to save PLAID_ACCESS_TOKEN_* directly after a
Plaid Link exchange, so the operator never copies a token through the
browser/clipboard/Infisical UI. The existing delivery pipeline is
unchanged: Infisical -> ESO -> K8s Secret -> env (Reloader restarts pods
on change, PR 7).

Auth is a dedicated *write-scoped* Universal Auth machine identity —
deliberately NOT the read identity ESO uses. Its credentials arrive via
their own optional ExternalSecret (mcp-hub-infisical-writer), so their
absence degrades to the manual copy/paste flow instead of blocking the
main mcp-hub-secrets sync (the B-097 lesson: one missing key stalls an
entire ExternalSecret).

API shapes verified against https://infisical.com/docs/api-reference
(2026-06-11):
  POST  /api/v1/auth/universal-auth/login  {clientId, clientSecret}
        -> {accessToken, ...}
  PATCH /api/v4/secrets/{name}  {projectId, environment, secretValue, secretPath}
        -> 404 when the secret doesn't exist yet
  POST  /api/v4/secrets/{name}  (same body) to create

The Option B end-state (dedicated walled-off token vault) replaces this
module entirely — see docs/plans/plaid-token-vault.md.
"""

import logging
import os
from typing import Dict, Optional

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 15.0


def _config() -> Optional[Dict[str, str]]:
    """Writer config from env, or None when not provisioned.

    INFISICAL_ENV_SLUG defaults from PLAID_ENV (production -> prod, else
    dev) so the prod overlay needs no extra kustomize patch — the same
    class of patch that silently no-op'd in B-092.
    """
    client_id = os.environ.get("INFISICAL_MACHINE_CLIENT_ID")
    client_secret = os.environ.get("INFISICAL_MACHINE_CLIENT_SECRET")
    project_id = os.environ.get("INFISICAL_PROJECT_ID")
    if not (client_id and client_secret and project_id):
        return None
    default_env = "prod" if os.environ.get("PLAID_ENV") == "production" else "dev"
    return {
        "host_api": os.environ.get(
            "INFISICAL_HOST_API", "https://app.infisical.com/api"
        ).rstrip("/"),
        "client_id": client_id,
        "client_secret": client_secret,
        "project_id": project_id,
        "environment": os.environ.get("INFISICAL_ENV_SLUG", default_env),
        "secret_path": os.environ.get("INFISICAL_SECRET_PATH", "/"),
    }


def write_configured() -> bool:
    """True when the write-scoped machine identity is provisioned."""
    return _config() is not None


async def write_secret(name: str, value: str, comment: str = "") -> None:
    """Create-or-update one secret in the configured project/environment.

    Update-first: once an institution is linked, re-links are the common
    case. Raises on any failure — the caller decides whether that is
    fatal (it isn't for /finance/exchange, which falls back to showing
    the token for manual entry).
    """
    cfg = _config()
    if cfg is None:
        raise RuntimeError(
            "Infisical writer not configured "
            "(INFISICAL_MACHINE_CLIENT_ID/SECRET, INFISICAL_PROJECT_ID)."
        )

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        login = await client.post(
            f"{cfg['host_api']}/v1/auth/universal-auth/login",
            json={
                "clientId": cfg["client_id"],
                "clientSecret": cfg["client_secret"],
            },
        )
        login.raise_for_status()
        token = login.json()["accessToken"]

        headers = {"Authorization": f"Bearer {token}"}
        body = {
            "projectId": cfg["project_id"],
            "environment": cfg["environment"],
            "secretPath": cfg["secret_path"],
            "secretValue": value,
        }
        if comment:
            body["secretComment"] = comment

        url = f"{cfg['host_api']}/v4/secrets/{name}"
        resp = await client.patch(url, json=body, headers=headers)
        if resp.status_code == 404:
            resp = await client.post(url, json=body, headers=headers)
        resp.raise_for_status()
        logger.info(
            "infisical secret written: name=%s env=%s path=%s",
            name, cfg["environment"], cfg["secret_path"],
        )
