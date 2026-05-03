# GrafanaDB Restore

A FastAPI-based Kubernetes deployment with an operator-facing web UI for restoring PostgreSQL backups from a MinIO bucket.

## Features

- Browser UI for selecting namespaces, backup sources, and database secrets
- REST API for validation, submission, and job-status polling
- Kubernetes-native restore execution via batch jobs
- Read-only backup browsing through a MinIO bucket
- Deployment manifests, RBAC, and example secret definitions

## Architecture

```
├── app/
│   ├── config.py
│   ├── k8s.py
│   ├── main.py
│   ├── models.py
│   └── static/
│       ├── app.js
│       ├── index.html
│       └── styles.css
├── k8s/
│   ├── configmap.yaml
│   ├── deployment.yaml
│   ├── example-db-secret.yaml
│   ├── example-minio-secret.yaml
│   ├── namespace.yaml
│   ├── rbac.yaml
│   └── service.yaml
├── tests/
│   └── test_api.py
├── .env.example
├── Dockerfile
├── README.md
└── requirements.txt
```

## Restore Workflow

1. The frontend lists available `.sql`, `.dump`, `.backup`, and `.tar` artifacts from the configured MinIO bucket.
2. The operator selects a namespace, backup file, and database secret.
3. The API validates the request and submits a Kubernetes `Job`.
4. An init container downloads the object with `mc` before the restore container runs `psql` or `pg_restore`.

## Prerequisites

- Kubernetes cluster with RBAC permissions to create jobs in the target namespace
- A MinIO bucket containing PostgreSQL backup artifacts reachable from the cluster
- A secret in the target namespace with these keys:
  - `host`
  - `port`
  - `database`
  - `username`
  - `password`
- Python 3.13+ for local development

The API deployment needs a `minio-credentials` secret with `MINIO_ACCESS_KEY` and `MINIO_SECRET_KEY`, and the config map must point `MINIO_ENDPOINT_URL` at the MinIO service.

## Local Development

```bash
/Users/andy/.pyenv/versions/3.13.11/bin/python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000` after starting the API.

If Kubernetes connectivity is not available locally, the API still serves the UI and restore-source listing, but restore submission will fail until kubeconfig or in-cluster auth is configured.

## API Endpoints

- `GET /health`
- `GET /api/namespaces`
- `GET /api/restore-sources?prefix=...`
- `POST /api/restore/validate`
- `POST /api/restore`
- `GET /api/jobs/{namespace}/{job_name}`

## Deployment

```bash
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/rbac.yaml
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/example-minio-secret.yaml
kubectl apply -f k8s/service.yaml
kubectl apply -f k8s/deployment.yaml
```

## Testing

```bash
pytest
```
