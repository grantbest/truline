from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import substrate as substrate_module  # noqa: E402
from substrate import Substrate  # noqa: E402


def test_add_note_posts_provenance(monkeypatch):
    captured = {}

    def fake_request(self, method, path, **kwargs):
        captured["method"] = method
        captured["path"] = path
        captured["json"] = kwargs["json"]
        return {"id": "note-1"}

    provenance = {
        "worker": "codex",
        "model": "codex-cli",
        "prompt_ref": "dev.task/task-1",
        "tokens": 0,
        "cost_usd": 0.0,
        "duration_s": 1.2,
    }

    monkeypatch.setenv("SUBSTRATE_URL", "https://substrate.example.test")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setattr(Substrate, "_request", fake_request)

    sub = Substrate()
    sub.add_note(
        "task-1",
        "status",
        "done",
        "factory-dispatcher/codex",
        provenance=provenance,
    )

    assert captured["method"] == "POST"
    assert captured["path"] == "/beads"
    assert captured["json"]["provenance"] == provenance


def test_find_bead_gets_beads_filtered_by_namespace_type_and_content_ref(monkeypatch):
    captured = {}

    def fake_request(self, method, path, **kwargs):
        captured["method"] = method
        captured["path"] = path
        captured["params"] = kwargs["params"]
        return [{"id": "principle-uuid-3", "content": {"ref": "PRIN-003"}}]

    monkeypatch.setenv("SUBSTRATE_URL", "https://substrate.example.test")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setattr(Substrate, "_request", fake_request)

    sub = Substrate()
    bead = sub.find_bead("arch", "principle", "PRIN-003")

    assert captured["method"] == "GET"
    assert captured["path"] == "/beads"
    assert captured["params"] == {
        "namespace": "arch",
        "type": "principle",
        "content_ref": "PRIN-003",
        "limit": 1,
    }
    assert bead == {"id": "principle-uuid-3", "content": {"ref": "PRIN-003"}}


