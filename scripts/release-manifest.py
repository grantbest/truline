#!/usr/bin/env python3
"""Build the release-gate manifest from PR numbers.

The live path reads PR metadata through ``gh`` and reads bead context through
the Substrate HTTP API. The fixture path uses the same provider shape so tests
can cover the manifest with no network and no credentials.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Protocol


def _load_shared(name: str):
    module = sys.modules.get(name)
    if module is not None:
        return module
    path = pathlib.Path(__file__).resolve().with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load shared module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_substrate = _load_shared("substrate_client")
SECRET_ENV_VARS = (_substrate.URL_ENV, _substrate.KEY_ENV)
GH_PR_FIELDS = "number,title,body,url,headRefName,baseRefName"
_markers = _load_shared("gate_markers")
OUTER_LOOP_RE = _markers.OUTER_LOOP_RE
find_bead_id = _markers.find_bead_id
CHANGE_KIND_RE = re.compile(r"^[ \t]*Change kind:[ \t]*(structural|behavioral)[ \t]*$", re.MULTILINE | re.IGNORECASE)
PR_URL_RE = re.compile(r"/pull/(\d+)(?:[/?#]|$)")


@dataclass(frozen=True)
class PrRecord:
    number: int
    title: str
    body: str
    url: str = ""
    head_ref: str = ""
    base_ref: str = ""


@dataclass(frozen=True)
class LinkRecord:
    link_type: str
    source_id: str
    target_id: str

    def other_id(self, bead_id: str) -> str | None:
        if self.source_id == bead_id:
            return self.target_id
        if self.target_id == bead_id:
            return self.source_id
        return None

    def direction_for(self, bead_id: str) -> str:
        if self.source_id == bead_id:
            return "out"
        if self.target_id == bead_id:
            return "in"
        return "link"


@dataclass
class PrContext:
    pr: PrRecord
    bead_id: str | None
    outer_loop: bool = False
    bead: dict[str, Any] | None = None
    notes: list[dict[str, Any]] = field(default_factory=list)
    links: list[LinkRecord] = field(default_factory=list)
    linked_beads: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def has_handoff_defect(self) -> bool:
        return self.bead_id is None and not self.outer_loop


class Provider(Protocol):
    def pr(self, number: int) -> PrRecord:
        ...

    def bead(self, bead_id: str) -> dict[str, Any]:
        ...

    def notes(self, bead_id: str) -> list[dict[str, Any]]:
        ...

    def links(self, bead_id: str) -> list[LinkRecord]:
        ...

    def release(self, ref: str) -> dict[str, Any] | None:
        ...


class FixtureProvider:
    def __init__(self, root: pathlib.Path) -> None:
        self.root = root

    def pr(self, number: int) -> PrRecord:
        data = self._read_json(self.root / "prs" / f"{number}.json")
        return _pr_from_gh(data)

    def bead(self, bead_id: str) -> dict[str, Any]:
        return self._read_json(self.root / "beads" / f"{bead_id}.json")

    def notes(self, bead_id: str) -> list[dict[str, Any]]:
        path = self.root / "notes" / f"{bead_id}.json"
        if not path.exists():
            return []
        data = self._read_json(path)
        return data if isinstance(data, list) else list(data.get("items") or data.get("beads") or [])

    def links(self, bead_id: str) -> list[LinkRecord]:
        path = self.root / "links" / f"{bead_id}.json"
        if not path.exists():
            return []
        data = self._read_json(path)
        rows = data if isinstance(data, list) else list(data.get("items") or data.get("links") or [])
        return [_link_from_dict(row) for row in rows]

    def release(self, ref: str) -> dict[str, Any] | None:
        path = self.root / "releases" / f"{ref}.json"
        if not path.exists():
            return None
        return self._read_json(path)

    def _read_json(self, path: pathlib.Path) -> Any:
        return json.loads(path.read_text())


class LiveProvider:
    def __init__(self) -> None:
        self._client = _substrate.reader(
            base_url=os.environ.get(_substrate.URL_ENV),
            key=os.environ.get(_substrate.KEY_ENV),
        )

    def pr(self, number: int) -> PrRecord:
        result = subprocess.run(
            ["gh", "pr", "view", str(number), "--json", GH_PR_FIELDS],
            check=False,
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or f"gh failed for PR #{number}"
            raise RuntimeError(message)
        return _pr_from_gh(json.loads(result.stdout))

    def bead(self, bead_id: str) -> dict[str, Any]:
        return self._request(f"/beads/{urllib.parse.quote(bead_id)}")

    def notes(self, bead_id: str) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({
            "namespace": "dev",
            "type": "note",
            "parent_id": bead_id,
            "limit": 100,
        })
        data = self._request(f"/beads?{query}")
        return data if isinstance(data, list) else list(data.get("items") or data.get("beads") or [])

    def links(self, bead_id: str) -> list[LinkRecord]:
        data = self._request(f"/beads/{urllib.parse.quote(bead_id)}/links?direction=both")
        rows = data if isinstance(data, list) else list(data.get("items") or data.get("links") or [])
        return [_link_from_dict(row) for row in rows]

    def release(self, ref: str) -> dict[str, Any] | None:
        query = urllib.parse.urlencode({
            "namespace": "arch",
            "type": "release",
            "content_ref": ref,
            "limit": 1,
        })
        data = self._request(f"/beads?{query}")
        rows = data if isinstance(data, list) else list(data.get("items") or data.get("beads") or [])
        return rows[0] if rows else None

    def _request(self, path: str) -> Any:
        return self._client.get(path)


def _pr_from_gh(data: dict[str, Any]) -> PrRecord:
    return PrRecord(
        number=int(data["number"]),
        title=str(data.get("title") or ""),
        body=str(data.get("body") or ""),
        url=str(data.get("url") or ""),
        head_ref=str(data.get("headRefName") or ""),
        base_ref=str(data.get("baseRefName") or ""),
    )


def _link_from_dict(row: dict[str, Any]) -> LinkRecord:
    return LinkRecord(
        link_type=str(row.get("link_type") or row.get("type") or "linked"),
        source_id=str(row.get("source_id") or ""),
        target_id=str(row.get("target_id") or ""),
    )


def extract_change_kind(body: str) -> str:
    match = CHANGE_KIND_RE.search(body or "")
    return match.group(1).lower() if match else "missing"


def collect_context(pr_numbers: list[int], provider: Provider) -> list[PrContext]:
    contexts: list[PrContext] = []
    for number in pr_numbers:
        pr = provider.pr(number)
        bead_id = find_bead_id(pr.body)
        context = PrContext(pr=pr, bead_id=bead_id, outer_loop=bool(OUTER_LOOP_RE.search(pr.body)))
        if bead_id:
            context.bead = provider.bead(bead_id)
            context.notes = provider.notes(bead_id)
            context.links = provider.links(bead_id)
            for link in context.links:
                linked_id = link.other_id(bead_id)
                if linked_id and linked_id not in context.linked_beads:
                    context.linked_beads[linked_id] = provider.bead(linked_id)
        contexts.append(context)
    return contexts


def _pr_number_from_bead(bead: dict[str, Any]) -> int | None:
    content = bead.get("content")
    content = content if isinstance(content, dict) else {}
    match = PR_URL_RE.search(str(content.get("pr_url") or ""))
    return int(match.group(1)) if match else None


def resolve_release_prs(ref: str, provider: Provider) -> list[int]:
    """PR numbers for every task that delivers ``ref``, via its delivers edges.

    A release the substrate does not hold is a refusal, not an empty
    manifest — see ``main``'s docstring reference to the same rule. A
    delivering task with no ``pr_url`` yet (not proposed) is simply not
    included; it has no PR to add.
    """
    release = provider.release(ref)
    if release is None:
        raise RuntimeError(f"release {ref} not found in the substrate")

    release_id = str(release.get("id") or "")
    numbers: list[int] = []
    for link in provider.links(release_id) if release_id else []:
        if link.link_type != "delivers":
            continue
        task_id = link.other_id(release_id)
        if not task_id:
            continue
        number = _pr_number_from_bead(provider.bead(task_id))
        if number is not None:
            numbers.append(number)
    return sorted(set(numbers))


def render_manifest(contexts: list[PrContext], env: dict[str, str] | None = None) -> str:
    lines: list[str] = [
        "# Release Gate Prompt Skeleton",
        "",
        "Review the PRs listed below as one release, not as independent changes.",
        "",
        "## Scope",
        "",
        "| PR | Title | Bead | Lane | Risk | Change kind |",
        "|---|---|---|---|---|---|",
    ]

    for context in contexts:
        content = _content(context.bead)
        if context.bead_id:
            bead_cell = context.bead_id
        elif context.outer_loop:
            bead_cell = "outer-loop (attended)"
        else:
            bead_cell = "MISSING - PR body carries no originating dev.task bead id"
        lines.append(
            "| "
            + " | ".join([
                _table(f"#{context.pr.number}"),
                _table(context.pr.title),
                _table(bead_cell),
                _table(_content_value(content, "lane")),
                _table(_content_value(content, "risk_class")),
                _table(extract_change_kind(context.pr.body)),
            ])
            + " |"
        )

    lines.extend(["", "## Bead Excerpts", ""])
    for context in contexts:
        lines.append(f"### PR #{context.pr.number} - {context.pr.title}")
        if not context.bead_id:
            if context.outer_loop:
                lines.extend([
                    "",
                    "This PR is explicitly marked outer-loop (attended). It carries no originating "
                    "dev.task bead by design; intent and verification evidence live in the PR body.",
                    "",
                ])
            else:
                lines.extend([
                    "",
                    "This PR carries no originating dev.task bead id in its body. This is a handoff defect.",
                    "",
                ])
            continue

        content = _content(context.bead)
        acceptance = _acceptance_lines(content.get("acceptance") or content.get("acceptance_criteria"))
        lines.extend([
            "",
            f"- Bead: `{context.bead_id}`",
            f"- Intent: {_first_line(content.get('intent'))}",
            "- Acceptance criteria:",
        ])
        if acceptance:
            lines.extend([f"  - {item}" for item in acceptance])
        else:
            lines.append("  - No acceptance criteria carried on the bead.")

        lines.append("- Notes:")
        note_lines = [_note_line(note) for note in context.notes]
        if note_lines:
            lines.extend([f"  - {line}" for line in note_lines])
        else:
            lines.append("  - No notes returned for this bead.")

        lines.append("- Linked beads:")
        linked_lines = [_linked_line(context.bead_id, link, context.linked_beads.get(link.other_id(context.bead_id) or "")) for link in context.links]
        linked_lines = [line for line in linked_lines if line]
        if linked_lines:
            lines.extend([f"  - {line}" for line in linked_lines])
        else:
            lines.append("  - No linked beads returned for this bead.")
        lines.append("")

    lines.extend([
        "## Merge Order",
        "",
        "SRE fills this section before handoff.",
        "",
        "- [ ] Required merge order:",
        "- [ ] Stacked-branch or retargeting notes:",
        "",
        "## Standing Disclosure Boilerplate",
        "",
        "Read GEMINI.md first. It defines the release-gate role, document spine, evidence rules, and output format.",
        "",
        "This manifest generator reads `SUBSTRATE_URL` and `SUBSTRATE_API_KEY` by name only; their values must never appear in this document.",
        "",
        "Your priorities, in order:",
        "1. Amendment conformance - did anything land that needed a ratified amendment first?",
        "2. Layer discipline - intent/outcome in beads, execution state in Temporal.",
        "3. Tidy-First - one declared Change kind per PR, structural and behavioral never mixed.",
        "4. Claim-to-evidence - is every number in each PR body reproducible?",
        "5. Cross-PR interaction and required merge order.",
        "",
        "Hold yourself to the evidence rules in GEMINI.md section 4. Verify before you assert.",
        "",
        "You can reach production data, and a claim about live bead state should be checked rather than listed as unverifiable. The substrate API key is not in the checkout; it is in the cluster. **Read it into a variable and never print it:**",
        "",
        "```bash",
        "KEY=$(kubectl --kubeconfig ~/.kube/config-cluster-a \\",
        "  -n platform-substrate-prod get secret substrate-secrets \\",
        "  -o jsonpath='{.data.SUBSTRATE_API_KEY}' | base64 -d)",
        'curl -s -H "X-API-Key: $KEY" "http://127.0.0.1:18001/beads?namespace=dev&limit=500"',
        "```",
        "",
        "Prod is `127.0.0.1:18001` behind `kubectl port-forward -n platform-substrate-prod svc/substrate 18001:8000`; 18000 is dev and a prod key 401s there. Quote the URL - zsh expands the `?`. Never interpolate the key into a command you write out, and never paste its value into a later command; if you must confirm which key you hold, print `printf '%s' \"$KEY\" | shasum -a 256 | cut -c1-16` and compare prefixes.",
        "",
        "Output per GEMINI.md section 5: ranked findings with file, line, what you checked, the failure scenario, and the fix; then MERGE / MERGE-WITH-CHANGES / DO-NOT-MERGE per PR with a required merge order; then the claims you could not verify, what you did not review, and proposed rules.",
        "",
        "PRs under review:",
        "```",
    ])
    lines.extend([str(context.pr.number) for context in contexts])
    lines.append("```")

    return scrub_secret_values("\n".join(lines) + "\n", env or os.environ)


def _content(bead: dict[str, Any] | None) -> dict[str, Any]:
    if not bead:
        return {}
    content = bead.get("content")
    return content if isinstance(content, dict) else {}


def _content_value(content: dict[str, Any], key: str) -> str:
    value = content.get(key)
    return str(value) if value not in (None, "") else "-"


def _first_line(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "No intent carried on the bead."
    return next((line.strip() for line in text.splitlines() if line.strip()), "No intent carried on the bead.")


def _acceptance_lines(value: Any) -> list[str]:
    if value is None:
        return []
    rows = value if isinstance(value, list) else [value]
    lines: list[str] = []
    for row in rows:
        if isinstance(row, dict):
            label = row.get("id") or row.get("name")
            text = row.get("statement") or row.get("text") or row.get("description") or row.get("criterion")
            combined = f"{label}: {text}" if label and text else label or text
            if combined:
                lines.append(str(combined))
        elif row not in (None, ""):
            lines.append(str(row))
    return lines


def _note_line(note: dict[str, Any]) -> str:
    content = _content(note)
    body = content.get("body") or content.get("note") or content.get("text") or content.get("summary")
    prefix = note.get("id") or content.get("title") or "note"
    return f"{prefix}: {_first_line(body)}"


def _linked_line(bead_id: str, link: LinkRecord, bead: dict[str, Any] | None) -> str:
    other_id = link.other_id(bead_id)
    if not other_id:
        return ""
    content = _content(bead)
    namespace = bead.get("namespace") if bead else None
    bead_type = bead.get("type") if bead else None
    kind = ".".join(str(part) for part in (namespace, bead_type) if part) or "bead"
    title = content.get("title") or content.get("intent") or content.get("decision") or content.get("summary") or ""
    suffix = f" - {_first_line(title)}" if title else ""
    return f"{link.direction_for(bead_id)} `{link.link_type}` {kind} `{other_id}`{suffix}"


def _table(value: str) -> str:
    return str(value).replace("|", r"\|").replace("\n", " ")


def scrub_secret_values(text: str, env: dict[str, str]) -> str:
    for name in SECRET_ENV_VARS:
        value = env.get(name)
        if value:
            text = text.replace(value, "")
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("prs", nargs="*", type=int, help="PR numbers to include")
    parser.add_argument(
        "--release",
        help="select PRs from this release's delivers edges instead of the positional list",
    )
    parser.add_argument("--fixture-dir", type=pathlib.Path, help="read PR and bead data from fixtures")
    args = parser.parse_args(argv)

    if bool(args.release) == bool(args.prs):
        parser.error("provide either PR numbers or --release, not both and not neither")

    try:
        provider: Provider = FixtureProvider(args.fixture_dir) if args.fixture_dir else LiveProvider()
        pr_numbers = resolve_release_prs(args.release, provider) if args.release else args.prs
        contexts = collect_context(pr_numbers, provider)
        print(render_manifest(contexts), end="")
    except (OSError, json.JSONDecodeError, subprocess.SubprocessError, RuntimeError) as exc:
        print(f"release-manifest: could not run: {scrub_secret_values(str(exc), os.environ)}", file=sys.stderr)
        return 2

    return 1 if any(context.has_handoff_defect for context in contexts) else 0


if __name__ == "__main__":
    sys.exit(main())
