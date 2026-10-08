"""Scheduled `arch.change` reconciler — a merged pull request writes the change record it is.

`ArchChangeContent` (`apps/substrate/src/schemas.py`) has existed since the EA metamodel landed
and nothing has ever written it. `docs/architecture/itsm-target-state.md` §2 decides the shape:
reconciler-derived, not a merge-hook — a hook in `scripts/merge-pr.sh` would make a substrate
outage block merging (inverting the platform's availability order) and would miss any merge that
bypassed the script, which is exactly the merge a change record must not miss.

This activity walks `gh pr list --state merged` forward from a persisted watermark and lands one
`arch.change` bead per merged PR, idempotently. See `.factory/design.md` for the full design
record: watermark shape, `change_type`/`applications`/evidence derivation, and the `delivers`
edge binding.

Two boundaries, both stated rather than silent (release-gate findings on #700/#701):

* **The watermark is `mergedAt` with a lookback overlap** (`WATERMARK_LOOKBACK_SECONDS`), not a
  strict cutoff: GitHub timestamps are second-granular and list visibility lags, so a merge can
  surface at-or-before the watermark one tick late. The dedup gate (`find_change` on
  `chg.pr-<n>`) bounds each already-landed PR in the overlap to a dedup GET plus its idempotent
  edge checks -- a handful of GETs, not a rebuild, because `_ensure_change_edges` runs
  unconditionally on the existing-bead path (`resolve_release_binding` and `_ensure_link` each
  list links, plus `find_application_by_ref` per resolved application). It is not free. Above
  that sits the sweep-wide `list_release_verdicts()` fetch, which runs once per tick whenever
  the overlap keeps `pending` non-empty, regardless of how many of those PRs are new. The
  measured trade favours the overlap heavily on a first sweep (57 pending PRs amortize that one
  dev.release listing 57-to-1). In steady state the listing is only *attributable* to the
  overlap on a tick that would otherwise have had `pending` empty and returned unchanged with no
  calls at all -- on a tick carrying at least one genuinely new merge it fires regardless, so the
  overlap's marginal cost there is zero listings. Strictly-after
  filtering alone was proven to lose ~4% of this repository's real merges permanently.
* **History before R26.05's opening (2026-09-07) is unrecorded by design** — the initial
  watermark is `INITIAL_WATERMARK`, not the beginning of time, because `gh pr list` pages at
  {limit} rows against 670+ merged PRs and an undesigned backfill would truncate silently. The
  gap is recorded once on the standing status bead (`context.pre_history_gap`, PRIN-008); a
  designed backfill is a cheap future bead that moves the watermark back.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

import httpx
import yaml
from temporalio import activity

_DISPATCHER_ROOT = Path(__file__).resolve().parents[1]
if str(_DISPATCHER_ROOT) not in sys.path:
    sys.path.insert(0, str(_DISPATCHER_ROOT))

import dispatch  # noqa: E402

REPO_ROOT = _DISPATCHER_ROOT.parents[1]

from substrate_client_loader import Substrate as _SharedSubstrateClient  # noqa: E402

MERGE_PR_MODULE_PATH = REPO_ROOT / "scripts" / "merge-pr.py"
PORTFOLIO_PATH = REPO_ROOT / "docs" / "architecture" / "model" / "application-portfolio.yaml"

CREATED_BY = "factory-dispatcher/change-apply"
# "factory-dispatcher/change-apply" is enrolled in apps/substrate/src/bead_rules.py's
# SOURCE_CLASS_WRITERS["derived"] -- declaring "derived" on the standing status
# observation below (obs.arch-change-watermark) depends on that enrolment being
# DEPLOYED, not just merged. See .factory/design.md. (The arch.change bead this
# reconciler also writes, build_change_content below, already declares "derived".)
STATUS_SOURCE_CLASS = "derived"
STATUS_REF = "obs.arch-change-watermark"
#: R26.05's opening (docs/plans/2026-09-07-decision-record-r26-05-opens.md) -- records start at
#: the release that chartered them; see the module docstring's second boundary.
INITIAL_WATERMARK = "2026-09-07T00:00:00Z"
#: 24h: wide enough that list-lag and same-second ties can never outrun it. Not free -- see the
#: module docstring's first boundary for the measured trade (the sweep-wide release listing sits
#: above the dedup gate: 57-to-1 amortized on the first sweep, plus-one per tick in steady state).
WATERMARK_LOOKBACK_SECONDS = 24 * 60 * 60
PRE_HISTORY_GAP_NOTE = (
    "Merges before 2026-09-07 (R26.05's opening) are unrecorded by design: the initial "
    "watermark starts at the chartering release rather than silently truncating at gh's page "
    "limit. A designed backfill is a future bead that moves the watermark back (PRIN-008: the "
    "gap is stated, not silent)."
)
UNRESOLVED_APPLICATION_REF = "app.unresolved"

CHANGE_TYPE_BY_KIND = {"structural": "standard", "behavioral": "normal"}
DEFAULT_CHANGE_TYPE_ON_GAP = "normal"
#: Deliberately independent of `scripts/merge-pr.py`'s CI-gated `CHANGE_KIND_RE`, which only ever
#: matches structural/behavioral -- see .factory/design.md for why emergency needs its own,
#: explicit, never-inferred marker.
EMERGENCY_MARKER_RE = re.compile(
    r"^[ \t]*Emergency change:[ \t]*true[ \t]*$", re.MULTILINE | re.IGNORECASE
)

#: A portfolio `evidence` entry is treated as a path candidate when it looks like one -- see
#: application_path_prefixes().
PORTFOLIO_PATH_ROOTS = ("apps/", "infrastructure/", "docs/", "scripts/", ".github/")

MERGED_PR_LIST_LIMIT = 200
HTTP_TIMEOUT_S = 30.0
#: Page size for dev.task/dev.release listings -- was a hard cap (500) with no pagination;
#: now just the per-request page size, since _PagedDevBeadCache pages past it.
DEV_BEAD_PAGE_SIZE = 500
#: Runaway guard, not a working limit: 50 pages x DEV_BEAD_PAGE_SIZE is far past any population
#: this reconciler will plausibly see. Exists so a listing that never reports a short page (e.g.
#: a server bug that ignores `offset`) fails loudly instead of looping or returning a silent
#: partial population -- see .factory/design.md.
MAX_DEV_BEAD_PAGES = 50


class ChangeApplyError(RuntimeError):
    pass


class _PagedDevBeadCache:
    """Fetches each `dev.<bead_type>` population at most once, paged to exhaustion.

    `page_fetch(bead_type, limit, offset)` is the only extension point, so the same pagination
    and one-fetch-per-population caching serves both the live HTTP store and an in-memory fake in
    tests. A page shorter than `limit` is the only reliable "no more pages" signal for offset
    paging; anything else means keep going -- see .factory/design.md for why this replaces a
    single capped read outright rather than adding a truncation check on top of one.
    """

    def __init__(
        self,
        page_fetch: Callable[[str, int, int], list[dict[str, Any]]],
        *,
        page_size: int = DEV_BEAD_PAGE_SIZE,
        max_pages: int = MAX_DEV_BEAD_PAGES,
    ):
        self._page_fetch = page_fetch
        self._page_size = page_size
        self._max_pages = max_pages
        self._cache: dict[str, list[dict[str, Any]]] = {}
        #: population-level fetches (not pages, not lookups) per bead_type -- test seam for
        #: pinning "fetched at most once per sweep" independent of how many lookups ask for it.
        self.fetch_calls: dict[str, int] = {}

    def get(self, bead_type: str) -> list[dict[str, Any]]:
        if bead_type in self._cache:
            return self._cache[bead_type]
        self.fetch_calls[bead_type] = self.fetch_calls.get(bead_type, 0) + 1
        beads: list[dict[str, Any]] = []
        offset = 0
        for _ in range(self._max_pages):
            page = self._page_fetch(bead_type, self._page_size, offset)
            beads.extend(page)
            if len(page) < self._page_size:
                self._cache[bead_type] = beads
                return beads
            offset += self._page_size
        raise ChangeApplyError(
            f"dev.{bead_type} listing did not terminate within {self._max_pages} pages "
            f"({self._max_pages * self._page_size} rows) -- refusing to treat a partial "
            "population as complete."
        )


def _load_merge_pr_module():
    """Import scripts/merge-pr.py by path -- the filename is not a valid module name.

    Reuses `extract_change_kind` rather than a second regex: one implementation of "exactly one
    bare Change kind: line", not a copy that could drift from the one CI already enforces.
    """
    spec = importlib.util.spec_from_file_location("merge_pr_under_change_apply", MERGE_PR_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# the narrow store this activity reads/writes through
# ---------------------------------------------------------------------------


class ChangeApplyStore(Protocol):
    def find_change(self, ref: str) -> dict[str, Any] | None: ...

    def create_change(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def find_status(self) -> dict[str, Any] | None: ...

    def create_status(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def update_status(
        self, bead_id: str, content: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]: ...

    def find_task_by_pr_url(self, pr_url: str) -> dict[str, Any] | None: ...

    def find_application_by_ref(self, ref: str) -> dict[str, Any] | None: ...

    def list_release_verdicts(self) -> list[dict[str, Any]]: ...

    def list_attachment_notes(self) -> list[dict[str, Any]]: ...

    def list_changes(self) -> list[dict[str, Any]]: ...

    def list_links(
        self, bead_id: str, *, direction: str = "outgoing", link_type: str | None = None
    ) -> list[dict[str, Any]]: ...

    def create_link(self, source_id: str, target_id: str, link_type: str) -> dict[str, Any]: ...


class SubstrateChangeApplyStore:
    """Narrow client for the bead reads/writes this activity owns.

    Deliberately not a widening of `substrate.Substrate` -- same reasoning as
    `ea_observation.SubstrateEAObserverStore`: one tested consumer, its own methods, nothing more.
    """

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        # Header construction and credential resolution live in substrate_client
        # (the one Python substrate client, M6) rather than duplicated here --
        # this store keeps its own domain methods and `_request` shape, borrowing
        # only the auth/base_url plumbing every store used to hand-roll.
        _client = _SharedSubstrateClient(base_url=base_url, api_key=api_key)
        self.base_url = _client.base_url
        self._headers = _client._headers
        # One cache per store instance: one instance is constructed per activity invocation
        # (default_store(), called once by apply_merged_pr_changes_activity) and lives for the
        # whole sweep, so caching here is what makes "at most once per sweep" true without any
        # sweep-scoped object of its own -- see .factory/design.md.
        self._dev_beads = _PagedDevBeadCache(self._fetch_dev_bead_page)
        # Separate cache, separate namespace: _fetch_dev_bead_page hard-codes
        # namespace=dev, so arch.change needs its own fetch function rather
        # than a namespace kwarg bolted onto the dev-scoped one.
        self._arch_beads = _PagedDevBeadCache(self._fetch_arch_bead_page)

    def _request(
        self, method: str, path: str, headers: dict[str, str] | None = None, **kwargs: Any
    ) -> Any:
        # Merge per-call headers over the standing auth headers instead of
        # passing both through httpx.request -- a caller supplying headers=
        # (create_link's X-Created-By) otherwise collides with the keyword
        # and raises TypeError on the live path the FakeStore never walks.
        # The parameter is explicit rather than popped from **kwargs so the
        # OPS-89 signature invariant can see it: a store whose merge lives
        # inside **kwargs is invisible to a check that reads signatures.
        merged_headers = {**self._headers, **(headers or {})}
        response = httpx.request(
            method,
            f"{self.base_url}{path}",
            headers=merged_headers,
            timeout=HTTP_TIMEOUT_S,
            **kwargs,
        )
        response.raise_for_status()
        return response.json()

    def find_change(self, ref: str) -> dict[str, Any] | None:
        found = self._request(
            "GET",
            "/beads",
            params={"namespace": "arch", "type": "change", "content_ref": ref, "limit": 1},
        )
        return found[0] if found else None

    def create_change(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/beads", json=payload)

    def find_status(self) -> dict[str, Any] | None:
        found = self._request(
            "GET",
            "/beads",
            params={
                "namespace": "arch",
                "type": "observation",
                "content_ref": STATUS_REF,
                "limit": 1,
            },
        )
        return found[0] if found else None

    def create_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/beads", json=payload)

    def update_status(
        self, bead_id: str, content: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        return self._request(
            "PATCH",
            f"/beads/{bead_id}",
            json={"content": content, "context": context, "created_by": CREATED_BY},
        )

    def _fetch_dev_bead_page(self, bead_type: str, limit: int, offset: int) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            "/beads",
            params={"namespace": "dev", "type": bead_type, "limit": limit, "offset": offset},
        )

    def _list_dev_beads(self, bead_type: str) -> list[dict[str, Any]]:
        return self._dev_beads.get(bead_type)

    def _fetch_arch_bead_page(self, bead_type: str, limit: int, offset: int) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            "/beads",
            params={"namespace": "arch", "type": bead_type, "limit": limit, "offset": offset},
        )

    def find_application_by_ref(self, ref: str) -> dict[str, Any] | None:
        found = self._request(
            "GET",
            "/beads",
            params={"namespace": "arch", "type": "application", "content_ref": ref, "limit": 1},
        )
        return found[0] if found else None

    def find_task_by_pr_url(self, pr_url: str) -> dict[str, Any] | None:
        return match_task_by_pr_url(self._list_dev_beads("task"), self.list_attachment_notes, pr_url)

    def list_release_verdicts(self) -> list[dict[str, Any]]:
        """Every `dev.release` bead, fetched once -- see `find_release_verdict_by_pr` (module
        level) for the per-PR match over this list, and `resolve_verdict_evidence` for why the
        fetch happens once per sweep rather than once per PR."""
        return self._list_dev_beads("release")

    def list_attachment_notes(self) -> list[dict[str, Any]]:
        """Every `dev.note` of `content.kind == "attachment"`, fetched once per sweep (the
        store has no route that filters notes server-side by kind, so the whole `dev.note`
        population pages once and is filtered client-side) -- see `match_task_by_pr_url` for
        why this is called lazily, only after both faster passes miss."""
        return [
            note
            for note in self._list_dev_beads("note")
            if (note.get("content") or {}).get("kind") == "attachment"
        ]

    def list_changes(self) -> list[dict[str, Any]]:
        return self._arch_beads.get("change")

    def list_links(
        self, bead_id: str, *, direction: str = "outgoing", link_type: str | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"direction": direction}
        if link_type is not None:
            params["link_type"] = link_type
        return self._request("GET", f"/beads/{bead_id}/links", params=params)

    def create_link(self, source_id: str, target_id: str, link_type: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/beads/{source_id}/links",
            headers={"X-Created-By": CREATED_BY},
            json={"target_id": target_id, "link_type": link_type},
        )


def default_store() -> ChangeApplyStore:
    return SubstrateChangeApplyStore()


# ---------------------------------------------------------------------------
# gh reads -- no new credential, the same subprocess wrapper dispatch.py uses
# ---------------------------------------------------------------------------


def list_merged_prs(cfg: "dispatch.Config", *, limit: int = MERGED_PR_LIST_LIMIT) -> list[dict[str, Any]]:
    """Every merged PR gh currently reports, most recent first (gh's default order)."""
    proc = dispatch.run(
        [
            "gh", "pr", "list",
            "--repo", cfg.repo,
            "--state", "merged",
            "--limit", str(limit),
            "--json", "number,url,title,body,mergedAt",
        ],
        cwd=cfg.repo_root,
        timeout=60,
    )
    return json.loads(proc.stdout or "[]")


def pr_touched_paths(cfg: "dispatch.Config", number: int) -> list[str]:
    """The file paths a merged PR touched, read live from GitHub -- independent of how (or
    whether) `scripts/merge-pr.sh` was involved in landing it."""
    proc = dispatch.run(
        ["gh", "pr", "view", str(number), "--repo", cfg.repo, "--json", "files"],
        cwd=cfg.repo_root,
        timeout=60,
    )
    payload = json.loads(proc.stdout or "{}")
    return [str(f.get("path")) for f in payload.get("files") or [] if f.get("path")]


# ---------------------------------------------------------------------------
# pure logic -- testable without gh, without Temporal, without a substrate
# ---------------------------------------------------------------------------


def change_ref_for_pr(number: int) -> str:
    return f"chg.pr-{number}"


def lookback_cutoff(watermark: str, *, lookback_seconds: int = WATERMARK_LOOKBACK_SECONDS) -> str:
    """`watermark - lookback`, as the same fixed-precision ISO-8601 `Z` string."""
    parsed = datetime.fromisoformat(watermark.replace("Z", "+00:00"))
    return _iso(parsed - timedelta(seconds=lookback_seconds))


def merged_prs_since(prs: list[dict[str, Any]], watermark: str | None) -> list[dict[str, Any]]:
    """Ascending by `mergedAt`, from `watermark - lookback` onward (INCLUSIVE at the cutoff).

    The overlap is the loss-proofing (release-gate finding on #701): a strictly-after filter
    permanently drops a merge whose `mergedAt` ties the watermark to the second, or that
    surfaces in `gh pr list` one tick late. Everything inside the overlap that was already
    landed is absorbed by the `find_change` dedup gate at zero write cost. A `None` watermark
    means first run: start at `INITIAL_WATERMARK` (see module docstring), never the beginning
    of gh's page.

    ISO-8601 `Z` timestamps of fixed precision sort correctly as plain strings -- no datetime
    parsing needed for the sort; only the lookback subtraction parses.
    """
    ordered = sorted(prs, key=lambda pr: pr["mergedAt"])
    if watermark is None:
        # First run: the initial watermark is a DESIGN boundary (R26.05's opening), not a
        # progress marker subject to ties or list lag -- no lookback below it, or the first
        # tick would quietly record 24h of pre-charter history the gap note says is excluded.
        cutoff = INITIAL_WATERMARK
    else:
        cutoff = lookback_cutoff(watermark)
    return [pr for pr in ordered if pr["mergedAt"] >= cutoff]


def classify_change_kind(
    body: str, *, extract_change_kind: Callable[[str], str], change_kind_re: re.Pattern[str]
) -> tuple[str, str | None]:
    """`(change_type, gap_note)`. `gap_note` is None exactly when nothing needs stating.

    Checks the emergency marker first -- it is an explicit override, not an inference, and takes
    precedence over whatever (if anything) the Change kind line says.
    """
    if EMERGENCY_MARKER_RE.search(body or ""):
        return "emergency", None
    try:
        declaration = extract_change_kind(body or "")
    except Exception as exc:  # merge_pr.Refusal, by name unimportable without importing the module
        return DEFAULT_CHANGE_TYPE_ON_GAP, (
            f"Change kind declaration missing or unparseable ({exc}); change_type defaulted to "
            f"'{DEFAULT_CHANGE_TYPE_ON_GAP}'."
        )
    match = change_kind_re.match(declaration)
    if match is None:  # defensive: extract_change_kind's own contract guarantees this matches
        return DEFAULT_CHANGE_TYPE_ON_GAP, (
            f"Change kind declaration {declaration!r} did not parse as structural/behavioral; "
            f"change_type defaulted to '{DEFAULT_CHANGE_TYPE_ON_GAP}'."
        )
    return CHANGE_TYPE_BY_KIND[match.group(1)], None


def load_portfolio_applications(path: Path = PORTFOLIO_PATH) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text()) or {}
    return data.get("applications", [])


