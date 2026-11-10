# Document Processing Queue

Batch processing for Vietnamese invoice images. SQLite stores the image queue,
worker attempts, original extraction, human corrections and review history.
The schema, arithmetic checks and optional Groq client come from InvoiceFlow VN.

## Install and run the sample batch on Windows

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe manage.py ingest
.\.venv\Scripts\python.exe manage.py worker 1
.\.venv\Scripts\python.exe manage.py report 1 --output reports/demo.json
```

Use the returned batch ID in place of 1 for later runs. The first fresh
database has three jobs with IDs 1–3. Demo uses prepared extracts from the
bundled synthetic invoices. Invoice A fails once and succeeds on its second
attempt. Invoice B has a deliberately wrong total of 160000. Invoice C
extracts without flags. All extracted documents wait for human approval.

## Correct and approve

Open the sample images in data/ before approving their values. If you are
using the first batch in a fresh database, use the commands below.
Otherwise use the IDs listed by the batch report.

```powershell
.\.venv\Scripts\python.exe manage.py inspect 2 --draft reports/invoice-b-review.json
notepad reports/invoice-b-review.json
```

Change total from 160000 to 165000 and save. Then:

```powershell
.\.venv\Scripts\python.exe manage.py review 1 --accept --note "Checked against image"
.\.venv\Scripts\python.exe manage.py review 2 --file reports/invoice-b-review.json --note "Corrected total from source image"
.\.venv\Scripts\python.exe manage.py review 3 --accept --note "Checked against image"
.\.venv\Scripts\python.exe manage.py inspect 2
.\.venv\Scripts\python.exe manage.py export 1
```

Inspect shows original extraction, reviewed values, and the before/after
event. Export requires every job to be approved and outputs one CSV row per
line item. Invoice totals repeat on each line and should be deduplicated by
job before aggregating. Reports score the original extraction so a human
correction cannot inflate extraction metrics.

## Optional live extraction

```powershell
$queueKey = Read-Host "Groq API key" -AsSecureString
$env:GROQ_API_KEY = [System.Net.NetworkCredential]::new('', $queueKey).Password
.\.venv\Scripts\python.exe manage.py ingest --mode live
```

Use the returned batch ID with `manage.py worker BATCH_ID` and
`manage.py report BATCH_ID --output reports/live.json`. Live mode sends
images to Groq. The default model is qwen/qwen3.8-27b. Override it with
INVOICE_VISION_MODEL if your project uses another vision/JSON-capable model.
Use synthetic or authorized inputs. A batch accepts 1–20 JPG/PNG files.
The bundled images have known field labels. Other inputs can be checked
against arithmetic rules but have no reference accuracy in the report.

Retryable live failures are queued with a delay. The worker exits when no
job is currently eligible. Wait for the delay and run it again. Jobs have
a three-attempt limit and three-minute leases. A late worker cannot write
over a reclaimed job. The lease protects stored results but does not
guarantee a provider call is made only once after a timeout or crash.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The tests use temporary databases and cover retries, expired claims, review
history and export blocking. The local CLI has no authentication or
production worker service. Use this prototype for fictional invoices.
