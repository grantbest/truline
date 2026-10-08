"""The one Python client for the substrate's BeadStore surface.

The union of ``apps/factory-dispatcher/substrate.py`` and
``scripts/substrate_client.py`` behind the 11-method ``BeadStore`` protocol
(``apps/factory-dispatcher/beadstore.py``), plus ``SubstrateReader``. See
``docs/audits/2026-09-12-architecture-review-modularity-and-contracts.md``
§4/§6.
"""

from .client import DEV_NAMESPACE, HTTP_TIMEOUT_S, MAX_PAGES, Substrate, SubstrateError
from .reader import SubstrateReader, reader

__all__ = [
    "DEV_NAMESPACE",
    "HTTP_TIMEOUT_S",
    "MAX_PAGES",
    "Substrate",
    "SubstrateError",
    "SubstrateReader",
    "reader",
]