def _looks_like_path(value: str) -> bool:
    return isinstance(value, str) and (
        any(value.startswith(root) for root in PORTFOLIO_PATH_ROOTS) or value.endswith(".md")
    )


def application_path_prefixes(
    applications: list[dict[str, Any]], *, repo_root: Path = REPO_ROOT
) -> dict[str, list[str]]:
    """`application ref -> path prefixes`, from the three signals the portfolio model actually
    carries today -- see .factory/design.md for why each is included."""
    prefixes: dict[str, list[str]] = {}
    for app in applications:
        ref = app.get("ref")
        if not ref:
            continue
        content = app.get("content") or {}
        candidates: set[str] = set()
        for entry in content.get("evidence") or []:
            if _looks_like_path(entry):
                candidates.add(entry)
        workload = content.get("workload") or {}
        for obj in workload.get("objects") or []:
            manifest = obj.get("manifest")
            if manifest:
                candidates.add(manifest)
        suffix = ref.split(".", 1)[1] if "." in ref else ref
        if (repo_root / "apps" / suffix).is_dir():
            candidates.add(f"apps/{suffix}/")
        if candidates:
            prefixes[ref] = sorted(candidates)
    return prefixes


def applications_for_touched_paths(
    touched_paths: list[str], prefixes: dict[str, list[str]]
) -> list[str]:
    matched: set[str] = set()
    for path in touched_paths:
        for ref, path_prefixes in prefixes.items():
            if any(path == prefix or path.startswith(prefix) for prefix in path_prefixes):
                matched.add(ref)
                break
    return sorted(matched)


