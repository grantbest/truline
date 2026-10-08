"""Runtime configuration for the factory dispatcher Temporal worker."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Sequence
from typing import Mapping

DEFAULT_DISPATCH_INTERVAL_SECONDS = 15 * 60

# The configuration the dispatcher cannot do its job without, declared once.
#
# Derived from what the code actually reads: TEMPORAL_URL below, SUBSTRATE_URL
# / SUBSTRATE_API_KEY in substrate.py, and FACTORY_REPO / FACTORY_REMOTE in
# dispatch.py's Config.from_env() (R2603-5: these named a household's actual
# GitHub repository as a compiled-in default; a household with a different
# repository could not run this code as-is). Everything else the dispatcher
# reads carries a default and is therefore not required.
#
# This lives here rather than in launchd_agent.py because the installer is not
# the only caller that has to answer the question. A second copy would drift,
# and the copy that drifts is the one doing the gating.
REQUIRED_CONFIG = (
    "SUBSTRATE_URL",
    "SUBSTRATE_API_KEY",
    "TEMPORAL_URL",
    "FACTORY_REPO",
    "FACTORY_REMOTE",
    # OPS-109 (#789 gate F1): the deployed-revision drift check's kubectl target.
    # Optional would let the nightly leg skip silently and report itself green.
    "FACTORY_DEPLOYED_REVISION_NAMESPACE",
    "FACTORY_DEPLOYED_REVISION_DEPLOYMENT",
)

# The worker dependency surface, declared once so each new import or shell-out
# has an obvious registration point. Worker binaries are NOT listed here:
# they derive from WORKER_REGISTRY's dispatchable entries (Amendment 30 PR-4,
# dispatch.required_worker_executables), so a retired or quarantined worker's
# binary is never demanded of a host that can no longer select it.
REQUIRED_WORKER_IMPORTS = ("httpx", "temporalio")
BASE_WORKER_EXECUTABLES = ("gh", "git", "sandbox-exec", "temporal")


def missing_required_config(values: Mapping[str, str]) -> list[str]:
    """Names from REQUIRED_CONFIG that are absent or empty in `values`."""
    return [name for name in REQUIRED_CONFIG if not values.get(name)]


ExecutableResolver = Callable[[str, str], str | None]


def resolve_executable(name: str, path: str) -> str | None:
    """Return the executable path for `name` on `path`, without running it."""
    return shutil.which(name, path=path)


def worker_executable_search_path(values: Mapping[str, str]) -> str:
    """The PATH Python subprocess lookups will use in this worker environment."""
    return values.get("PATH", os.defpath)


def missing_required_worker_executables(
    values: Mapping[str, str],
    *,
    required: Sequence[str],
    resolver: ExecutableResolver = resolve_executable,
) -> list[str]:
    """Names from `required` that are not resolvable executable files on PATH."""
    path = worker_executable_search_path(values)
    return [name for name in required if resolver(name, path) is None]


def missing_worker_executables_diagnosis(
    missing: Sequence[str],
    values: Mapping[str, str],
) -> str:
    path = worker_executable_search_path(values)
    searched = path if path else "(empty)"
    return (
        f"Missing required executable{'s' if len(missing) != 1 else ''}: "
        f"{', '.join(missing)}. PATH searched: {searched}"
    )


def _positive_int_from_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer number of seconds") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


class Config:
    TEMPORAL_NAMESPACE = os.environ.get("TEMPORAL_NAMESPACE", "dev")
    DISPATCH_SCHEDULE_ID = os.environ.get(
        "FACTORY_DISPATCH_SCHEDULE_ID",
        "factory-dispatcher-dev",
    )

    @property
    def TEMPORAL_URL(self) -> str:
        return os.environ["TEMPORAL_URL"]

    @property
    def DISPATCH_SCHEDULE_INTERVAL_SECONDS(self) -> int:
        return _positive_int_from_env(
            "FACTORY_DISPATCH_INTERVAL_SECONDS",
            DEFAULT_DISPATCH_INTERVAL_SECONDS,
        )


config = Config()
