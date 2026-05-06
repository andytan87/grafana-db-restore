from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.k8s import RestoreService
from app.models import HealthResponse, JobStatusResponse, RestoreJobRequest, RestoreJobResponse, RestoreSource, RestoreValidationResponse


@lru_cache
def get_restore_service() -> RestoreService:
    return RestoreService(get_settings())


def create_app() -> FastAPI:
    settings = get_settings()
    static_dir = Path(__file__).parent / "static"

    application = FastAPI(title=settings.app_name, version="0.1.0")
    application.mount("/assets", StaticFiles(directory=static_dir), name="assets")

    @application.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @application.get("/health", response_model=HealthResponse)
    async def health(service: RestoreService = Depends(get_restore_service)) -> HealthResponse:
        connected = service.kubernetes_connected
        message = "API is running and connected to Kubernetes" if connected else "API is running but Kubernetes connectivity is unavailable"
        return HealthResponse(
            status="healthy",
            kubernetes_connected=connected,
            message=message,
            timestamp=datetime.now(tz=UTC),
        )

    @application.get("/api/namespaces", response_model=list[str])
    async def list_namespaces(service: RestoreService = Depends(get_restore_service)) -> list[str]:
        return service.list_namespaces()

    @application.get("/api/restore-sources", response_model=list[RestoreSource])
    async def list_restore_sources(
        prefix: str = Query(default="", description="Optional object key prefix under the configured MinIO bucket"),
        environment: str | None = Query(default=None, description="Optional environment override (`dev` or `prod`) for bucket selection"),
        namespace: str | None = Query(default=None, description="Optional namespace used to resolve the MinIO bucket (dev/prod)"),
        service: RestoreService = Depends(get_restore_service),
    ) -> list[RestoreSource]:
        return service.list_restore_sources(prefix=prefix, environment=environment, namespace=namespace)

    @application.post("/api/restore/validate", response_model=RestoreValidationResponse)
    async def validate_restore(
        restore_request: RestoreJobRequest,
        service: RestoreService = Depends(get_restore_service),
    ) -> RestoreValidationResponse:
        return service.validate_restore_request(restore_request)

    @application.post("/api/restore", response_model=RestoreJobResponse)
    async def submit_restore(
        restore_request: RestoreJobRequest,
        service: RestoreService = Depends(get_restore_service),
    ) -> RestoreJobResponse:
        try:
            return service.submit_restore_job(restore_request)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @application.get("/api/jobs/{namespace}/{job_name}", response_model=JobStatusResponse)
    async def get_job_status(
        namespace: str,
        job_name: str,
        service: RestoreService = Depends(get_restore_service),
    ) -> JobStatusResponse:
        try:
            return service.get_job_status(namespace=namespace, job_name=job_name)
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    return application


app = create_app()