_PR_NUMBER_RE = re.compile(r"/pull/(\d+)$")


def pr_number_from_url(url: str) -> str:
    match = _PR_NUMBER_RE.search(url)
    return match.group(1) if match else ""


def match_task_by_pr_url(
    tasks: list[dict[str, Any]],
    attachment_notes: Callable[[], list[dict[str, Any]]],
    pr_url: str,
    *,
    pr_refs_only: bool = False,
) -> dict[str, Any] | None:
    """Resolve a merged PR's `dev.task` through every field the dispatcher sets on it, in the
    order the dispatcher writes them: `content.pr_refs` (only `--bind-pr` writes this), then
    `content.pr_url` (the normal PR-open step's `patch_content`), then the attachment note's
    `content.url` -- never a note's body text, and never a non-attachment note. `attachment_
    notes` is called at most once, and only when both faster passes miss: a lookup that
    resolves through `pr_refs` or `pr_url` reads no `dev.note` at all. `pr_refs_only` stops
    after the first pass, for measuring what the pre-B19 matcher alone could resolve.

    The one matching algorithm both `SubstrateChangeApplyStore.find_task_by_pr_url` and its
    test double call -- neither keeps a loop of its own, so the two cannot drift apart.
    """
    for task in tasks:
        if pr_url in ((task.get("content") or {}).get("pr_refs") or []):
            return task
    if pr_refs_only:
        return None
    for task in tasks:
        if (task.get("content") or {}).get("pr_url") == pr_url:
            return task
    for note in attachment_notes():
        content = note.get("content") or {}
        if content.get("kind") != "attachment" or content.get("url") != pr_url:
            continue
        parent_id = note.get("parent_id")
        for task in tasks:
            if task.get("id") == parent_id:
                return task
        return None
    return None


