# Employee Attendance & Analytics API

A Fastapi and Mongodb backend for managing employee attendance ,including punch-in, punch-out, regularization,and audit history. it also provides aggregation-based analytics for monthly employee summaraies, department performance, late-arraivals rankings and attendance trends.


## How to run 

Requires Python 3.11+ and MongoDB 6.0+.

```bash
python -m venv .venv                               # it will create a virtual environment in the current directory

.venv\Scripts\activate                             # activate the virtual environment (windows)

pip install -r requirements.txt                    # install dependencies which include in requirements.txt file

uvicorn app.main:app --port 8000                   # it will run the Fastapi sever on port 8000
```
MongoDB is configured through the `MONGO_URI` and `MONGO_DB` environment variables. If they are not set, the application uses `mongodb://localhost:27017` and `attendance_db` by default. A local `.env` file may be used for development but should not be committed.

For local development, start MongoDB with:

```bash
docker run -p 27017:27017 mongo:7                 # it will run the Mangodb server on port 27017
```

## Status

All required items mentioned in the problem statement have been completed accordingly. One proposed solution is documented in [click and check the last point](./DECISIONS.md)
