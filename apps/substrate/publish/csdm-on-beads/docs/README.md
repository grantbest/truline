# Example model

`architecture/model/*.yaml` is a minimal, self-consistent model that exists
to prove `../scripts/ea-conformance.py` runs clean against a repository that
isn't the one this tree was extracted from. It is deliberately small:

- `cap.example` — one capability, `operating`, with a dated
  `supports_none_reason` (nothing above it to support).
- `app.example` — one application realizing `cap.example`.
- `app.substrate` / `app.temporal` — two stand-in applications carrying the
  `FOUNDATION (Amendment 24...)` note the checker's foundation-grouping rule
  looks for. Name them whatever you like in your own model; the checker
  only cares that *some* application declares the grouping when your model
  has a foundation pair, or drop `check_foundation` from `main()` if your
  model has no such concept.

Run it from this tree's root:

```
EA_CANONICAL_CLUSTER=example-cluster python3 scripts/ea-conformance.py --no-baseline
```

This file (`docs/README.md`) is itself cited as evidence by every object in
the example model, which is why `check_evidence` — the rule that every
citation shaped like a repository path must actually exist — has something
real to check.