#: Recorded when `list_release_verdicts()` returns nothing at all: a structural fact about the
#: store (the bead type has no writer), true for every PR in the sweep alike -- never "this PR
#: has no verdict", which would misstate one absence as 57 individual per-PR findings.
NO_RELEASE_VERDICTS_WRITTEN_EVIDENCE = (
    "release-gate verdict: no dev.release record exists in the store -- dev.release has no "
    "writer yet (a structural gap, not a per-PR finding)"
)


def find_release_verdict_by_pr(
    releases: list[dict[str, Any]], pr_number: str, pr_url: str
) -> dict[str, Any] | None:
    """Pure match over an already-fetched `list_release_verdicts()` result -- no store access,
    so matching every PR in a sweep against the same list costs one store read, not one per PR."""
    needles = (pr_url, f"#{pr_number}", f"PR-{pr_number}", f"PR #{pr_number}")
    for release in releases:
        refs = (release.get("content") or {}).get("pr_refs") or []
        for ref in refs:
            lowered = str(ref).lower()
            if any(needle.lower() in lowered for needle in needles):
                return release
    return None


def resolve_verdict_evidence(
    releases: list[dict[str, Any]], *, pr_number: str, pr_url: str
) -> str:
    """`releases` is the sweep-wide `list_release_verdicts()` result, fetched once by the
    caller -- never re-queried here per PR. An empty list is the structural gap (nothing writes
    `dev.release`); a non-empty list with no match for this PR is a genuine per-PR gap, honestly
    named because a writer demonstrably exists."""
    if not releases:
        return NO_RELEASE_VERDICTS_WRITTEN_EVIDENCE
    release = find_release_verdict_by_pr(releases, pr_number, pr_url)
    if release is None:
        return "release-gate verdict: no dev.release found referencing this PR"
    content = release.get("content") or {}
    verdict = content.get("verdict", "unknown")
    return f"release-gate verdict: {verdict} (dev.release {release.get('id')})"


