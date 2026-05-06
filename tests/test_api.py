from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from app.config import Settings
from app.k8s import RESTORE_JOB_POD_ANNOTATIONS, RestoreService
from app.main import app, get_restore_service
from app.models import JobStatusResponse, RestoreJobRequest, RestoreJobResponse, RestoreSource, RestoreValidationResponse


class FakeRestoreService:
    kubernetes_connected = True

    def list_namespaces(self) -> list[str]:
        return ["database", "grafana"]

    def list_restore_sources(
        self, prefix: str = "", environment: str | None = None, namespace: str | None = None
    ) -> list[RestoreSource]:
        base = [
            RestoreSource(
                path="daily/grafana-2026-04-07.dump",
                size_bytes=2048,
                modified_at=datetime(2026, 4, 7, 12, 0, tzinfo=UTC),
                extension=".dump",
            )
        ]
        return [source for source in base if not prefix or source.path.startswith(prefix)]

    def validate_restore_request(self, restore_request):
        return RestoreValidationResponse(valid=True, errors=[], warnings=[], resolved_source_path=restore_request.source_path)

    def submit_restore_job(self, restore_request):
        return RestoreJobResponse(
            status="submitted",
            job_name="postgres-restore-20260407120000",
            namespace=restore_request.namespace,
            source_path=restore_request.source_path,
            created_at=datetime(2026, 4, 7, 12, 0, tzinfo=UTC),
        )

    def get_job_status(self, namespace: str, job_name: str):
        return JobStatusResponse(job_name=job_name, namespace=namespace, active=0, succeeded=1, failed=0, conditions=["Complete"])


def setup_module() -> None:
    app.dependency_overrides[get_restore_service] = lambda: FakeRestoreService()


def teardown_module() -> None:
    app.dependency_overrides.clear()


client = TestClient(app)


# ------------------------------------------------------------------ API endpoint tests

def test_health_endpoint() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["kubernetes_connected"] is True


def test_namespaces_endpoint() -> None:
    response = client.get("/api/namespaces")
    assert response.status_code == 200
    assert response.json() == ["database", "grafana"]


def test_restore_sources_endpoint() -> None:
    response = client.get("/api/restore-sources")
    assert response.status_code == 200
    assert response.json()[0]["path"] == "daily/grafana-2026-04-07.dump"


