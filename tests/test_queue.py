import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from manage import DATA, export, fixtures, ingest, report, worker
from queue_store import Queue


class QueueTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.queue = Queue(self.folder / "test.sqlite3")

    def test_demo_retry_review_history_and_batch_export(self):
        batch = ingest(self.queue, DATA, "demo")["batch_id"]
        worker(self.queue, batch)
        outcome = report(self.queue, batch)
        self.assertEqual(outcome["states"], {"review": 3})
        self.assertEqual(outcome["retried_documents"], 1)
        self.assertEqual(outcome["flagged_documents"], 1)
        path = self.folder / "export.csv"
        with self.assertRaises(ValueError):
            export(self.queue, batch, path)
        self.assertFalse(path.exists())
        references = dict(fixtures().values())
        for job in outcome["jobs"]:
            correction = None
            if job["filename"] == "invoice-b.png":
                with self.assertRaises(ValueError):
                    self.queue.review(job["id"], None, "Incorrect total")
                correction = references[job["filename"]]
            self.queue.review(job["id"], correction, "Checked against source image")
            if correction:
                detail = self.queue.inspect(job["id"])
                self.assertEqual(detail["raw"]["total"], 160000)
                self.assertEqual(detail["reviewed"]["total"], 165000)
                self.assertEqual(detail["events"][-1]["detail"]["before"]["total"], 160000)
        export(self.queue, batch, path)
        self.assertEqual(len(path.read_text(encoding="utf-8-sig").splitlines()), 6)

    def test_expired_lease_can_be_reclaimed_and_stale_worker_cannot_write(self):
        batch = ingest(self.queue, DATA, "demo")["batch_id"]
        with patch("queue_store.time.time", return_value=1_000):
            old = self.queue.claim(batch)
        with patch("queue_store.time.time", return_value=1_181):
            new = self.queue.claim(batch)
            self.assertEqual(new["id"], old["id"])
            self.assertEqual(new["attempts"], 2)
            invoice = fixtures()[new["digest"]][1]
            self.assertEqual(self.queue.finish(old, invoice=invoice), "stale_claim")
            self.assertEqual(self.queue.finish(new, invoice=invoice), "review")

    def test_retry_budget_stops_repeated_failure(self):
        batch = ingest(self.queue, DATA, "demo")["batch_id"]
        for expected in ("queued", "queued", "failed"):
            job = self.queue.claim(batch)
            self.assertEqual(self.queue.finish(job, error="test failure"), expected)
        result = self.queue.inspect(job["id"])
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(result["state"], "failed")