def resolve_release_binding(store: ChangeApplyStore, *, pr_url: str) -> str | None:
    """The `arch.release` bead id a merged PR's `dev.task` delivers, if any.

    Best-effort, never a gate: a PR merged outside the dispatcher (no matching `dev.task`) simply
    gets no `delivers` edge on its `arch.change` record.
    """
    task = store.find_task_by_pr_url(pr_url)
    if task is None:
        return None
    links = store.list_links(task["id"], direction="outgoing", link_type="delivers")
    if not links:
        return None
    return links[0].get("target_id")


def build_change_content(
    *,
    pr: dict[str, Any],
    touched_paths: list[str],
    application_prefixes: dict[str, list[str]],
    extract_change_kind: Callable[[str], str],
    change_kind_re: re.Pattern[str],
    verdict_evidence: str,
) -> dict[str, Any]:
    change_type, gap_note = classify_change_kind(
        pr.get("body") or "", extract_change_kind=extract_change_kind, change_kind_re=change_kind_re
    )
    applications = applications_for_touched_paths(touched_paths, application_prefixes)
    evidence = [pr["url"], verdict_evidence]
    if gap_note:
        evidence.append(gap_note)
    if not applications:
        applications = [UNRESOLVED_APPLICATION_REF]
        unmatched = ", ".join(sorted(touched_paths)) if touched_paths else "(no files reported)"
        evidence.append(f"no portfolio application matched touched paths: {unmatched}")
    return {
        "ref": change_ref_for_pr(pr["number"]),
        "change_type": change_type,
        "summary": pr.get("title") or f"PR #{pr['number']}",
        "applications": applications,
        "evidence": evidence,
        "implemented_at": pr["mergedAt"],
        "source_class": "derived",
    }


