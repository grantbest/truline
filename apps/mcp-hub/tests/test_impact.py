"""``tools.impact`` -- served impact analysis over the EA graph's typed edges
(R26.09/O-4, PC-ASR-002/AC-1). Every test here runs against the committed
fixture (``fixtures/impact-graph.example.json``) or a plain in-memory fake;
nothing reads the live store.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from tools import impact

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "impact-graph.example.json"


def _load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text())


def _population(fixture: dict, *, partial: bool = False, partial_reads=None) -> impact.GraphPopulation:
    return impact.GraphPopulation(
        changes=fixture["changes"],
        applications=fixture["applications"],
        cis=fixture["cis"],
        services=fixture["services"],
        observations=fixture.get("observations", []),
        links=fixture["links"],
        partial=partial,
        partial_reads=partial_reads or [],
    )


def _refs(entries) -> set:
    return {e["ref"] for e in entries}


# ---------------------------------------------------------------------------
# compute_impact -- pure function over the fixture
# ---------------------------------------------------------------------------


def test_change_with_affects_walks_to_ci_and_service_and_separates_unknown():
    population = _population(_load_fixture())
    result = impact.compute_impact("change", "chg.with-affects", population)

    assert _refs(result["affected"]["applications"]) == {"app.known"}
    # A change never directly targets a CI/service -- app.known's own
    # depends_on/consumes edges are context, not affected (PR #1043 gate,
    # .factory/design.md Sec 1).
    assert result["affected"]["cis"] == []
    assert result["affected"]["services"] == []
    assert _refs(result["context"]["runs_on"]["cis"]) == {"ci.owned"}
    assert _refs(result["context"]["runs_on"]["services"]) == {"svc.one"}
    # app.unknown is directly affected (an outgoing affects edge targets it)
    # but its own depends_on posture is unknown -- it must be reported, and
    # only in the unknown list, never in affected.applications.
    assert _refs(result["affected"]["unknown_applications"]) == {"app.unknown"}
    assert result["affected"]["lower_bound"] is True


def test_unknown_application_never_appears_in_affected_applications():
    population = _population(_load_fixture())
    result = impact.compute_impact("change", "chg.with-affects", population)

    affected_ids = {a["id"] for a in result["affected"]["applications"]}
    unknown_ids = {a["id"] for a in result["affected"]["unknown_applications"]}
    assert "app-2" in unknown_ids
    assert "app-2" not in affected_ids


def test_change_without_affects_edges_returns_empty_affected_sets_not_an_error():
    population = _population(_load_fixture())
    result = impact.compute_impact("change", "chg.without-affects", population)

    assert result["affected"]["applications"] == []
    assert result["affected"]["cis"] == []
    assert result["affected"]["services"] == []
    assert result["affected"]["unknown_applications"] == []
    assert result["affected"]["lower_bound"] is False


def test_application_origin_walks_forward_to_its_own_ci_and_service():
    # Required change 1(a), PR #1043 gate: an application origin's own
    # forward depends_on/consumes edges are context, never affected --
    # ci.owned/svc.one must appear ONLY under context.runs_on.
    population = _population(_load_fixture())
    result = impact.compute_impact("application", "app.known", population)

    assert _refs(result["affected"]["applications"]) == {"app.known"}
    assert result["affected"]["cis"] == []
    assert result["affected"]["services"] == []
    assert _refs(result["context"]["runs_on"]["cis"]) == {"ci.owned"}
    assert _refs(result["context"]["runs_on"]["services"]) == {"svc.one"}
    # Only known-posture applications reached -- the answer is a real ceiling
    # here, not a floor.
    assert result["affected"]["lower_bound"] is False


def test_ci_origin_finds_its_owning_application():
    # Required change 1(b), PR #1043 gate: a CI origin's owner's own forward
    # consumes edge (svc.one) is context, never affected; affected.cis holds
    # no CI other than the origin (here, none at all -- nothing populates it
    # under the current edge vocabulary, see compute_impact's own comment).
    population = _population(_load_fixture())
    result = impact.compute_impact("ci", "ci.owned", population)

    assert _refs(result["affected"]["applications"]) == {"app.known"}
    assert result["affected"]["services"] == []
    assert _refs(result["affected"]["cis"]) - {"ci.owned"} == set()
    assert _refs(result["context"]["runs_on"]["services"]) == {"svc.one"}


def test_unowned_ci_origin_has_no_owner_to_walk_from():
    population = _population(_load_fixture())
    result = impact.compute_impact("ci", "ci.unowned", population)

    assert result["affected"]["applications"] == []
    assert result["affected"]["cis"] == []
    assert result["affected"]["services"] == []


def test_unknown_ref_raises_not_found():
    population = _population(_load_fixture())
    with pytest.raises(impact.NotFound):
        impact.compute_impact("application", "app.does-not-exist", population)


def test_invalid_kind_raises_value_error():
    population = _population(_load_fixture())
    with pytest.raises(ValueError):
        impact.compute_impact("service", "svc.one", population)


# ---------------------------------------------------------------------------
# Posture from links/observations, not a content field (round-2 #1020 gate
# finding) -- and unknown-posture applications still expand into real CI/
# service edges, since posture decides completeness, not whether an edge is
# read.
# ---------------------------------------------------------------------------


def test_unknown_posture_application_edges_still_expand_to_ci_and_service():
    # app.ci-only depends_on a CI and consumes a service, but has no
    # application-to-application depends_on edge and no assessed-none
    # observation -- ea_dependency's "coherence case", unknown posture. The
    # CI and service it really depends on must still surface in the answer --
    # but as context, not affected (required change 1(a), PR #1043 gate).
    population = _population(_load_fixture())
    result = impact.compute_impact("application", "app.ci-only", population)

    assert _refs(result["affected"]["applications"]) == set()
    assert _refs(result["affected"]["unknown_applications"]) == {"app.ci-only"}
    assert result["affected"]["cis"] == []
    assert result["affected"]["services"] == []
    assert _refs(result["context"]["runs_on"]["cis"]) == {"ci.observed-only"}
    assert _refs(result["context"]["runs_on"]["services"]) == {"svc.observed-only"}
    assert result["affected"]["lower_bound"] is True


def test_ci_reached_through_an_unknown_posture_owner_still_finds_its_service():
    # Required change 1(c), PR #1043 gate: ci.observed-only's owner
    # (app.ci-only) has unknown posture -- its consumes edge to
    # svc.observed-only is still real and still read, but is context, not
    # affected.
    population = _population(_load_fixture())
    result = impact.compute_impact("ci", "ci.observed-only", population)

    assert _refs(result["affected"]["applications"]) == set()
    assert _refs(result["affected"]["unknown_applications"]) == {"app.ci-only"}
    assert result["affected"]["services"] == []
    assert _refs(result["context"]["runs_on"]["services"]) == {"svc.observed-only"}


def test_application_to_application_depends_on_edge_alone_makes_the_source_known():
    # app.dependent depends_on app.leaf -- an application-to-application
    # edge, with no assessed-none observation for either side -- makes
    # app.dependent's OWN posture known purely from that outgoing edge.
    # app.leaf is a forward dependency of the origin, not a dependent of it,
    # so the reverse-only closure (required change 2, .factory/design.md
    # Sec 2) never reaches it: it appears in neither affected.applications
    # nor unknown_applications.
    population = _population(_load_fixture())
    result = impact.compute_impact("application", "app.dependent", population)

    assert _refs(result["affected"]["applications"]) == {"app.dependent"}
    assert "app.leaf" not in _refs(result["affected"]["unknown_applications"])
    assert "app.leaf" not in _refs(result["affected"]["applications"])


def test_application_closure_never_walks_forward_into_a_dependency_and_back_out_to_its_other_dependents():
    # app.dependent and app.sibling both depends_on app.leaf (A and C both
    # depend on B) -- a change to app.dependent must propagate only to
    # applications that depend ON app.dependent (there are none in this
    # fixture), never sideways through the shared dependency app.leaf to
    # app.sibling. Round-2 #1024 gate finding: the previous both-direction
    # closure walked forward from app.dependent into app.leaf and then back
    # out along app.leaf's other reverse edge to app.sibling, incorrectly
    # reporting two independent consumers of the same dependency as
    # affecting each other.
    population = _population(_load_fixture())
    result = impact.compute_impact("application", "app.dependent", population)

    assert _refs(result["affected"]["applications"]) == {"app.dependent"}
    assert "app.sibling" not in _refs(result["affected"]["applications"])
    assert "app.sibling" not in _refs(result["affected"]["unknown_applications"])


def test_application_closure_does_propagate_forward_to_its_own_dependents():
    # The positive case for the same fixture: a change to app.leaf (the
    # shared dependency) DOES propagate to both app.dependent and
    # app.sibling -- they are its dependents (reverse depends_on edges), the
    # one direction impact actually travels.
    population = _population(_load_fixture())
    result = impact.compute_impact("application", "app.leaf", population)

    assert _refs(result["affected"]["unknown_applications"]) >= {"app.leaf"}
    assert _refs(result["affected"]["applications"]) >= {"app.dependent", "app.sibling"}


def test_assessed_none_observation_makes_an_application_known_without_an_app_edge():
    # app.known's posture comes entirely from its active assessed-none
    # arch.observation (obs-1) here -- its own depends_on edge targets a CI,
    # not an application, so the app-edge branch alone would not make it
    # known.
    population = _population(_load_fixture())
    result = impact.compute_impact("application", "app.known", population)

    assert _refs(result["affected"]["applications"]) == {"app.known"}
    assert _refs(result["affected"]["unknown_applications"]) == set()


# ---------------------------------------------------------------------------
# coverage -- measured from the exact population read, both numbers present
# ---------------------------------------------------------------------------


def test_coverage_is_measured_from_the_read_population_not_a_constant():
    fixture = _load_fixture()
    population = _population(fixture)
    result = impact.compute_impact("change", "chg.with-affects", population)
    coverage = result["coverage"]

    assert coverage["applications_total"] == len(fixture["applications"]) == 6
    # Known: app.known (assessed-none observation), app.dependent and
    # app.sibling (each has its own app-to-app depends_on edge). Unknown:
    # app.unknown, app.ci-only (CI-only), app.leaf.
    assert coverage["applications_assessed"] == 3
    assert coverage["cis_total"] == len(fixture["cis"]) == 3
    # ci.owned and ci.observed-only each have an incoming depends_on edge;
    # ci.unowned does not.
    assert coverage["cis_owned"] == 2
    assert coverage["changes_total"] == len(fixture["changes"]) == 2
    # Only chg.with-affects carries outgoing affects edges.
    assert coverage["changes_with_affects"] == 1
    assert coverage["partial"] is False
    assert coverage["partial_reads"] == []
    assert coverage["failed_types"] == []


def test_coverage_denominators_hold_regardless_of_which_node_is_queried():
    fixture = _load_fixture()
    population = _population(fixture)
    by_change = impact.compute_impact("change", "chg.without-affects", population)
    by_ci = impact.compute_impact("ci", "ci.unowned", population)

    assert by_change["coverage"]["applications_total"] == by_ci["coverage"]["applications_total"]
    assert by_change["coverage"]["cis_owned"] == by_ci["coverage"]["cis_owned"] == 2


def test_partial_flag_carries_through_from_the_population_read():
    population = _population(_load_fixture(), partial=True, partial_reads=["arch.service: boom"])
    result = impact.compute_impact("application", "app.known", population)

    assert result["coverage"]["partial"] is True
    assert result["coverage"]["partial_reads"] == ["arch.service: boom"]


def test_failed_types_carries_through_from_the_population_read():
    # Distinct from partial_reads (a human-readable log line): failed_types
    # is what the console checks to render a total as unknown ("--/--")
    # rather than a fabricated "0/0" for exactly the collections that failed
    # (round-2 #1024 gate finding, .factory/design.md Sec 6).
    population = _population(_load_fixture())
    population.failed_types = {"ci", "service"}
    result = impact.compute_impact("application", "app.known", population)

    assert result["coverage"]["failed_types"] == ["ci", "service"]


def test_coverage_totals_reflect_a_population_larger_than_the_fixture_alone():
    # Guards against a hardcoded/constant denominator: a population whose
    # sizes differ from the committed fixture's own counts must move the
    # totals, not just the numerators. Each addition also carries the edge
    # that would move its own NUMERATOR (round-2 #1024 gate finding: the
    # previous version of this test added bead-only, edge-less entries, so a
    # hardcoded numerator constant would have passed it undetected).
    fixture = _load_fixture()
    population = _population(fixture)
    population.applications = population.applications + [
        {"id": "app-extra", "namespace": "arch", "type": "application", "content": {"ref": "app.extra"}}
    ]
    population.cis = population.cis + [
        {"id": "ci-extra", "namespace": "arch", "type": "ci", "content": {"ref": "ci.extra"}}
    ]
    population.changes = population.changes + [
        {"id": "chg-extra", "namespace": "arch", "type": "change", "content": {"ref": "chg.extra"}}
    ]
    population.links = population.links + [
        # app-extra -> app.known: an app-to-app depends_on edge, moving
        # applications_assessed.
        {"id": "link-extra-app", "source_id": "app-extra", "target_id": "app-1", "link_type": "depends_on"},
        # app-extra -> ci-extra: gives ci-extra an incoming depends_on edge,
        # moving cis_owned.
        {"id": "link-extra-ci", "source_id": "app-extra", "target_id": "ci-extra", "link_type": "depends_on"},
        # chg-extra -> app.known: an outgoing affects edge, moving
        # changes_with_affects.
        {"id": "link-extra-chg", "source_id": "chg-extra", "target_id": "app-1", "link_type": "affects"},
    ]

    fixture_result = impact.compute_impact("change", "chg.with-affects", _population(fixture))
    result = impact.compute_impact("change", "chg.with-affects", population)
    coverage = result["coverage"]
    fixture_coverage = fixture_result["coverage"]

    assert coverage["applications_total"] == len(fixture["applications"]) + 1
    assert coverage["cis_total"] == len(fixture["cis"]) + 1
    assert coverage["changes_total"] == len(fixture["changes"]) + 1
    assert coverage["applications_assessed"] == fixture_coverage["applications_assessed"] + 1
    assert coverage["cis_owned"] == fixture_coverage["cis_owned"] + 1
    assert coverage["changes_with_affects"] == fixture_coverage["changes_with_affects"] + 1


# ---------------------------------------------------------------------------
# get_impact -- the async orchestrator, with injected fakes standing in for
# the packaged substrate client (never the live store)
# ---------------------------------------------------------------------------

_TYPE_TO_FIXTURE_KEY = {
    "change": "changes",
    "application": "applications",
    "ci": "cis",
    "service": "services",
    "observation": "observations",
}


def _fake_reads(fixture: dict):
    beads_by_type = {type_: fixture.get(key, []) for type_, key in _TYPE_TO_FIXTURE_KEY.items()}
    links_by_bead: dict[str, list] = {}
    for link in fixture["links"]:
        links_by_bead.setdefault(link["source_id"], []).append(link)
        links_by_bead.setdefault(link["target_id"], []).append(link)

    async def list_beads(namespace: str, type_: str, limit: int):
        assert namespace == "arch"
        return list(beads_by_type.get(type_, []))

    async def list_links(bead_id: str):
        return list(links_by_bead.get(bead_id, []))

    return list_beads, list_links


def test_get_impact_computes_over_injected_reads_and_stamps_provenance():
    fixture = _load_fixture()
    list_beads, list_links = _fake_reads(fixture)

    result = asyncio.run(
        impact.get_impact(
            "change", "chg.with-affects", list_beads=list_beads, list_links=list_links
        )
    )

    assert _refs(result["affected"]["applications"]) == {"app.known"}
    assert "computed_at" in result
    assert "revision_hint" in result


def test_get_impact_degrades_a_failed_read_to_partial_instead_of_raising():
    fixture = _load_fixture()
    _, list_links = _fake_reads(fixture)

    async def failing_list_beads(namespace: str, type_: str, limit: int):
        if type_ == "service":
            raise RuntimeError("substrate unreachable")
        return fixture[_TYPE_TO_FIXTURE_KEY[type_]]

    result = asyncio.run(
        impact.get_impact(
            "application", "app.known", list_beads=failing_list_beads, list_links=list_links
        )
    )

    assert result["coverage"]["partial"] is True
    assert any("arch.service" in reason for reason in result["coverage"]["partial_reads"])
    assert result["coverage"]["failed_types"] == ["service"]
    # The rest of the answer still comes back -- a failed service read must
    # not fail the whole response (PRIN-015).
    assert _refs(result["affected"]["applications"]) == {"app.known"}


def test_get_impact_never_reads_links_for_observation_beads():
    # Required change 3 (.factory/design.md Sec 4): the walk never follows an
    # edge touching an arch.observation bead -- posture comes from its own
    # state/content.ref, not a link -- so its links must never be fetched.
    # On the live graph this collection is the fastest-growing one (nightly
    # ea-observation runs), so this was also the fastest-growing part of the
    # read.
    fixture = _load_fixture()
    list_beads, _ = _fake_reads(fixture)
    observation_ids = {o["id"] for o in fixture.get("observations", [])}
    assert observation_ids, "fixture must carry at least one observation for this test to mean anything"

    called_ids: list[str] = []

    async def counting_list_links(bead_id: str):
        called_ids.append(bead_id)
        return []

    asyncio.run(
        impact.get_impact(
            "application", "app.known", list_beads=list_beads, list_links=counting_list_links
        )
    )

    assert not (set(called_ids) & observation_ids)
    expected_targets = (
        len(fixture["changes"])
        + len(fixture["applications"])
        + len(fixture["cis"])
        + len(fixture["services"])
    )
    assert len(called_ids) == expected_targets


def test_get_impact_raises_not_found_for_an_unknown_ref():
    fixture = _load_fixture()
    list_beads, list_links = _fake_reads(fixture)

    with pytest.raises(impact.NotFound):
        asyncio.run(
            impact.get_impact(
                "application", "app.nope", list_beads=list_beads, list_links=list_links
            )
        )


def test_get_impact_raises_unavailable_not_not_found_when_the_origins_own_read_fails():
    # round-2 #1020 gate finding: if the "application" collection read fails
    # outright, there is no basis to say "no bead with ref=app.known" -- that
    # 404 would assert a negative the read never established. This must
    # surface as Unavailable (-> 503 at the route), never NotFound.
    fixture = _load_fixture()
    _, list_links = _fake_reads(fixture)

    async def failing_list_beads(namespace: str, type_: str, limit: int):
        if type_ == "application":
            raise RuntimeError("substrate unreachable")
        return fixture[_TYPE_TO_FIXTURE_KEY[type_]]

    with pytest.raises(impact.Unavailable) as exc_info:
        asyncio.run(
            impact.get_impact(
                "application", "app.known", list_beads=failing_list_beads, list_links=list_links
            )
        )
    assert not isinstance(exc_info.value, impact.NotFound)


async def test_default_list_beads_pages_past_a_single_page(monkeypatch, httpx_mock):
    # round-2 #1020 gate finding: guard against `_default_list_beads`
    # ever regressing to a single-page (`[:limit]`-style) read. The packaged
    # substrate_client itself pages by offset until a short page -- this
    # drives that real client end to end through a fake httpx transport.
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")

    limit = 3
    full_page = [
        {"id": f"app-{i}", "namespace": "arch", "type": "application", "content": {"ref": f"app.{i}"}}
        for i in range(limit)
    ]
    short_page = [{"id": "app-3", "namespace": "arch", "type": "application", "content": {"ref": "app.3"}}]

    httpx_mock.add_response(method="GET", json=full_page)
    httpx_mock.add_response(method="GET", json=short_page)

    result = await impact._default_list_beads("arch", "application", limit)

    assert len(result) == limit + 1
    assert {b["id"] for b in result} == {f"app-{i}" for i in range(limit)} | {"app-3"}


# ---------------------------------------------------------------------------
# M12 client isolation (this bead's own AC): impact.py must reach the store
# only through the packaged substrate client, never a second
# header-constructing HTTP client.
# ---------------------------------------------------------------------------


def test_impact_module_never_constructs_an_x_api_key_header_directly():
    source = (Path(__file__).parent.parent / "src" / "tools" / "impact.py").read_text()
    assert "X-API-Key" not in source
    assert not re.search(r"^\s*import httpx", source, re.MULTILINE)
    assert not re.search(r"^\s*from httpx", source, re.MULTILINE)


def test_impact_module_reaches_the_store_only_through_the_finance_call_substrate_seam():
    source = (Path(__file__).parent.parent / "src" / "tools" / "impact.py").read_text()
    assert "_call_substrate" in source
    assert "from .finance import _call_substrate" in source
