# Employee Attendance & Analytics API

FastAPI + MongoDB service for the HROne attendance assignment. All application code is in `app/main.py`; the contract is
`openapi.yaml`, the stored data is described in `DATA_MODEL.md`.

## Run

Requires Python 3.11+ and MongoDB 6.0+.

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000                       # from the repository root
```

Configuration comes from `MONGO_URI` and `MONGO_DB` (defaults: `mongodb://localhost:27017` and `attendance_db`). A local
`.env` with those two keys is optional; real environment variables take precedence over it. For a local database:
`docker run -p 27017:27017 mongo:7`.

`python sample_seed.py` loads the nine sample documents. It **replaces** the contents of both collections.

## Worth knowing

- All indexes are created idempotently at startup; there are no manual steps. If MongoDB is unreachable at startup the app
  still starts, `GET /health` returns 503, and the indexes are created on the first health check that reaches MongoDB.
- Every endpoint in `openapi.yaml` is implemented. The four analytics endpoints are MongoDB aggregation pipelines that
  read the stored `work_hours`, `late_minutes`, `overtime_minutes` and `half_day`; they never recompute them.
- Punch-in races are decided by the unique `(emp_code, date)` index. Punch-out and regularization use conditional
  writes. A regularization that loses a race re-reads and retries, so no history entry is lost; after 10 consecutive
  lost races either write returns 409.
- Instants are read as UTC-aware datetimes (`tz_aware=True`) and truncated to whole seconds before any calculation.
- `REVIEW.md` lists the starter defects with reproductions and fixes, plus the code judged correct. `DECISIONS.md`
  answers the five Part C questions.