def test_find_bead_returns_none_when_nothing_matches(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_URL", "https://substrate.example.test")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setattr(Substrate, "_request", lambda self, method, path, **kwargs: [])

    sub = Substrate()
    assert sub.find_bead("arch", "principle", "PRIN-999") is None


def test_create_task_posts_dev_task_pending_with_content(monkeypatch):
    captured = {}

    def fake_request(self, method, path, **kwargs):
        captured["method"] = method
        captured["path"] = path
        captured["json"] = kwargs["json"]
        return {"id": "new-bead-id", **kwargs["json"]}

    monkeypatch.setenv("SUBSTRATE_URL", "https://substrate.example.test")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setattr(Substrate, "_request", fake_request)

    sub = Substrate()
    content = {"title": "t", "intent": "i", "acceptance": ["a"]}
    bead = sub.create_task(content, "factory-dispatcher/file-task")

    assert captured["method"] == "POST"
    assert captured["path"] == "/beads"
    assert captured["json"] == {
        "namespace": "dev",
        "type": "task",
        "state": "pending",
        "trust_tier": "user",
        "created_by": "factory-dispatcher/file-task",
        "content": content,
    }
    assert bead["id"] == "new-bead-id"


def test_create_task_accepts_an_explicit_trust_tier(monkeypatch):
    captured = {}

    def fake_request(self, method, path, **kwargs):
        captured["json"] = kwargs["json"]
        return {"id": "new-bead-id"}

    monkeypatch.setenv("SUBSTRATE_URL", "https://substrate.example.test")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setattr(Substrate, "_request", fake_request)

    sub = Substrate()
    sub.create_task({}, "factory-dispatcher/scanner", trust_tier="system")

    assert captured["json"]["trust_tier"] == "system"


def test_add_link_posts_target_and_link_type_with_created_by_as_a_header(monkeypatch):
    captured = {}

    def fake_request(self, method, path, **kwargs):
        captured["method"] = method
        captured["path"] = path
        captured["json"] = kwargs["json"]
        captured["headers"] = kwargs.get("headers")
        return {"id": "link-1", "source_id": "task-1", "target_id": "principle-uuid-3"}

    monkeypatch.setenv("SUBSTRATE_URL", "https://substrate.example.test")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setattr(Substrate, "_request", fake_request)

    sub = Substrate()
    sub.add_link("task-1", "principle-uuid-3", "applies", "factory-dispatcher/claude")

    assert captured["method"] == "POST"
    assert captured["path"] == "/beads/task-1/links"
    assert captured["json"] == {"target_id": "principle-uuid-3", "link_type": "applies"}
    assert captured["headers"] == {"X-Created-By": "factory-dispatcher/claude"}


def test_list_beads_gets_namespace_type_state_and_limit(monkeypatch):
    captured = {}

    def fake_request(self, method, path, **kwargs):
        captured["method"] = method
        captured["path"] = path
        captured["params"] = kwargs["params"]
        return [{"id": "release-1"}]

    monkeypatch.setenv("SUBSTRATE_URL", "https://substrate.example.test")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setattr(Substrate, "_request", fake_request)

    sub = Substrate()
    beads = sub.list_beads("arch", "release", state="planned")

    assert captured["method"] == "GET"
    assert captured["path"] == "/beads"
    assert captured["params"] == {
        "namespace": "arch",
        "type": "release",
        "limit": 200,
        "state": "planned",
    }
    assert beads == [{"id": "release-1"}]


def test_list_beads_omits_state_when_not_given(monkeypatch):
    captured = {}

    def fake_request(self, method, path, **kwargs):
        captured["params"] = kwargs["params"]
        return []

    monkeypatch.setenv("SUBSTRATE_URL", "https://substrate.example.test")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setattr(Substrate, "_request", fake_request)

    sub = Substrate()
    sub.list_beads("arch", "release")

    assert captured["params"] == {"namespace": "arch", "type": "release", "limit": 200}


def test_list_tasks_is_list_beads_scoped_to_dev_task(monkeypatch):
    captured = {}

    def fake_request(self, method, path, **kwargs):
        captured["params"] = kwargs["params"]
        return []

    monkeypatch.setenv("SUBSTRATE_URL", "https://substrate.example.test")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setattr(Substrate, "_request", fake_request)

    sub = Substrate()
    sub.list_tasks(state="pending")

    assert captured["params"] == {
        "namespace": "dev",
        "type": "task",
        "limit": 200,
        "state": "pending",
    }


def test_list_beads_pages_past_a_full_first_page_instead_of_truncating(monkeypatch):
    """A caller that gets back exactly ``limit`` rows cannot tell "that is
    everyone" from "there is more" -- list_beads must not let that ambiguity
    leak to its own caller. The fake backend enforces limit/offset exactly as
    the real substrate list endpoint does (see apps/substrate/src/routes.py's
    ``order_by(created_at.desc()).limit(limit).offset(offset)``) and holds
    more beads than the limit requested; a version that returned only the
    first page would fail this by dropping the tail.
    """
    all_beads = [{"id": f"bead-{i}"} for i in range(5)]
    seen_offsets = []

    def fake_request(self, method, path, **kwargs):
        params = kwargs["params"]
        offset = params.get("offset", 0)
        seen_offsets.append(offset)
        limit = params["limit"]
        return all_beads[offset : offset + limit]

    monkeypatch.setenv("SUBSTRATE_URL", "https://substrate.example.test")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setattr(Substrate, "_request", fake_request)

    sub = Substrate()
    beads = sub.list_beads("dev", "task", limit=2)

    assert beads == all_beads
    assert seen_offsets == [0, 2, 4]


def test_list_tasks_also_pages_past_the_limit(monkeypatch):
    """list_tasks delegates to list_beads -- scanner's coverage union and
    file_task's duplicate check both read through list_tasks, so the fix has
    to hold there too, not just on the lower-level method it wraps.
    """
    all_tasks = [{"id": f"task-{i}"} for i in range(7)]

    def fake_request(self, method, path, **kwargs):
        params = kwargs["params"]
        offset = params.get("offset", 0)
        limit = params["limit"]
        return all_tasks[offset : offset + limit]

    monkeypatch.setenv("SUBSTRATE_URL", "https://substrate.example.test")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setattr(Substrate, "_request", fake_request)

    sub = Substrate()
    assert sub.list_tasks(limit=3) == all_tasks


def test_request_merges_extra_headers_over_the_api_key_header(monkeypatch):
    """The X-Created-By header add_link relies on must not clobber X-API-Key."""
    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"ok": True}

    def fake_httpx_request(method, url, headers=None, **kwargs):
        captured["headers"] = headers
        return FakeResponse()

    monkeypatch.setenv("SUBSTRATE_URL", "https://substrate.example.test")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    import substrate as substrate_module

    monkeypatch.setattr(substrate_module.httpx, "request", fake_httpx_request)

    sub = Substrate()
    sub._request(
        "POST",
        "/beads/x/links",
        headers={"X-Created-By": "someone", "Content-Type": "text/plain"},
    )

    assert captured["headers"]["X-API-Key"] == "test-key"
    assert captured["headers"]["X-Created-By"] == "someone"
    # Precedence, not just presence. X-Created-By never collides with the
    # standing headers, so asserting it proves only that the merge happened --
    # inverting the merge to {**headers, **self._headers} left this test green.
    # Content-Type IS in the standing set, so this pins per-call wins.
    assert captured["headers"]["Content-Type"] == "text/plain"


def test_paging_raises_at_the_bound_rather_than_looping_on_a_server_ignoring_offset(
    monkeypatch,
):
    """A server that ignores ``offset`` returns a full page forever. Unbounded,
    that is a hot request loop and unbounded memory in a nightly -- worse than
    the truncation pagination exists to fix -- so the walk raises at MAX_PAGES
    instead of spinning or silently returning a short answer.
    """
    calls = {"n": 0}

    def fake_request(self, method, path, **kwargs):
        calls["n"] += 1
        limit = kwargs["params"]["limit"]
        return [{"id": f"b{calls['n']}"} for _ in range(limit)]  # never short: no exhaustion

    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setattr(substrate_module.Substrate, "_request", fake_request)

    sub = substrate_module.Substrate()
    with pytest.raises(substrate_module.SubstrateError) as excinfo:
        sub.list_tasks(limit=10)

    assert calls["n"] == substrate_module.MAX_PAGES
    message = str(excinfo.value)
    assert "did not exhaust" in message
    assert "offset" in message
    # The refusal must say what it is protecting, not just that it stopped.
    assert "silently incomplete" in message


def test_paging_refuses_a_nonpositive_limit_instead_of_looping_forever(monkeypatch):
    """``limit=0`` satisfies neither exit condition: ``0 < 0`` is false and
    ``offset += 0`` never advances. Refuse at the door."""
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")

    sub = substrate_module.Substrate()
    for bad in (0, -1):
        with pytest.raises(ValueError, match="limit must be positive"):
            sub.list_tasks(limit=bad)
