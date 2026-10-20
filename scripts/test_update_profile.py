"""Focused checks for complete public snapshots and honest language shares."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("profile", Path(__file__).with_name("update_profile.py"))
profile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(profile)


def repo(identifier, name=None, fork=False):
    return {"id": identifier, "name": name or f"repo-{identifier}", "owner": {"login": "example"}, "private": False, "fork": fork, "stargazers_count": 0}


class SnapshotTests(unittest.TestCase):
    def test_pagination_reads_beyond_first_page(self):
        with patch.object(profile, "get_json", side_effect=[[repo(i) for i in range(100)], [repo(100)]]):
            self.assertEqual(len(profile.fetch_repos("example", None)), 101)

    def test_full_final_page_fails_instead_of_truncating(self):
        with patch.object(profile, "get_json", return_value=[repo(i) for i in range(100)]):
            with self.assertRaises(RuntimeError):
                profile.fetch_repos("example", None, max_pages=1)

    def test_exactly_full_page_requires_a_following_empty_page(self):
        with patch.object(profile, "get_json", side_effect=[[repo(i) for i in range(100)], []]) as request:
            self.assertEqual(len(profile.fetch_repos("example", None)), 100)
            self.assertEqual(request.call_count, 2)

    def test_duplicate_pages_fail(self):
        first = [repo(i) for i in range(100)]
        with patch.object(profile, "get_json", side_effect=[first, [repo(0)]]):
            with self.assertRaises(ValueError):
                profile.fetch_repos("example", None)

    def test_languages_exclude_forks_and_profile_case_insensitively(self):
        with patch.object(profile, "get_json", return_value={"Python": 70, "HTML": 30}) as request:
            totals, analyzed, populated = profile.fetch_languages("example", [repo(1, "Example"), repo(2, fork=True), repo(3)], None)
        self.assertEqual((totals, analyzed, populated), ({"Python": 70, "HTML": 30}, 1, 1))
        request.assert_called_once_with("/repos/example/repo-3/languages", None)

    def test_top_five_retain_denominator_and_other_bytes(self):
        totals = profile.Counter({"A": 50, "B": 20, "C": 10, "D": 8, "E": 7, "F": 3, "G": 2})
        rows = profile.language_rows(totals)
        self.assertEqual(rows[-1], ("Other", 5))
        self.assertEqual(sum(value for _, value in rows), 100)
        svg = profile.build_metrics("example", totals, 4, 3, "2026-10-20", profile.THEMES["light"], False)
        self.assertIn("50.0%", svg)
        self.assertIn("5.0%", svg)

    def test_api_failure_leaves_all_existing_assets_untouched(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = [Path(folder) / f"{kind}-{theme}.svg" for kind in ("stats", "metrics") for theme in ("light", "dark")]
            for path in paths:
                path.write_text("previous snapshot", encoding="utf-8")
            with patch("sys.argv", ["update_profile.py", "--output-dir", folder]), patch.object(profile, "get_json", side_effect=TimeoutError):
                with self.assertRaises(TimeoutError):
                    profile.main()
            self.assertTrue(all(path.read_text(encoding="utf-8") == "previous snapshot" for path in paths))

    def test_late_language_failure_leaves_previous_snapshot_untouched(self):
        with tempfile.TemporaryDirectory() as folder:
            old = Path(folder) / "metrics-light.svg"
            old.write_text("previous snapshot", encoding="utf-8")
            replies = [{"public_repos": 2}, [repo(1), repo(2)], {"Python": 50}, TimeoutError()]
            with patch("sys.argv", ["update_profile.py", "--login", "example", "--output-dir", folder]), patch.object(profile, "get_json", side_effect=replies):
                with self.assertRaises(TimeoutError):
                    profile.main()
            self.assertEqual(old.read_text(encoding="utf-8"), "previous snapshot")
            self.assertEqual(len(list(Path(folder).iterdir())), 1)

    def test_incomplete_repository_count_fails_before_language_fetch(self):
        with patch("sys.argv", ["update_profile.py", "--login", "example"]), patch.object(profile, "get_json", side_effect=[{"public_repos": 2}, [repo(1)]]):
            with self.assertRaises(RuntimeError):
                profile.main()


if __name__ == "__main__":
    unittest.main()
