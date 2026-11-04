"""Step-derived idempotency keys.

An agent that times out and retries, a run that is resumed and replays a
step, or two workers that pick up the same step all send the *same* call
again. A random key per call would make each of those a new write. A key
derived from what the step *is* - the run, the step, the tool and its
arguments - makes them the same write:

* the gateway uses it to find the approval that step already asked for,
  instead of queueing a second one, and to return a finished step's stored
  result instead of executing again;
* the downstream server receives it (as ``idempotency_key`` by default) on
  every write, so a vendor that dedupes on keys applies the write once even
  if the gateway itself crashes between sending and recording.

The ``step`` component is optional. A client that can name its steps (an
orchestrator with a step counter, MCP ``_meta``) should, because without it
two deliberately identical writes in the same run share a key and the second
is treated as a retry of the first. That is the safe direction to be wrong
in for writes, and it is stated as a limitation in the README.

The idempotency argument itself is excluded from the hash, so a client that
already sends its own key does not perturb the derived one.
"""

from __future__ import annotations

from typing import Any

from .canonical import digest

PREFIX = "gk_"


def step_key(run_id: str, tool: str, args: dict[str, Any], *, step: str = "", exclude: str = "idempotency_key") -> str:
    material = {
        "run": run_id,
        "step": step,
        "tool": tool,
        "args": {k: v for k, v in (args or {}).items() if k != exclude},
    }
    return PREFIX + digest(material)[:32]