def test_restore_submit_minio_endpoint() -> None:
    response = client.post(
        "/api/restore",
        json={
            "namespace": "database",
            "environment": "prod",
            "source_path": "2026/04/grafana-2026-04-07.dump",
            "database_secret_name": "grafana-db-credentials",
            "target_database": "grafana",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "submitted"


# ------------------------------------------------------------------ MinIO listing unit tests

@mock_aws
def test_list_s3_sources() -> None:
    bucket = "test-pg-backups"
    original_boto3_client = boto3.client
    original_boto3_client("s3", region_name="us-east-1").create_bucket(Bucket=bucket)
    s3 = original_boto3_client("s3", region_name="us-east-1")
    s3.put_object(Bucket=bucket, Key="grafana/2026-04-07.dump", Body=b"pg_dump data")
    s3.put_object(Bucket=bucket, Key="grafana/2026-04-06.sql", Body=b"sql data")
    s3.put_object(Bucket=bucket, Key="grafana/readme.txt", Body=b"text")  # filtered out

    settings = Settings(minio_bucket=bucket, minio_region="us-east-1", minio_endpoint_url="http://localhost:9000")
    service = RestoreService.__new__(RestoreService)
    service.settings = settings
    service._kubernetes_connected = False
    service._core_api = None
    service._batch_api = None

    with patch("app.k8s.boto3.client", side_effect=lambda service_name, **kwargs: original_boto3_client(service_name, region_name="us-east-1")):
        sources = service.list_s3_sources()
    paths = [s.path for s in sources]
    assert "grafana/2026-04-07.dump" in paths
    assert "grafana/2026-04-06.sql" in paths
    assert not any(p.endswith(".txt") for p in paths)


@mock_aws
def test_list_s3_sources_with_prefix_filter() -> None:
    bucket = "test-pg-backups-prefix"
    original_boto3_client = boto3.client
    original_boto3_client("s3", region_name="us-east-1").create_bucket(Bucket=bucket)
    s3 = original_boto3_client("s3", region_name="us-east-1")
    s3.put_object(Bucket=bucket, Key="tenant-a/2026-04-07.dump", Body=b"a")
    s3.put_object(Bucket=bucket, Key="tenant-b/2026-04-07.dump", Body=b"b")

    settings = Settings(minio_bucket=bucket, minio_region="us-east-1", minio_endpoint_url="http://localhost:9000")
    service = RestoreService.__new__(RestoreService)
    service.settings = settings
    service._kubernetes_connected = False
    service._core_api = None
    service._batch_api = None

    with patch("app.k8s.boto3.client", side_effect=lambda service_name, **kwargs: original_boto3_client(service_name, region_name="us-east-1")):
        sources = service.list_s3_sources(prefix="tenant-a/")
    assert len(sources) == 1
    assert sources[0].path == "tenant-a/2026-04-07.dump"


def test_submitted_job_disables_k8tz_injection() -> None:
    service = RestoreService.__new__(RestoreService)
    service.settings = Settings(
        minio_bucket="test-bucket",
        minio_endpoint_url="http://minio:9000",
        minio_credentials_secret_name="minio-credentials",
    )
    service._kubernetes_connected = True
    service._core_api = None
    service._batch_api = MagicMock()

    request = RestoreJobRequest(
        namespace="database",
        environment="prod",
        source_path="grafana_prod_default_2025-07-26-18:21:43.backup",
        database_secret_name="grafana-db-credentials",
        target_database="grafana",
    )

    service.submit_restore_job(request)

    create_call = service._batch_api.create_namespaced_job.call_args
    assert create_call is not None
    submitted_job = create_call.kwargs["body"]
    assert submitted_job.metadata.annotations is None
    annotations = submitted_job.spec.template.metadata.annotations
    assert annotations == RESTORE_JOB_POD_ANNOTATIONS


def test_resolve_bucket_name_prefers_shared_bucket() -> None:
    service = RestoreService.__new__(RestoreService)
    service.settings = Settings(
        minio_bucket="shared-bucket",
        minio_endpoint_url="http://minio:9000",
        minio_credentials_secret_name="minio-credentials",
    )
    service._kubernetes_connected = False
    service._core_api = None
    service._batch_api = None

    assert service._resolve_bucket_name(namespace="team-dev") == "shared-bucket"
    assert service._resolve_bucket_name(namespace="team-prod") == "shared-bucket"
    assert service._resolve_bucket_name(namespace="shared") == "shared-bucket"


def test_resolve_bucket_name_requires_shared_bucket() -> None:
    service = RestoreService.__new__(RestoreService)
    service.settings = Settings(
        minio_bucket="",
        minio_endpoint_url="http://minio:9000",
        minio_credentials_secret_name="minio-credentials",
    )
    service._kubernetes_connected = False
    service._core_api = None
    service._batch_api = None

    assert service._resolve_bucket_name(namespace="mon-metric-grafana") == ""


def test_resolve_bucket_name_prefers_explicit_environment() -> None:
    service = RestoreService.__new__(RestoreService)
    service.settings = Settings(
        minio_bucket="shared-bucket",
        minio_prefix_dev="dev-folder",
        minio_prefix_prod="prod-folder",
        minio_endpoint_url="http://minio:9000",
        minio_credentials_secret_name="minio-credentials",
    )
    service._kubernetes_connected = False
    service._core_api = None
    service._batch_api = None

    assert service._resolve_bucket_name(environment="dev", namespace="mon-metric-grafana") == "shared-bucket"
    assert service._resolve_bucket_name(environment="prod", namespace="mon-metric-grafana") == "shared-bucket"
    assert service._resolve_bucket_prefix(environment="dev", namespace="mon-metric-grafana") == "dev-folder"
    assert service._resolve_bucket_prefix(environment="prod", namespace="mon-metric-grafana") == "prod-folder"


def test_submit_restore_job_uses_environment_bucket() -> None:
    service = RestoreService.__new__(RestoreService)
    service.settings = Settings(
        minio_bucket="elkintranet-nonprod-monitoring",
        minio_prefix_dev="grafana-backup-dev",
        minio_prefix_prod="grafana-backup-prod",
        minio_endpoint_url="http://minio:9000",
        minio_credentials_secret_name="minio-credentials",
        minio_mc_image="minio/mc:latest",
    )
    service._kubernetes_connected = True
    service._core_api = None
    service._batch_api = MagicMock()

    request = RestoreJobRequest(
        namespace="mon-metric-grafana",
        environment="prod",
        source_path="grafana_prod_default_2025-07-26-18:21:43.backup",
        database_secret_name="grafana-db-credentials",
        target_database="grafana",
    )

    service.submit_restore_job(request)

    create_call = service._batch_api.create_namespaced_job.call_args
    assert create_call is not None
    submitted_job = create_call.kwargs["body"]
    init_container = submitted_job.spec.template.spec.init_containers[0]
    command = " ".join(init_container.command)
    assert "src/elkintranet-nonprod-monitoring/grafana-backup-prod/grafana_prod_default_2025-07-26-18:21:43.backup" in command