def _ensure_link(
    store: ChangeApplyStore, source_id: str, target_id: str, link_type: str, *, dry_run: bool = False
) -> bool:
    """Create the link iff absent; True when a link was created (or, under `dry_run`, would
    have been). The idempotent-finish pattern the delivers binding used in attempt 1, restored
    and generalized (release-gate finding F2/regression on #701): edges must be completable on
    a resumed run, never lost to a crash between bead creation and link creation."""
    existing = store.list_links(source_id, direction="outgoing", link_type=link_type)
    if any(link.get("target_id") == target_id for link in existing):
        return False
    if not dry_run:
        store.create_link(source_id, target_id, link_type)
    return True


def _ensure_delivers_edge(
    store: ChangeApplyStore, change_id: str, pr_url: str, *, dry_run: bool = False
) -> tuple[bool, bool]:
    """`(created, resolved)` -- `resolved` is False exactly when no release binding could be
    found at all (no matching task, or a matching task with no outgoing `delivers` link)."""
    release_id = resolve_release_binding(store, pr_url=pr_url)
    if release_id is None:
        return False, False
    return _ensure_link(store, change_id, release_id, "delivers", dry_run=dry_run), True


def _ensure_affects_edges(
    store: ChangeApplyStore, change_id: str, applications: list[str], *, dry_run: bool = False
) -> int:
    created = 0
    for app_ref in applications:
        if app_ref == UNRESOLVED_APPLICATION_REF:
            continue
        app = store.find_application_by_ref(app_ref)
        if app is None:
            continue
        if _ensure_link(store, change_id, app["id"], "affects", dry_run=dry_run):
            created += 1
    return created


