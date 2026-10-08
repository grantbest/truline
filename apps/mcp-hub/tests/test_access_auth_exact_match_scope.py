"""access_auth.EXACT_MATCH_SCOPES (R26.09/O-2): unlike every other scope,
factory.merge must not be satisfied by the human wildcard ('*', granted to
every authenticated person by identity_headers) or by 'admin'. See
access_auth.EXACT_MATCH_SCOPES's own docstring and .factory/design.md.

Unit-level, directly against check_scope/current_client_identity -- the HTTP
round trip through the actual route is covered separately in
test_factory_merge_router_access.py.
"""

import pytest
from fastapi import HTTPException

import access_auth
from access_auth import AccessIdentity, current_client_identity


@pytest.mark.parametrize("scopes", ["*", "admin", "*,admin", "factory.read,*"])
def test_exact_match_scope_rejects_the_wildcard_and_admin(scopes):
    token = current_client_identity.set(
        AccessIdentity(client="whoever", client_type="human", scopes=scopes)
    )
    try:
        with pytest.raises(HTTPException) as exc_info:
            access_auth.check_scope("factory.merge")
        assert exc_info.value.status_code == 403
    finally:
        current_client_identity.reset(token)


def test_exact_match_scope_accepts_only_the_literal_scope():
    token = current_client_identity.set(
        AccessIdentity(client="agent-merge", client_type="service", scopes="factory.merge")
    )
    try:
        access_auth.check_scope("factory.merge")  # must not raise
    finally:
        current_client_identity.reset(token)


def test_a_different_granted_scope_still_does_not_satisfy_it():
    token = current_client_identity.set(
        AccessIdentity(client="agent-dev", client_type="service", scopes="factory.write")
    )
    try:
        with pytest.raises(HTTPException) as exc_info:
            access_auth.check_scope("factory.merge")
        assert exc_info.value.status_code == 403
    finally:
        current_client_identity.reset(token)


def test_wildcard_is_unaffected_for_an_ordinary_scope():
    """Pins that the carve-out is scoped to EXACT_MATCH_SCOPES only -- every
    other scope's wildcard bypass is untouched by this change."""
    token = current_client_identity.set(
        AccessIdentity(client="whoever", client_type="human", scopes="*")
    )
    try:
        access_auth.check_scope("factory.write")  # must not raise
    finally:
        current_client_identity.reset(token)
