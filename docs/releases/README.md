# Release charters

A release is one three-sprint arc, chartered before its sprints are planned. The charter holds the
objective and decomposes it into outcomes; a sprint holds only the time box. Every task cites the
outcome it serves through a `delivers` edge.

`R00.01.json` is an example in the shape `scripts/release-load.py --check` validates. Replace it
with your own; keep three charters in `planned` or `in_flight` at all times.

| Field | Meaning |
|---|---|
| `ref` | the release id, cited by tasks and by `arch.release` beads |
| `objective` | one paragraph of what is different once the release lands |
| `sprints` | the sprint ids inside the arc |
| `opened_at`, `target_at` | the time box |
| `declared_balance` | intended share per work class: `feature`, `enabling`, `blocking`, `risk`, `security` |
| `outcomes[]` | id, work class, statement written for the person the release is for, and the requirement criteria (`REQ-ID/AC-n`) that prove it |

Balance is reported, not enforced: `scripts/release-status.py` shows declared against actual and
counts the work bound to no release. `policy/health-policy.json` holds the thresholds the release
health report applies.