def _ensure_change_edges(
    store: ChangeApplyStore, *, change_id: str, pr_url: str, applications: list[str]
) -> int:
    """Finish the record's outgoing edges idempotently; returns how many were created.

    * `delivers` -> the arch.release the PR's dev.task delivers (best-effort, never a gate).
    * `affects` -> each resolved arch.application. The schema's own docstring states the
      design -- "a change is an event against a CI, so it carries the CI by an affects edge" --
      and the console's persona panels traverse exactly this edge (PRIN-003: a real dependency
      is a typed edge or it is a defect). `app.unresolved` is a stated gap, not a CI: no edge.
    """
    created, _ = _ensure_delivers_edge(store, change_id, pr_url)
    created = 1 if created else 0
    created += _ensure_affects_edges(store, change_id, applications)
    return created


def backfill_change_edges(store: ChangeApplyStore, *, dry_run: bool) -> dict[str, Any]:
    """Walk every existing `arch.change` and finish its `delivers`/`affects` edges, idempotently
    -- the population-wide counterpart of `_ensure_change_edges`'s per-PR call in `land_merged_
    pr`, for the beads this reconciler already landed before B19 widened `find_task_by_pr_url`.

    A change's PR url is `content.evidence[0]` -- `build_change_content` puts `pr["url"]` there
    first, and nothing else writes `arch.change.content.evidence`.
    """
    changes = store.list_changes()
    delivers_created = 0
    affects_created = 0
    unresolved: list[str] = []
    for change in changes:
        content = change.get("content") or {}
        evidence = content.get("evidence") or []
        pr_url = evidence[0] if evidence else None
        resolved = False
        if pr_url is not None:
            created, resolved = _ensure_delivers_edge(store, change["id"], pr_url, dry_run=dry_run)
            if created:
                delivers_created += 1
        if not resolved:
            unresolved.append(content.get("ref") or change.get("id"))
        affects_created += _ensure_affects_edges(
            store, change["id"], content.get("applications") or [], dry_run=dry_run
        )
    return {
        "changes": len(changes),
        "edges_created": {"delivers": delivers_created, "affects": affects_created},
        "unresolved": unresolved,
    }


