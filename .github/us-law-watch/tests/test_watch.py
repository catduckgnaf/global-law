import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("w", Path(__file__).resolve().parents[1] / "scripts" / "us_law_watch.py")
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)


def corpus(pin, status="current", repo="lexifina/x"):
    return {"pin": pin, "repo": repo, "history": "in-force", "coverage": "c", "status": status, "last_verified": "2026-11-01"}


OLD = {"upstream_commit": "a" * 40, "tools_tree": "t1", "judgments_us_tree": "j1", "corpora": {
    "united-states/federal/usc": corpus("1" * 40),
    "united-states/connecticut/caselaw": corpus("2" * 40),
    "united-states/ohio/acts": corpus("3" * 40),
    "united-states/gone/acts": corpus("4" * 40)}}
NEW = {"upstream_commit": "b" * 40, "tools_tree": "t2", "judgments_us_tree": "j1", "corpora": {
    "united-states/federal/usc": corpus("5" * 40, status="stale"),
    "united-states/connecticut/caselaw": corpus("6" * 40),
    "united-states/ohio/acts": corpus("3" * 40),
    "united-states/new/acts": corpus("7" * 40)}}


class DiffTests(unittest.TestCase):
    def test_diff(self):
        d = w.diff_snapshots(OLD, NEW)
        self.assertEqual(d["advanced"], ["united-states/connecticut/caselaw", "united-states/federal/usc"])
        self.assertEqual(d["added"], ["united-states/new/acts"])
        self.assertEqual(d["removed"], ["united-states/gone/acts"])
        self.assertEqual(d["status_changes"], [("united-states/federal/usc", "current", "stale")])
        self.assertTrue(d["tools_changed"])
        self.assertFalse(d["judgments_changed"])

    def test_no_change(self):
        d = w.diff_snapshots(NEW, NEW)
        self.assertFalse(any([d["advanced"], d["added"], d["removed"], d["status_changes"], d["tools_changed"]]))

    def test_report_mentions_everything(self):
        d = w.diff_snapshots(OLD, NEW)
        details = {"united-states/federal/usc": {"status": "ahead", "total": 20, "commits": [f"Pub. L. 119-{i}" for i in range(20)], "files": 3, "files_capped": False},
                   "united-states/connecticut/caselaw": {"error": "HTTP 404 from the compare API"}}
        text = w.render(OLD, NEW, d, details, ["united-states/federal/usc", "united-states/connecticut/caselaw"], "2026-11-30")
        for needle in ("2026-11-30", "Pub. L. 119-0", "and 8 more", "united-states/new/acts", "united-states/gone/acts",
                       "tools/", "stale", "| error |", "not currently verified"):
            self.assertIn(needle, text)

    def test_diverged_pin_is_flagged(self):
        d = w.diff_snapshots(OLD, NEW)
        text = w.render(OLD, NEW, d, {"united-states/federal/usc": {"status": "diverged", "total": 1, "commits": ["x"], "files": 1, "files_capped": False}}, [], "d")
        self.assertIn("history may have been rewritten", text)


class SnapshotTests(unittest.TestCase):
    def test_snapshot_reads_pins_urls_and_scope(self):
        with tempfile.TemporaryDirectory() as t:
            r = Path(t)
            def g(*a):
                subprocess.run(["git", "-C", t, *a], check=True, capture_output=True)
            g("init", "-q", "-b", "main")
            g("config", "user.email", "a@b")
            g("config", "user.name", "a")
            (r / "data").mkdir()
            (r / "data/freshness.tsv").write_text("path\tstatus\tlast_verified\nunited-states/ohio/acts\tcurrent\t2026-11-01\n")
            (r / "manifest.json").write_text(json.dumps({"corpora": [{"path": "united-states/ohio/acts", "history": "current-only", "coverage": "x"}]}))
            (r / ".gitmodules").write_text('[submodule "united-states/ohio/acts"]\n\tpath = united-states/ohio/acts\n\turl = https://github.com/lexifina/united-states-ohio-law.git\n'
                                           '[submodule "france/national/acts"]\n\tpath = france/national/acts\n\turl = https://github.com/lexifina/france-law.git\n')
            g("add", ".")
            sha = "9" * 40
            g("update-index", "--add", "--cacheinfo", f"160000,{sha},united-states/ohio/acts")
            g("update-index", "--add", "--cacheinfo", f"160000,{sha},france/national/acts")
            g("commit", "-qm", "x")
            snap = w.take_snapshot(r)
            self.assertEqual(list(snap["corpora"]), ["united-states/ohio/acts"])
            c = snap["corpora"]["united-states/ohio/acts"]
            self.assertEqual((c["pin"], c["repo"], c["status"], c["history"]), (sha, "lexifina/united-states-ohio-law", "current", "current-only"))


if __name__ == "__main__":
    unittest.main()
