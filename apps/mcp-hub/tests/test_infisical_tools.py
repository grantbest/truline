import json

import pytest

from tools import infisical


@pytest.fixture
def writer_env(monkeypatch):
    monkeypatch.setenv("INFISICAL_MACHINE_CLIENT_ID", "mi-client")
    monkeypatch.setenv("INFISICAL_MACHINE_CLIENT_SECRET", "mi-secret")
    monkeypatch.setenv("INFISICAL_PROJECT_ID", "proj-123")
    monkeypatch.delenv("INFISICAL_ENV_SLUG", raising=False)
    monkeypatch.delenv("INFISICAL_HOST_API", raising=False)
    monkeypatch.delenv("PLAID_ENV", raising=False)


def test_write_not_configured_without_identity(monkeypatch):
    for var in (
        "INFISICAL_MACHINE_CLIENT_ID",
        "INFISICAL_MACHINE_CLIENT_SECRET",
        "INFISICAL_PROJECT_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    assert infisical.write_configured() is False


def test_write_configured_with_identity(writer_env):
    assert infisical.write_configured() is True


@pytest.mark.asyncio
async def test_write_secret_raises_when_unconfigured(monkeypatch):
    monkeypatch.delenv("INFISICAL_MACHINE_CLIENT_ID", raising=False)
    monkeypatch.delenv("INFISICAL_MACHINE_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("INFISICAL_PROJECT_ID", raising=False)
    with pytest.raises(RuntimeError, match="not configured"):
        await infisical.write_secret("K", "v")


@pytest.mark.asyncio
async def test_write_secret_updates_existing(writer_env, httpx_mock):
    httpx_mock.add_response(
        url="https://app.infisical.com/api/v1/auth/universal-auth/login",
        method="POST",
        json={"accessToken": "at-1"},
    )
    httpx_mock.add_response(
        url="https://app.infisical.com/api/v4/secrets/PLAID_ACCESS_TOKEN_SOFI",
        method="PATCH",
        json={"secret": {}},
    )

    await infisical.write_secret("PLAID_ACCESS_TOKEN_SOFI", "access-sandbox-1")

    patch_req = httpx_mock.get_request(method="PATCH")
    body = json.loads(patch_req.read())
    assert body["projectId"] == "proj-123"
    assert body["environment"] == "dev"  # PLAID_ENV unset -> dev
    assert body["secretValue"] == "access-sandbox-1"
    assert patch_req.headers["Authorization"] == "Bearer at-1"


@pytest.mark.asyncio
async def test_write_secret_creates_on_404(writer_env, httpx_mock):
    httpx_mock.add_response(
        url="https://app.infisical.com/api/v1/auth/universal-auth/login",
        method="POST",
        json={"accessToken": "at-1"},
    )
    httpx_mock.add_response(
        url="https://app.infisical.com/api/v4/secrets/PLAID_ACCESS_TOKEN_CITI",
        method="PATCH",
        status_code=404,
        json={"message": "not found"},
    )
    httpx_mock.add_response(
        url="https://app.infisical.com/api/v4/secrets/PLAID_ACCESS_TOKEN_CITI",
        method="POST",
        json={"secret": {}},
    )

    await infisical.write_secret("PLAID_ACCESS_TOKEN_CITI", "access-sandbox-2")

    create_reqs = [
        r for r in httpx_mock.get_requests(method="POST")
        if r.url.path.endswith("/v4/secrets/PLAID_ACCESS_TOKEN_CITI")
    ]
    assert len(create_reqs) == 1
    assert json.loads(create_reqs[0].read())["secretValue"] == "access-sandbox-2"


@pytest.mark.asyncio
async def test_write_secret_env_slug_follows_plaid_env(writer_env, monkeypatch, httpx_mock):
    """Prod pods (PLAID_ENV=production) must write to the Infisical prod
    env without any kustomize patch — B-092 taught us not to rely on one."""
    monkeypatch.setenv("PLAID_ENV", "production")
    httpx_mock.add_response(
        url="https://app.infisical.com/api/v1/auth/universal-auth/login",
        method="POST",
        json={"accessToken": "at-1"},
    )
    httpx_mock.add_response(
        url="https://app.infisical.com/api/v4/secrets/K",
        method="PATCH",
        json={"secret": {}},
    )

    await infisical.write_secret("K", "v")

    body = json.loads(httpx_mock.get_request(method="PATCH").read())
    assert body["environment"] == "prod"
