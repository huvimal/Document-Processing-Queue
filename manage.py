"""Ingest, extract, review and export a batch of Vietnamese invoices."""
import argparse
import csv
import hashlib
import io
import json
import os
import re
from collections import Counter
from pathlib import Path
from time import perf_counter

from invoice import Invoice, export_csv, issues
from queue_store import Queue
from vision import MODEL, extract, read_image

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"


def fixtures():
    reference = json.loads((DATA / "reference.json").read_text(encoding="utf-8"))
    return {hashlib.sha256((DATA / name).read_bytes()).hexdigest(): (name, Invoice.model_validate(row))
            for name, row in reference.items()}


def ingest(queue, folder, mode):
    paths = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg"))
    if not 1 <= len(paths) <= 20:
        raise ValueError("Use a folder with 1–20 JPG or PNG images.")
    known = fixtures() if mode == "demo" else {}
    files, seen = [], set()
    for path in paths:
        if path.stat().st_size > 5_000_000:
            raise ValueError(f"{path.name}: file exceeds 5 MB.")
        data = path.read_bytes()
        read_image(data)
        digest = hashlib.sha256(data).hexdigest()
        if mode == "demo" and digest not in known:
            raise ValueError("Demo mode recognizes only the bundled sample images.")
        if digest not in seen:
            files.append((path.name, data))
            seen.add(digest)
    return {"batch_id": queue.ingest(mode, files), "jobs": len(files), "duplicates_skipped": len(paths) - len(files)}


def worker(queue, batch_id):
    mode, _ = queue.jobs(batch_id)
    known = fixtures() if mode == "demo" else {}
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if mode == "live" and not key:
        raise ValueError("Set GROQ_API_KEY before running a live batch.")
    results = []
    while job := queue.claim(batch_id):
        start = perf_counter()
        try:
            if mode == "demo":
                name, result = known[job["digest"]]
                if name == "invoice-a.png" and job["attempts"] == 1:
                    raise RuntimeError("Simulated transient extraction failure")
                if name == "invoice-b.png":
                    result = result.model_copy(update={"total": 160000})
            else:
                result = extract(read_image(job["image"]), key, os.environ.get("INVOICE_VISION_MODEL", MODEL))
            state = queue.finish(job, invoice=result, elapsed=round((perf_counter() - start) * 1000, 3))
        except (RuntimeError, ValueError) as error:
            # Authentication/permission errors require an operator, not an immediate retry.
            http = re.match(r"Groq HTTP (\d+)", str(error))
            retryable = not http or int(http[1]) in (408, 429) or int(http[1]) >= 500
            state = queue.finish(job, error=str(error), delay=30 * job["attempts"] if mode == "live" else 0,
                                 retryable=retryable)
        results.append({"job_id": job["id"], "state": state, "attempt": job["attempts"]})
    return {"processed_attempts": results}


def fields(invoice):
    data = invoice.model_dump(mode="json")
    lines = data.pop("lines")
    data["line_count"] = len(lines)
    for index, line in enumerate(lines, 1):
        for key, value in line.items():
            data[f"line_{index}.{key}"] = value
    return data


def report(queue, batch_id):
    mode, jobs = queue.jobs(batch_id)
    known = fixtures()
    rows = []
    for job in jobs:
        item = {key: job[key] for key in ("id", "filename", "state", "attempts", "error")}
        item["last_successful_extraction_ms"] = job["processing_ms"] if mode == "live" else None
        if job["raw_json"]:
            raw = Invoice.model_validate_json(job["raw_json"])
            item["arithmetic_or_missing_flags"] = issues(raw)
            fixture = known.get(job["digest"])
            if fixture:
                expected = fields(fixture[1])
                actual = fields(raw)
                item["field_errors"] = {key: {"expected": expected.get(key), "actual": actual.get(key)}
                                        for key in sorted(expected.keys() | actual.keys())
                                        if key not in expected or key not in actual or expected[key] != actual[key]}
                item["fields_total"] = len(expected.keys() | actual.keys())
                item["fields_matched"] = item["fields_total"] - len(item["field_errors"])
        rows.append(item)
    extracted = [item for item in rows if "arithmetic_or_missing_flags" in item]
    flags = sum(bool(item["arithmetic_or_missing_flags"]) for item in extracted)
    return {"batch_id": batch_id, "mode": mode, "documents": len(jobs),
            "states": dict(Counter(item["state"] for item in rows)),
            "extracted_documents": len(extracted), "flagged_documents": flags,
            "flagged_rate": flags / len(extracted) if extracted else None,
            "retried_documents": sum(item["attempts"] > 1 for item in rows), "jobs": rows}


def export(queue, batch_id, path):
    _, rows = queue.jobs(batch_id)
    if not rows or any(row["state"] != "approved" for row in rows):
        raise ValueError("Every job in the batch must be approved before export.")
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    for index, row in enumerate(rows):
        invoice = Invoice.model_validate_json(row["reviewed_json"])
        exported = export_csv(invoice).decode("utf-8-sig")
        header, *lines = csv.reader(io.StringIO(exported))
        if index == 0:
            writer.writerow(["job_id", *header])
        writer.writerows([row["id"], *line] for line in lines)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(output.getvalue().encode("utf-8-sig"))
    return {"export": str(path), "approved_documents": len(rows)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "queue.sqlite3")
    sub = parser.add_subparsers(dest="command", required=True)
    upload = sub.add_parser("ingest")
    upload.add_argument("--folder", type=Path, default=DATA)
    upload.add_argument("--mode", choices=("demo", "live"), default="demo")
    for name in ("worker", "report", "export"):
        cmd = sub.add_parser(name)
        cmd.add_argument("batch_id", type=int)
        if name in ("report", "export"):
            cmd.add_argument("--output", type=Path)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("job_id", type=int)
    inspect.add_argument("--draft", type=Path, help="Save extracted fields for manual correction.")
    review = sub.add_parser("review")
    review.add_argument("job_id", type=int)
    review.add_argument("--note", required=True)
    choice = review.add_mutually_exclusive_group(required=True)
    choice.add_argument("--accept", action="store_true")
    choice.add_argument("--file", type=Path)
    args = parser.parse_args()
    try:
        queue = Queue(args.db)
        if args.command == "ingest":
            result = ingest(queue, args.folder, args.mode)
        elif args.command == "worker":
            result = worker(queue, args.batch_id)
        elif args.command == "report":
            result = report(queue, args.batch_id)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        elif args.command == "inspect":
            result = queue.inspect(args.job_id)
            if args.draft:
                draft = result["reviewed"] or result["raw"]
                if not draft:
                    raise ValueError("No extracted draft is available.")
                args.draft.parent.mkdir(parents=True, exist_ok=True)
                args.draft.write_text(json.dumps(draft, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        elif args.command == "review":
            invoice = Invoice.model_validate_json(args.file.read_text(encoding="utf-8-sig")) if args.file else None
            queue.review(args.job_id, invoice, args.note)
            result = queue.inspect(args.job_id)
        else:
            result = export(queue, args.batch_id, args.output or Path("reports") / f"batch-{args.batch_id}.csv")
    except (ValueError, OSError) as error:
        parser.exit(2, f"{error}\n")
    print(json.dumps(result, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
