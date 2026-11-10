"""SQLite work queue with leased claims and a durable review history."""
import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager

from invoice import Invoice, issues

MAX_ATTEMPTS = 3
LEASE_SECONDS = 180
SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY, mode TEXT NOT NULL CHECK(mode IN ('demo','live'))
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES batches(id),
    filename TEXT NOT NULL, digest TEXT NOT NULL, image BLOB NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','running','review','approved','failed')),
    attempts INTEGER NOT NULL DEFAULT 0, available_at REAL NOT NULL DEFAULT 0,
    claim TEXT, lease_until REAL, raw_json TEXT, reviewed_json TEXT, error TEXT, processing_ms REAL,
    UNIQUE(batch_id, digest)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id),
    event TEXT NOT NULL, detail TEXT NOT NULL, at_epoch REAL NOT NULL
);
"""


def event(db, job_id, action, detail=""):
    db.execute("INSERT INTO events(job_id,event,detail,at_epoch) VALUES (?,?,?,?)",
               (job_id, action, detail, time.time()))


class Queue:
    def __init__(self, path):
        self.path = path
        with self.connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def ingest(self, mode, files):
        with self.connect() as db:
            batch_id = db.execute("INSERT INTO batches(mode) VALUES (?)", (mode,)).lastrowid
            for name, data in files:
                digest = hashlib.sha256(data).hexdigest()
                job = db.execute("INSERT INTO jobs(batch_id,filename,digest,image) VALUES (?,?,?,?)",
                                 (batch_id, name, digest, data)).lastrowid
                event(db, job, "queued")
            return batch_id

    def jobs(self, batch_id):
        with self.connect() as db:
            batch = db.execute("SELECT mode FROM batches WHERE id = ?", (batch_id,)).fetchone()
            if not batch:
                raise ValueError("Batch not found.")
            rows = db.execute("""SELECT id,filename,digest,state,attempts,raw_json,reviewed_json,error,processing_ms
                                 FROM jobs WHERE batch_id = ? ORDER BY id""", (batch_id,)).fetchall()
        return batch["mode"], [dict(row) for row in rows]

    def claim(self, batch_id):
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            expired = db.execute("""SELECT id,attempts FROM jobs WHERE batch_id = ?
                                     AND state = 'running' AND lease_until <= ?""", (batch_id, now)).fetchall()
            for job in expired:
                state = "failed" if job["attempts"] >= MAX_ATTEMPTS else "queued"
                db.execute("UPDATE jobs SET state=?,claim=NULL,lease_until=NULL,error=? WHERE id=?",
                           (state, "worker lease expired", job["id"]))
                event(db, job["id"], "lease_expired", state)
            row = db.execute("""SELECT * FROM jobs WHERE batch_id=? AND state='queued'
                                AND available_at<=? ORDER BY id LIMIT 1""", (batch_id, now)).fetchone()
            if not row:
                return None
            claim = uuid.uuid4().hex
            db.execute("""UPDATE jobs SET state='running',attempts=attempts+1,claim=?,lease_until=?
                          WHERE id=?""", (claim, now + LEASE_SECONDS, row["id"]))
            event(db, row["id"], "claimed", str(row["attempts"] + 1))
            return {**dict(row), "claim": claim, "attempts": row["attempts"] + 1}

    def finish(self, job, invoice=None, error=None, delay=0, elapsed=None, retryable=True):
        now = time.time()
        if invoice is not None:
            state = "review"
        else:
            state = "queued" if retryable and job["attempts"] < MAX_ATTEMPTS else "failed"
        raw = invoice.model_dump_json() if invoice is not None else None
        with self.connect() as db:
            result = db.execute("""UPDATE jobs SET state=?,raw_json=?,error=?,claim=NULL,
                                    lease_until=NULL,available_at=?,processing_ms=?
                                    WHERE id=? AND state='running' AND claim=? AND lease_until>?""",
                                (state, raw, error, now + delay, elapsed, job["id"], job["claim"], now))
            if not result.rowcount:
                return "stale_claim"
            action = {"review": "extracted", "queued": "retry_scheduled", "failed": "failed"}[state]
            event(db, job["id"], action, error or "")
            return state

    def review(self, job_id, replacement, note):
        if not note.strip():
            raise ValueError("A review note is required.")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["state"] not in ("review", "approved"):
                raise ValueError("Job has no extracted invoice to review.")
            before = Invoice.model_validate_json(row["reviewed_json"] or row["raw_json"])
            after = replacement if replacement is not None else before
            problems = issues(after)
            if problems:
                raise ValueError(" ".join(problems))
            db.execute("UPDATE jobs SET state='approved',reviewed_json=? WHERE id=?", (after.model_dump_json(), job_id))
            event(db, job_id, "reviewed", json.dumps({
                "note": note, "before": before.model_dump(mode="json"), "after": after.model_dump(mode="json"),
            }, ensure_ascii=False))

    def inspect(self, job_id):
        with self.connect() as db:
            row = db.execute("""SELECT id,batch_id,filename,state,attempts,raw_json,reviewed_json,error
                                FROM jobs WHERE id=?""", (job_id,)).fetchone()
            if not row:
                raise ValueError("Job not found.")
            events = db.execute("SELECT event,detail,at_epoch FROM events WHERE job_id=? ORDER BY id",
                                (job_id,)).fetchall()
        output = dict(row)
        for key in ("raw_json", "reviewed_json"):
            raw = output.pop(key)
            output[key.removesuffix("_json")] = json.loads(raw) if raw else None
        output["events"] = [
            {**dict(item), "detail": json.loads(item["detail"]) if item["event"] == "reviewed" else item["detail"]}
            for item in events
        ]
        return output
