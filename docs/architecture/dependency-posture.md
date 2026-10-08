# Dependency posture — the one definition

An application's dependency posture has exactly one definition. Without that, "any `depends_on`
edge", "an application-to-application edge" and "the `content.depends_on` field" each yield a
different candidate list, and a rule written against posture means whichever an author picked.

The definition is the one `apps/factory-dispatcher/activities/ea_dependency.py` computes
(`land_dependency_posture`, `summarize_dependency_posture`), and nothing else may define it:

- **`known`** — the application has at least one outgoing `depends_on` edge whose target is
  another `arch.application`.
- **`assessed_none`** — no such edge, and a dated assessed-none `arch.observation` for the
  application exists (its ref is deterministic per application).
- **`unknown`** — neither.
- **coherence case** — counted in `unknown`, listed separately: the application's only
  `depends_on` edges target `arch.ci` (the observer's technology layer), so it depends on
  something but the model has not classified what.

An edge wins over an observation when both exist. `content.depends_on` on an `arch.application`
is not posture: dependencies live as typed links, the field is empty on every live application,
and `scripts/ea-conformance.py`'s DERIVED check fails a hand-authored copy. `scripts/ea-coverage.py`
reports posture only from the observer's standing status bead (`obs.ea-observer-status`,
`context.dependency_posture`); it never computes one. `apps/mcp-hub/src/tools/impact.py` collapses
`known` and `assessed_none` into its own `known` on purpose, and a committed fixture, byte-identical
in both apps' test trees, proves the two classifiers agree.
