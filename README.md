# AI ASR File MVP

A Railway-ready MVP for consulting interview audio transcription and analysis.

## Stack

- Frontend: React + TypeScript + Vite + Ant Design
- Backend: FastAPI + SQLAlchemy
- Queue: Redis + RQ
- Local dev: SQLite + local mock storage + mock ASR/LLM
- Railway: PostgreSQL + Redis + Railway Storage Buckets + Aliyun ASR/LLM

## Local Quick Start

```bash
cd app/backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --reload
```

In another terminal:

```bash
cd app/frontend
npm install
npm run dev
```

Default login:

```text
admin / mp2026
```

## Railway Services

Deploy two services from the same repo:

- `web`: runs FastAPI and serves the built frontend.
- `worker`: runs RQ worker.

See `backend/.env.example` for environment variables.

Product and API notes are tracked in `prd.md` and `api-spec.md`.


## Railway Deploy Checklist

The repo now includes a root `Dockerfile` that builds the Vite frontend and copies `frontend/dist` into the FastAPI container, so the web service can serve the SPA directly.

Create two Railway services from the same repo/root:

- Web service: use `railway.web.toml`, command `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
- Worker service: use `railway.worker.toml`, command `python -m app.worker`.

Required production variables:

```text
APP_ENV=production
APP_BASE_URL=https://<your-web-service-domain>
SESSION_SECRET=<random-secret>
ADMIN_USERNAME=admin
ADMIN_PASSWORD=mp2026
DATABASE_URL=<Railway PostgreSQL connection string>
REDIS_URL=<Railway Redis connection string>
QUEUE_SYNC=false
STORAGE_PROVIDER=railway_bucket
STORAGE_MOCK_ENABLED=false
RAILWAY_BUCKET_ENDPOINT=<Railway bucket S3 endpoint>
RAILWAY_BUCKET_NAME=<bucket name>
RAILWAY_BUCKET_ACCESS_KEY_ID=<access key>
RAILWAY_BUCKET_SECRET_ACCESS_KEY=<secret key>
RAILWAY_BUCKET_REGION=auto
ASR_MOCK_ENABLED=false
LLM_MOCK_ENABLED=false
ASR_API_KEY=<Aliyun key>
ASR_MODEL=fun-asr
ASR_POLL_INTERVAL_SECONDS=10
ASR_POLL_TIMEOUT_SECONDS=14400
LLM_CLEAN_API_KEY=<Aliyun key>
LLM_SUMMARY_API_KEY=<Aliyun key>
LLM_QA_API_KEY=<Aliyun key>
LLM_TIMEOUT_SECONDS=300
```

After deployment, open System Settings in the app and run the AI connection tests and storage connection test before uploading real recordings.

Notes:

- Web and Worker must share the same PostgreSQL and Redis variables.
- The ASR model is configurable. If your Aliyun account does not accept `fun-asr`, set the ASR model in System Settings to the enabled model name such as `paraformer-v2`.
- When System Settings are saved, AI and Bucket credentials are stored in PostgreSQL so both Railway services can read the same runtime configuration.
