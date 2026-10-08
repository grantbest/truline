FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    procps \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

# Paths are relative to the REPO ROOT: build-mcp-hub.yml builds with
# `-f apps/mcp-hub/Dockerfile .` (OPS-112) so later images can COPY trees
# outside apps/mcp-hub/. The image content is unchanged by that move.
COPY apps/mcp-hub/pyproject.toml ./
COPY apps/mcp-hub/src/ ./src/

RUN uv pip install --system -e .

# M9: the dispatcher's own filing capability (tools/task_filing.py calls
# file_task.file_spec from this tree, never a re-derived filing path —
# PRIN-005). Preserves each tree's checkout-relative nesting under apps/ —
# unlike apps/mcp-hub/src above, deliberately flattened to ./src — because
# file_task.py, failure_diagnosis.py (imported by dispatch.py),
# substrate_client_loader.py and scripts/substrate_client.py each derive a
# SIBLING path from their own __file__ location (scripts/, apps/substrate/),
# and those derivations only resolve correctly when this layout matches a
# real checkout exactly. See .factory/design.md ("Discovery 2") for the full
# analysis, including the one such derivation (failure_diagnosis.py's own)
# that resolves to a path this image does NOT have and why that is harmless
# here. tests/ and apps/factory-dispatcher/tasks/ are excluded from the build
# context by the root .dockerignore already (OPS-112), not by anything here.
COPY apps/factory-dispatcher/ ./apps/factory-dispatcher/
COPY scripts/ ./scripts/
COPY apps/substrate/src/ ./apps/substrate/src/
COPY apps/substrate/client/ ./apps/substrate/client/

# The requirements registry, admitted to the build context by the root .dockerignore.
# file_task.py:61 resolves REQUIREMENTS_DIR to parents[2]/docs/requirements -- /app/docs/
# requirements here -- and _check_traceability runs BEFORE any substrate call, so without
# this the HTTP intake 422s every spec citing a real requirement (#909 gate, F1).
# TRADE-OFF, STATED: this is a BUILD-TIME SNAPSHOT. A requirement added after the last
# mcp-hub build is unresolvable until the next one. The durable fix is to resolve
# requirement refs from the substrate the way _check_release_traceability already resolves
# release refs (file_task.py:140,148); that is a follow-up bead, not this image's job.
COPY docs/requirements/ ./docs/requirements/

# The one runtime dependency the dispatcher tree needs beyond what mcp-hub's
# own pyproject already installs above (httpx, temporalio, pydantic):
# activities/change_apply.py and activities/ea_apply.py both `import yaml` at
# module scope, and activities/__init__.py imports both unconditionally —
# reached from a plain `import dispatch`. Pinned to match
# apps/factory-dispatcher/requirements.txt's own "PyYAML>=6.0".
RUN uv pip install --system "PyYAML>=6.0"

# Always set, unlike tools/factory_status.py's FACTORY_STATUS_REPO_ROOT (that
# one is optional by design — this image never ships that checkout, and
# degrading to "unknown" is the correct, permanent state for it). A filing
# route that degrades the same way files nothing (the #781 gate finding this
# bead exists to not repeat) — tools/task_filing.py raises plainly if this is
# ever unset, because in THIS image that can only mean a real misconfiguration.
ENV FACTORY_DISPATCHER_ROOT=/app

# Build provenance, surfaced at runtime via GET /health -- see
# openapi_app.health for why. build-mcp-hub.yml passes `--build-arg
# GIT_SHA=$GITHUB_SHA` since #769 (2026-09-12); "unknown" now means a local
# or hand build, never a CI image.
#
# DELIBERATELY PLACED LAST, below every expensive layer. An ENV whose value
# changes invalidates the build cache for every instruction after it. While
# GIT_SHA is always "unknown" that costs nothing, but the moment the
# follow-up passes the real sha, EVERY merge would otherwise re-run
# apt-get, the uv install and a full dependency resolve -- against the
# 600s timeout on the self-hosted runner in build-mcp-hub.yml, degrading
# the very deploy path this change exists to unblock. Keep it here.
ARG GIT_SHA=unknown
ENV GIT_SHA=$GIT_SHA

EXPOSE 8000

# Serve tool functions as OpenAPI for Open WebUI's Tool Servers feature.
CMD ["uvicorn", "openapi_app:app", "--app-dir", "src", "--host", "0.0.0.0", "--port", "8000"]
