"""AC-7/AC-2: the count SEC-a0166920-2 (the migration half of dev.finding a0166920) drives
to zero.

THE RULE FOR EVERY OTHER BEAD: a bead that adds a spawn call passes
`env=process_env.child_env(...)` (with `needs=` only for a credential that child
provably needs). It NEVER raises MAX_UNFIXED_SPAWN_CALLS above zero. The constant only
ever goes down -- it is now at the floor.

MAX_UNFIXED_SPAWN_CALLS was measured at 22, at origin/main 93aeb217 (independently
re-measured by a fresh critic; also re-verified when this test was added), and SEC-
a0166920-2 drove it to 0 by adding env=process_env.child_env(...) to every one of
those 22 call sites. Four subprocess.run call sites already passed an explicit env=
before that migration and were never in the unfixed count: dispatch.run,
dispatch.run_worker, dispatch.run_verification_shell, and
launchd_agent.validate_worker_dependencies. A fifth spawn site,
tunnel_keeper.TunnelKeeper._spawn_link (the injected `subprocess.Popen` default at
tunnel_keeper.py:256/:447), is invisible to this ast-based scan -- it is a bare
reference to the class used as a default parameter value, never a Call node --
SEC-a0166920-2 fixed it explicitly (it now passes env=process_env.child_env() at its
own call site) rather than relying on this count.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spawn_scan import find_unfixed_spawn_calls  # noqa: E402

FACTORY_DISPATCHER_DIR = Path(__file__).resolve().parents[1]

MAX_UNFIXED_SPAWN_CALLS = 0


def test_unfixed_spawn_call_count_never_exceeds_the_measured_ratchet():
    unfixed = find_unfixed_spawn_calls(FACTORY_DISPATCHER_DIR)

    if len(unfixed) > MAX_UNFIXED_SPAWN_CALLS:
        listing = "\n".join(f"  {c.path}:{c.line} {c.function}" for c in unfixed)
        raise AssertionError(
            f"{len(unfixed)} unfixed spawn calls exceeds the ratchet of "
            f"{MAX_UNFIXED_SPAWN_CALLS}:\n{listing}"
        )
