"""Ten agents, one slot, over the whole stack.

The project's loudest claim is that double-booking is impossible, and that a
caller who loses the race is told something it can act on. This runs that claim:
ten concurrent `book_appointment` calls for the same slot, each completing its
own confirmation round trip, over OAuth and HTTP rather than against the
database.

    make up && make race

Before the guard in `backend/domain/services.py`, the same command answered some
of the losers with INTERNAL_ERROR: the optimistic `version_id` on `agenda_slot`
raises `StaleDataError`, a different class from the `IntegrityError` the code
caught, so it escaped as an unhandled 500. The race is timing dependent, so it
did not happen every run, which is exactly why it survived the test suite.

Output is kept under 48 columns so it stays readable when it is shown to
someone rather than read in a wide terminal.
"""

from __future__ import annotations

import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from scripts.probe import (
    answer_for,
    bookable_pair,
    code_of,
    message_of,
    rpc,
    sql,
    token,
)

#: Long codes shortened only for width. The full code is what the API returns.
SHORT = {
    "PATIENT_ALREADY_BOOKED": "ALREADY_BOOKED",
    "CONCURRENCY_CONFLICT": "CONFLICT",
}


def race(access: str, slot_id: int, patient_id: int) -> tuple[int, str, str]:
    """One agent's whole path: propose, approve, execute."""
    arguments = {"patient_id": patient_id, "slot_id": slot_id}
    paused: dict[str, Any] = rpc(
        access, "tools/call", {"name": "book_appointment", "arguments": arguments}
    )
    if paused.get("resultType") != "input_required":
        return patient_id, code_of(paused), message_of(paused)
    done: dict[str, Any] = rpc(
        access,
        "tools/call",
        {
            "name": "book_appointment",
            "arguments": arguments,
            "requestState": paused["requestState"],
            "inputResponses": answer_for(paused),
        },
    )
    return patient_id, code_of(done), message_of(done)


def alternatives_in(message: str) -> str:
    """The times the refusal offers, which is the part that makes it useful."""
    if "closest free slots are:" not in message:
        return ""
    offered = message.split("closest free slots are:")[1]
    times = [part.strip().split(" ")[1] for part in offered.split(",")[:2] if " " in part.strip()]
    return "  next: " + " ".join(times) if times else ""


def main() -> int:
    access = token()
    _, slot_id, _ = bookable_pair(access, set())
    found = rpc(
        access, "tools/call", {"name": "search_patients", "arguments": {"name": "a", "limit": 25}}
    )
    patients = [int(p.get("patient_id") or p["id"]) for p in found["structuredContent"]["result"]][
        :10
    ]
    while len(patients) < 10:
        patients.append(patients[0])

    started = time.time()
    with ThreadPoolExecutor(max_workers=10) as pool:
        rows = list(pool.map(lambda p: race(access, slot_id, p), patients))
    elapsed = time.time() - started

    for patient_id, code, message in rows:
        if code == "OK":
            print(f"  agent {patient_id:<3}  BOOKED")
        else:
            print(f"  agent {patient_id:<3}  {SHORT.get(code, code)}{alternatives_in(message)}")

    counts = Counter(code for _, code, _ in rows)
    live = sql(
        f"select count(*) from appointment where slot_id={slot_id} "  # noqa: S608
        "and status in ('scheduled','confirmed','waiting','attended')"
    )
    print()
    print(f"  {'booked':<22}{counts.get('OK', 0)}")
    for code in sorted(c for c in counts if c != "OK"):
        print(f"  {SHORT.get(code, code):<22}{counts[code]}")
    print(f"  {'unhandled 500':<22}{counts.get('INTERNAL_ERROR', 0)}")
    print()
    print(f"  {'rows on that slot':<22}{live}")
    print(f"  {'wall clock':<22}{elapsed:.1f}s")
    # One row survives or the guarantee is broken, whatever the codes said.
    return 0 if live == "1" and not counts.get("INTERNAL_ERROR") else 1


if __name__ == "__main__":
    sys.exit(main())
