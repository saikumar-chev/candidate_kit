# Candidate kit

Read in this order:

1. `PROBLEM_STATEMENT.docx`: what you are asked to do, how it is evaluated.
2. `openapi.yaml`: the API contract and the business rules (R1 - R10).
3. `DATA_MODEL.md`: what is stored in MongoDB, with nine sample documents.
4. `app/main.py`: the starter. Put **all** your code in this one file. It must start with `uvicorn app.main:app --port 8000`.
5. `REVIEW.md`, `DECISIONS.md`: templates to fill in.

`sample_data/` holds the nine sample documents as MongoDB Extended JSON; `sample_seed.py` loads them. The grader's dataset is
much larger and contains cases they do not show.

## Local setup
### Windows PowerShell
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python sample_seed.py
python -m uvicorn app.main:app --port 8000 --reload
```

The default connection is `mongodb://localhost:27017` and the default database is `attendance_db`.
For another MongoDB instance, set `MONGO_URI` and `MONGO_DB` before starting the app:

```powershell
$env:MONGO_URI = "mongodb+srv://<user>:<password>@<cluster>/attendance_db?retryWrites=true&w=majority"
$env:MONGO_DB = "attendance_db"
```

### macOS/Linux
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python sample_seed.py
python -m uvicorn app.main:app --port 8000 --reload
```

MongoDB **6.0 or newer** is required (the grader uses 7.0). MongoDB Atlas free tier (M0) works.

## Submit
A public Git repository with `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md` and a short `README.md`.
No `.env`, no secrets, no Dockerfile.
