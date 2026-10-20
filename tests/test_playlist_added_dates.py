from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yt_library import core

from tests.support import migrated_connection


class PlaylistAddedDateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.conn = migrated_connection(Path(self.temp_dir.name) / "library.sqlite3")
        self.addCleanup(self.conn.close)

    def scan(self, video_ids: list[str | None], *, observed_at: str = "2026-10-19T20:00:00Z") -> None:
        with patch("yt_library.core.utc_now", return_value=observed_at), self.conn:
            core.save_playlist_scan(
                self.conn,
                "PLdates",
                [
                    {
                        "video_id": video_id,
                        "position": 100 + index,
                        "title": "Fresh title" if video_id else "",
                        "availability": "public" if video_id else "unavailable",
                    }
                    for index, video_id in enumerate(video_ids)
                ],
                "ok",
                "",
            )

    def set_date(self, position: int, added_at: str | None, source: str = "youtube") -> None:
        with self.conn:
            self.conn.execute(
                """
                UPDATE playlist_items SET added_at = ?, source_quality = ?
                WHERE playlist_id = 'PLdates' AND position = ?
                """,
                (added_at, source, position),
            )

    def dates(self) -> list[tuple[str | None, str | None]]:
        return [
            (row["video_id"], row["added_at"])
            for row in self.conn.execute(
                """
                SELECT video_id, added_at FROM playlist_items
                WHERE playlist_id = 'PLdates' ORDER BY position
                """
            )
        ]

    def test_repeated_no_op_scans_preserve_dates_from_all_existing_sources(self) -> None:
        self.scan(["takeout", "api", "scanned", "unknown", "empty"])
        self.set_date(1, "2025-06-12T17:02:03Z", "takeout")
        self.set_date(2, "2026-09-01T01:02:03Z", "youtube_api")
        self.set_date(3, "2026-10-01T04:05:06Z")
        self.set_date(5, "")
        expected = [
            ("takeout", "2025-06-12T17:02:03Z"),
            ("api", "2026-09-01T01:02:03Z"),
            ("scanned", "2026-10-01T04:05:06Z"),
            ("unknown", None),
            ("empty", None),
        ]
        with self.conn:
            self.conn.execute(
                "UPDATE playlists SET last_changed_at = '2026-10-01T04:05:06Z' WHERE playlist_id = 'PLdates'"
            )
        for observed_at in ("2026-10-20T20:00:00Z", "2026-10-21T20:00:00Z"):
            self.scan(["takeout", "api", "scanned", "unknown", "empty"], observed_at=observed_at)
            self.assertEqual(self.dates(), expected)
            self.assertEqual(
                self.conn.execute("SELECT last_changed_at FROM playlists WHERE playlist_id = 'PLdates'").fetchone()[0],
                "2026-10-01T04:05:06Z",
            )
            self.assertEqual(
                {
                    tuple(row) for row in self.conn.execute(
                        "SELECT source_quality, updated_at FROM playlist_items"
                    )
                },
                {("youtube", observed_at)},
            )

    def test_unique_dates_follow_video_ids_through_reorder_and_insertions(self) -> None:
        self.scan(["one", "two"])
        self.set_date(1, "2025-01-01T01:00:00Z")
        self.set_date(2, "2025-02-01T02:00:00Z")
        self.scan(["two", "new", "one"])
        self.assertEqual(self.dates(), [
            ("two", "2025-02-01T02:00:00Z"),
            ("new", None),
            ("one", "2025-01-01T01:00:00Z"),
        ])

    def test_duplicate_dates_survive_unchanged_membership_in_their_own_slots(self) -> None:
        self.scan(["repeated", "other", "repeated"])
        self.set_date(1, "2025-01-01T01:00:00Z")
        self.set_date(3, "2025-02-01T02:00:00Z")
        for _ in range(2):
            self.scan(["repeated", "other", "repeated"])
            self.assertEqual(self.dates(), [
                ("repeated", "2025-01-01T01:00:00Z"),
                ("other", None),
                ("repeated", "2025-02-01T02:00:00Z"),
            ])

    def test_changed_duplicate_membership_does_not_guess_occurrence_dates(self) -> None:
        for after in (
            ["repeated", "repeated", "other"],
            ["repeated", "other"],
            ["repeated", "other", "repeated", "repeated"],
        ):
            with self.subTest(after=after):
                self.scan(["repeated", "other", "repeated"])
                self.set_date(1, "2025-01-01T01:00:00Z")
                self.set_date(2, "2025-03-01T03:00:00Z")
                self.set_date(3, "2025-02-01T02:00:00Z")
                self.scan(after)
                self.assertEqual(self.dates(), [
                    (video_id, "2025-03-01T03:00:00Z" if video_id == "other" else None)
                    for video_id in after
                ])

    def test_new_duplicate_cannot_inherit_a_single_existing_date(self) -> None:
        self.scan(["repeated"])
        self.set_date(1, "2025-01-01T01:00:00Z")
        self.scan(["repeated", "repeated"])
        self.assertEqual(self.dates(), [("repeated", None), ("repeated", None)])

    def test_hidden_slots_never_match_dates_by_position(self) -> None:
        self.scan(["known", None])
        self.set_date(1, "2025-01-01T01:00:00Z", "takeout")
        self.set_date(2, "2025-02-01T02:00:00Z")
        self.scan([None, "new"])
        self.assertEqual(self.dates(), [
            (None, None),
            ("new", None),
            ("known", "2025-01-01T01:00:00Z"),
        ])
        self.assertEqual(
            self.conn.execute("SELECT membership_state FROM playlist_items WHERE video_id = 'known'").fetchone()[0],
            "retained_unavailable",
        )
        self.scan(["known", "new"])
        self.assertEqual(self.dates(), [("known", "2025-01-01T01:00:00Z"), ("new", None)])

    def test_removed_then_readded_member_does_not_reuse_an_old_date(self) -> None:
        self.scan(["one"])
        self.set_date(1, "2025-01-01T01:00:00Z")
        self.scan([])
        self.scan(["one"])
        self.assertEqual(self.dates(), [("one", None)])

    def test_retained_duplicate_is_not_a_unique_date_match(self) -> None:
        self.scan(["one"])
        self.set_date(1, "2025-01-01T01:00:00Z")
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO playlist_items(
                    playlist_id, position, video_id, membership_state, source_quality, added_at
                ) VALUES ('PLdates', 2000, 'one', 'retained_unavailable', 'takeout', '2025-02-01T02:00:00Z')
                """
            )
        self.scan(["one"])
        self.assertEqual(self.dates(), [("one", None)])

    def test_dates_are_scoped_to_the_playlist(self) -> None:
        self.scan(["one"])
        with self.conn:
            self.conn.execute("INSERT INTO playlists(playlist_id) VALUES ('PLother')")
            self.conn.execute(
                """
                INSERT INTO playlist_items(playlist_id, position, video_id, added_at)
                VALUES ('PLother', 1, 'one', '2025-01-01T01:00:00Z')
                """
            )
        self.scan(["one"])
        self.assertEqual(self.dates(), [("one", None)])

    def test_error_and_header_only_observations_leave_member_dates_alone(self) -> None:
        self.scan(["one"])
        self.set_date(1, "2025-01-01T01:00:00Z")
        with self.conn:
            core.save_playlist_scan_error(self.conn, "PLdates", "Incomplete result")
        self.assertEqual(self.dates(), [("one", "2025-01-01T01:00:00Z")])
        with self.conn:
            core.save_playlist_metadata_observation(
                self.conn, "PLdates", {"title": "Header only", "video_count": 1}
            )
        self.assertEqual(self.dates(), [("one", "2025-01-01T01:00:00Z")])


if __name__ == "__main__":
    unittest.main()
