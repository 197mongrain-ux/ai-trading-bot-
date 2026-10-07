"""Score and volume tag. Both are journal-only.

A 7/9 or 9/9 score does not arm. A low score does not block.
``HEAVY`` / ``VOL_OK`` never vetoes.

The default score is tape density (prints // 10, capped at 9) so a thick
tape with no sweep still prints 9/9 and stays flat, while a valid 30-print
reclaim prints 3/9 and can still arm. Pass ``score=`` into the engine to
log an external Model 3 number instead — it is still not an arm gate, and
it is not combined with a FLOW_OK pre-place check. Two readers use it.
The closer-ticker cancel keeps a resting Alo whose score is strictly
above the candidate; equal scores still follow the closer-bps swap.
The close-margin reserve keeps its fraction for the highest score among
close setups, then the closer swing in bps.
"""

from __future__ import annotations

HEAVY_PRINTS = 60


def log_only_score(print_count: int) -> int:
    if print_count <= 0:
        return 0
    return max(0, min(9, int(print_count) // 10))


def volume_tag(print_count: int) -> str:
    if print_count >= HEAVY_PRINTS:
        return "HEAVY"
    return "VOL_OK"