def land_merged_pr(
    store: ChangeApplyStore,
    cfg: "dispatch.Config",
    pr: dict[str, Any],
    *,
    extract_change_kind: Callable[[str], str],
    change_kind_re: re.Pattern[str],
    application_prefixes: dict[str, list[str]],
    fetch_touched_paths: Callable[["dispatch.Config", int], list[str]],
    release_verdicts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Land one merged PR's `arch.change`, idempotently. Returns `{"status": "created"|"unchanged", ...}`."""
    ref = change_ref_for_pr(pr["number"])
    existing = store.find_change(ref)
    if existing is not None:
        # Second gate, restored (attempt 1's pattern): the bead existing does not mean its
        # edges do -- a crash between create_change and the link writes must be completable
        # on resume, not a permanent silent loss of the delivers/affects bindings.
        applications = (existing.get("content") or {}).get("applications") or []
        finished = _ensure_change_edges(
            store, change_id=existing["id"], pr_url=pr["url"], applications=applications
        )
        if finished:
            return {"status": "completed_edges", "ref": ref, "edges_created": finished}
        return {"status": "unchanged", "ref": ref}

    touched_paths = fetch_touched_paths(cfg, pr["number"])
    pr_number = str(pr["number"])
    verdict_evidence = resolve_verdict_evidence(
        release_verdicts, pr_number=pr_number, pr_url=pr["url"]
    )
    content = build_change_content(
        pr=pr,
        touched_paths=touched_paths,
        application_prefixes=application_prefixes,
        extract_change_kind=extract_change_kind,
        change_kind_re=change_kind_re,
        verdict_evidence=verdict_evidence,
    )
    bead = store.create_change(
        {
            "namespace": "arch",
            "type": "change",
            "state": "active",
            "trust_tier": "system",
            "created_by": CREATED_BY,
            "content": content,
        }
    )
    _ensure_change_edges(
        store, change_id=bead["id"], pr_url=pr["url"], applications=content["applications"]
    )
    return {"status": "created", "ref": ref, "id": bead["id"]}


def _iso(value: datetime) -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _status_workload() -> dict[str, str]:
    return {
        "cluster": "repository",
        "namespace": "github",
        "kind": "MergedPullRequest",
        "name": "arch-change-reconciler",
    }


def _write_status(
    store: ChangeApplyStore,
    existing: dict[str, Any] | None,
    *,
    watermark: str | None,
    now: datetime,
    created: int,
    unchanged: int,
    first_run: bool = False,
) -> None:
    content = {
        "ref": STATUS_REF,
        "source_class": STATUS_SOURCE_CLASS,
        "observed_at": _iso(now),
        "workload": _status_workload(),
    }
    context = {
        "last_merged_at": watermark,
        "last_run_at": _iso(now),
        "last_run_created": created,
        "last_run_unchanged": unchanged,
    }
    if first_run:
        context["pre_history_gap"] = PRE_HISTORY_GAP_NOTE
    elif existing is not None:
        prior_gap = (existing.get("context") or {}).get("pre_history_gap")
        if prior_gap:
            context["pre_history_gap"] = prior_gap
    if existing is None:
        store.create_status(
            {
                "namespace": "arch",
                "type": "observation",
                "state": "active",
                "trust_tier": "system",
                "created_by": CREATED_BY,
                "content": content,
                "context": context,
            }
        )
    else:
        store.update_status(existing["id"], content, context)


def apply_merged_pr_changes(
    store: ChangeApplyStore,
    cfg: "dispatch.Config",
    *,
    list_prs_fn: Callable[["dispatch.Config"], list[dict[str, Any]]] = list_merged_prs,
    fetch_touched_paths: Callable[["dispatch.Config", int], list[str]] = pr_touched_paths,
    portfolio_path: Path = PORTFOLIO_PATH,
    merge_pr_module: Any = None,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """Reconcile merged PRs forward from the persisted watermark into `arch.change` beads.

    Zero writes when nothing changed: an empty `merged_prs_since` window makes zero substrate
    calls beyond the initial status/list reads. See .factory/design.md.
    """
    merge_pr_module = merge_pr_module or _load_merge_pr_module()
    existing_status = store.find_status()
    watermark = (existing_status.get("context") or {}).get("last_merged_at") if existing_status else None
    first_run = watermark is None

    prs = list_prs_fn(cfg)
    pending = merged_prs_since(prs, watermark)
    if not pending:
        return {"status": "unchanged", "checked": 0, "created": 0, "unchanged": 0, "watermark": watermark}

    applications = load_portfolio_applications(portfolio_path)
    prefixes = application_path_prefixes(applications)
    # Fetched once for the whole sweep, not once per PR landed: see
    # resolve_verdict_evidence / find_release_verdict_by_pr for why a per-PR store read against
    # dev.release (a bead type with no writer) is exactly the cost shape this must not have.
    release_verdicts = store.list_release_verdicts()

    created = 0
    unchanged = 0
    completed_edges = 0
    for pr in pending:
        result = land_merged_pr(
            store,
            cfg,
            pr,
            extract_change_kind=merge_pr_module.extract_change_kind,
            change_kind_re=merge_pr_module.CHANGE_KIND_RE,
            application_prefixes=prefixes,
            fetch_touched_paths=fetch_touched_paths,
            release_verdicts=release_verdicts,
        )
        if result["status"] == "created":
            created += 1
        elif result["status"] == "completed_edges":
            completed_edges += 1
        else:
            unchanged += 1
        if watermark is None or pr["mergedAt"] > watermark:
            watermark = pr["mergedAt"]
            _write_status(
                store, existing_status, watermark=watermark, now=now_fn(),
                created=created, unchanged=unchanged, first_run=first_run,
            )
            existing_status = store.find_status()
            first_run = False

    return {
        "status": "applied",
        "checked": len(pending),
        "created": created,
        "unchanged": unchanged,
        "completed_edges": completed_edges,
        "watermark": watermark,
    }


@activity.defn(name="apply_merged_pr_changes")
def apply_merged_pr_changes_activity(request: dict[str, Any] | None = None) -> dict[str, Any]:
    request = request or {}
    cfg = dispatch.Config.from_env()
    return apply_merged_pr_changes(default_store(), cfg)


ACTIVITIES = [apply_merged_pr_changes_activity]
