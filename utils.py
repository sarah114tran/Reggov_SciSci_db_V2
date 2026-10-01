"""
utils.py
Deterministic UUID generation so that re-running the loader
on the same source data never creates duplicate rows.

Usage:
    make_id("comment", "EPA-HQ-OAR-2021-0317", "comment-42")
    → always returns the same UUID string for those inputs
"""

import uuid

# A fixed namespace so IDs are stable across runs but scoped to this project.
_NAMESPACE = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")


def make_id(*parts: str) -> str:
    """
    Return a deterministic UUID string derived from the given parts.

    Args:
        *parts: Arbitrary strings that uniquely identify a record,
                e.g. make_id("comment", docket_id, raw_comment_index)

    Returns:
        A lowercase UUID string.
    """
    key = "|".join(str(p) for p in parts)
    return str(uuid.uuid5(_NAMESPACE, key))
