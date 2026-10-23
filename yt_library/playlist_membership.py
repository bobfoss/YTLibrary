"""Reconcile current memberships without replacing surviving database rows."""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import sqlite3
from typing import Any


class AmbiguousMembershipError(ValueError):
    """A scan cannot safely identify existing duplicate occurrences."""


@contextmanager
def membership_transaction(conn: sqlite3.Connection):
    conn.execute("SAVEPOINT membership_reconcile")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK TO membership_reconcile")
        conn.execute("RELEASE membership_reconcile")
        raise
    else:
        conn.execute("RELEASE membership_reconcile")


def reconcile_memberships(
    conn: sqlite3.Connection,
    playlist_id: str,
    incoming: list[dict[str, Any]],
    now: str,
    *,
    observe_new: bool = False,
    retain_missing: bool = True,
    remove_missing: bool = True,
) -> None:
    from .playlist_dates import valid_added_date
    previous = [dict(row) for row in conn.execute(
        "SELECT rowid AS membership_rowid, * FROM playlist_items WHERE playlist_id=? ORDER BY position",
        (playlist_id,),
    )]
    old_counts = Counter(row["video_id"] for row in previous if row["video_id"])
    new_counts = Counter(row.get("video_id") for row in incoming if row.get("video_id"))
    old_sets = Counter(row["set_video_id"] for row in previous if row["set_video_id"])
    new_sets = Counter(row.get("set_video_id") for row in incoming if row.get("set_video_id"))
    used: set[int] = set()
    plan: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    for index, item in enumerate(incoming, 1):
        item = dict(item, position=index)
        video_id = item.get("video_id") or None
        set_id = item.get("set_video_id") or ""
        match = None
        if set_id and old_sets[set_id] == new_sets[set_id] == 1:
            match = next(row for row in previous if row["set_video_id"] == set_id)
            if match["video_id"] and video_id and match["video_id"] != video_id:
                raise AmbiguousMembershipError("Conflicting playlist occurrence identity")
        elif set_id and new_sets[set_id] == 1 and all(row["set_video_id"] for row in previous if row["video_id"] == video_id):
            pass  # A new explicit occurrence, including remove/re-add or duplicates.
        elif video_id and old_counts[video_id] == new_counts[video_id] == 1:
            candidate = next(row for row in previous if row["video_id"] == video_id)
            if not (set_id and candidate["set_video_id"] and candidate["set_video_id"] != set_id):
                match = candidate
        elif video_id and old_counts[video_id]:
            old_positions = [row["position"] for row in previous if row["video_id"] == video_id]
            new_positions = [i for i, row in enumerate(incoming, 1) if row.get("video_id") == video_id]
            if old_positions == new_positions:
                match = next(row for row in previous if row["position"] == index)
            else:
                raise AmbiguousMembershipError("Duplicate membership changed without stable occurrence IDs; prior membership preserved")
        elif not video_id:
            match = next((row for row in previous if not row["video_id"] and row["position"] == index), None)
        if match:
            row_id = match["membership_rowid"]
            if row_id in used:
                raise AmbiguousMembershipError("Repeated playlist occurrence identity")
            used.add(row_id)
        plan.append((item, match))

    current_ids = {item.get("video_id") for item in incoming if item.get("video_id")}
    retained_position = (len(incoming) + 1) * 1000
    for row in previous:
        if row["membership_rowid"] in used:
            continue
        if not remove_missing or (retain_missing and row["video_id"] not in current_ids and (
            row["membership_state"] == "retained_unavailable" or row["source_quality"] == "takeout"
        )):
            state = "retained_unavailable" if remove_missing else row["membership_state"]
            plan.append((dict(row, position=retained_position, membership_state=state), row))
            used.add(row["membership_rowid"])
            retained_position += 1

    columns = ("position", "video_id", "membership_state", "unavailable_kind", "source_quality",
               "match_type", "match_confidence", "set_video_id", "added_at", "added_at_source", "first_seen_at")
    with membership_transaction(conn):
        # Temporarily park only moving rows to avoid position-key collisions.
        floor = min([0] + [row["position"] for row in previous]) - len(previous) - 1
        for offset, row in enumerate(previous):
            if row["membership_rowid"] not in used:
                conn.execute("DELETE FROM playlist_items WHERE rowid=?", (row["membership_rowid"],))
            elif any(old is row and item["position"] != row["position"] for item, old in plan):
                conn.execute("UPDATE playlist_items SET position=? WHERE rowid=?", (floor - offset, row["membership_rowid"]))
        for item, old in plan:
            video_id = item.get("video_id") or None
            item["added_at"] = valid_added_date(item.get("added_at")) or None
            added = (old or {}).get("added_at") or item.get("added_at") or None
            source = (old or {}).get("added_at_source") or "unknown"
            if item.get("added_at") and item["added_at"] == added and (not old or not old.get("added_at") or source == "unknown"):
                source = item.get("added_at_source") or "unknown"
            first_seen = (old or {}).get("first_seen_at")
            if old is None and observe_new and video_id and (new_counts[video_id] == 1 or (item.get("set_video_id") and new_sets[item["set_video_id"]] == 1)):
                first_seen = now
            values = {
                "position": item["position"], "video_id": video_id,
                "membership_state": item.get("membership_state") or ("current" if video_id else "unresolved_unavailable"),
                "unavailable_kind": item.get("unavailable_kind") or (item.get("availability") or "unavailable" if not video_id else ""),
                "source_quality": item.get("source_quality") or "youtube",
                "match_type": item.get("match_type") or ("" if video_id else "ambiguous_hidden_slot"),
                "match_confidence": item.get("match_confidence") or ("video_id" if video_id else "hidden_slot_only"),
                "set_video_id": item.get("set_video_id") or (old or {}).get("set_video_id") or "",
                "added_at": added, "added_at_source": source, "first_seen_at": first_seen,
            }
            if old:
                if any(old[key] != values[key] for key in columns):
                    conn.execute(
                        "UPDATE playlist_items SET " + ", ".join(key + "=?" for key in columns) + ", updated_at=? WHERE rowid=?",
                        (*[values[key] for key in columns], now, old["membership_rowid"]),
                    )
            else:
                conn.execute(
                    "INSERT INTO playlist_items(playlist_id, " + ", ".join(columns) + ", updated_at) VALUES (" + ",".join("?" for _ in range(len(columns) + 2)) + ")",
                    (playlist_id, *[values[key] for key in columns], now),
                )
