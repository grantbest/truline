"""Per-namespace extension points the core owns.

A namespace the core does not define directly -- ``finance`` is the one that
exists today -- can contribute a router and an integrity-error mapper here,
from its own module, on import. Mirrors ``schemas.NAMESPACE_TYPE_SCHEMAS``:
no core module (``main.py``, ``routes.py``) names a namespace's router or
mapper directly, and there is no discovery or dynamic import -- registration
is the only way an entry ever appears here.

Encryption policy is not one of these registries -- see
``crypto.register_encrypted_namespace``. Migrations 0005/0007 import
``crypto.ENCRYPTED_NAMESPACES`` standalone, with no app composition root in
the loop (``alembic upgrade`` never imports ``main.py``), so that hook has to
fire the moment ``crypto.py`` itself is imported, not merely when this
registry's consumers (``main.py``, ``routes.py``) are.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from sqlalchemy.exc import IntegrityError

IntegrityErrorMapper = Callable[[IntegrityError], Optional[HTTPException]]


class NamespaceRouterRegistry:
    """Routers a namespace outside the core mounts on the running app.

    Registration appends rather than replaces: a namespace may contribute
    more than one router (finance does -- its summary endpoint and the
    rules dry-run router are two separate ``APIRouter`` instances today).

    ``mount_pending`` -- not a plain "loop over ``all()`` and include" at the
    call site -- exists because of a real ordering hazard: whichever of
    ``routes.py`` / ``finance_schemas.py`` is the OTHER's trigger in a given
    import (Python resolves the cycle differently depending on which side a
    caller imports first), a naive read-then-nest at import time can run
    before the namespace has finished registering. Tracking what has already
    been mounted, by identity, makes calling this repeatedly -- from
    whichever side actually knows registration is complete -- safe.
    """

    def __init__(self) -> None:
        self._routers: Dict[str, List[APIRouter]] = {}
        self._mounted_ids: set[int] = set()

    def register(self, namespace: str, router: APIRouter) -> None:
        self._routers.setdefault(namespace, []).append(router)

    def for_namespace(self, namespace: str) -> List[APIRouter]:
        return list(self._routers.get(namespace, []))

    def all(self) -> List[APIRouter]:
        return [router for routers in self._routers.values() for router in routers]

    def mount_pending(self, target: APIRouter) -> None:
        """Nest every registered router not yet mounted into ``target``."""
        for router in self.all():
            if id(router) not in self._mounted_ids:
                target.include_router(router)
                self._mounted_ids.add(id(router))


class IntegrityErrorMapperRegistry:
    """Per-namespace translation from a DB ``IntegrityError`` to an HTTP response.

    One mapper per namespace, matching the module-level dict literal this
    kind of registry replaces elsewhere in this codebase (``schemas.py``'s
    ``NamespaceSchemaRegistry``) -- a second registration for the same
    namespace overwrites the first.
    """

    def __init__(self) -> None:
        self._mappers: Dict[str, IntegrityErrorMapper] = {}

    def register(self, namespace: str, mapper: IntegrityErrorMapper) -> None:
        self._mappers[namespace] = mapper

    def get(self, namespace: str) -> Optional[IntegrityErrorMapper]:
        return self._mappers.get(namespace)

    def all(self) -> List[IntegrityErrorMapper]:
        return list(self._mappers.values())


NAMESPACE_ROUTERS = NamespaceRouterRegistry()
NAMESPACE_INTEGRITY_ERROR_MAPPERS = IntegrityErrorMapperRegistry()


__all__ = [
    "IntegrityErrorMapper",
    "IntegrityErrorMapperRegistry",
    "NamespaceRouterRegistry",
    "NAMESPACE_ROUTERS",
    "NAMESPACE_INTEGRITY_ERROR_MAPPERS",
]
