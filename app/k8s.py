from __future__ import annotations

import logging
import shlex
from datetime import UTC, datetime
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from kubernetes import client, config
from kubernetes.config.config_exception import ConfigException

from app.config import Settings
from app.models import JobStatusResponse, RestoreJobRequest, RestoreJobResponse, RestoreSource, RestoreValidationResponse

LOGGER = logging.getLogger(__name__)

RESTORE_JOB_POD_ANNOTATIONS = {
    # Avoid timezone sidecar injection for short-lived restore jobs.
    "k8tz.io/inject": "false",
}


class RestoreService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._core_api: client.CoreV1Api | None = None
        self._batch_api: client.BatchV1Api | None = None
        self._kubernetes_connected = False
        self._configure_client()

    @property
    def kubernetes_connected(self) -> bool:
        return self._kubernetes_connected

    def _configure_client(self) -> None:
        try:
            config.load_incluster_config()
            self._kubernetes_connected = True
        except ConfigException:
            try:
                config.load_kube_config()
                self._kubernetes_connected = True
            except ConfigException:
                LOGGER.warning("Kubernetes configuration is unavailable; API will run in offline mode")
                self._kubernetes_connected = False
                return

        self._core_api = client.CoreV1Api()
        self._batch_api = client.BatchV1Api()

    def list_namespaces(self) -> list[str]:
        if not self._core_api:
            return []
        namespaces = self._core_api.list_namespace().items
        return sorted(namespace.metadata.name for namespace in namespaces if namespace.metadata and namespace.metadata.name)

    # ------------------------------------------------------------------ source listing

    def _resolve_bucket_name(self, environment: str | None = None, namespace: str | None = None) -> str:
        return self.settings.minio_bucket.strip()

    def _resolve_bucket_prefix(self, environment: str | None = None, namespace: str | None = None) -> str:
        environment_value = (environment or "").strip().lower()
        if environment_value == "dev":
            return self.settings.minio_prefix_dev.strip("/")
        if environment_value == "prod":
            return self.settings.minio_prefix_prod.strip("/")

        return ""

    def list_restore_sources(
        self, prefix: str = "", environment: str | None = None, namespace: str | None = None
    ) -> list[RestoreSource]:
        return self.list_s3_sources(prefix=prefix, environment=environment, namespace=namespace)

    def list_s3_sources(
        self, prefix: str = "", environment: str | None = None, namespace: str | None = None
    ) -> list[RestoreSource]:
        bucket_name = self._resolve_bucket_name(environment=environment, namespace=namespace)
        if not bucket_name or not self.settings.minio_endpoint_url:
            LOGGER.warning("S3 listing requested but MINIO_BUCKET or MINIO_ENDPOINT_URL is not configured")
            return []

        bucket_prefix = self._resolve_bucket_prefix(environment=environment, namespace=namespace)
        key_prefix = f"{bucket_prefix}/{prefix.lstrip('/')}" if bucket_prefix else prefix.lstrip("/")
        allowed_extensions = set(self.settings.allowed_backup_extensions)

        boto_kwargs: dict = {
            "region_name": self.settings.minio_region,
            "endpoint_url": self.settings.minio_endpoint_url,
        }
        if self.settings.minio_access_key:
            boto_kwargs["aws_access_key_id"] = self.settings.minio_access_key
        if self.settings.minio_secret_key:
            boto_kwargs["aws_secret_access_key"] = self.settings.minio_secret_key

        results: list[RestoreSource] = []
        try:
            s3 = boto3.client("s3", **boto_kwargs)
            paginator = s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket_name, Prefix=key_prefix):
                for obj in page.get("Contents", []):
                    key: str = obj["Key"]
                    suffix = Path(key).suffix
                    if suffix not in allowed_extensions:
                        continue
                    relative_key = key[len(bucket_prefix) + 1 :] if bucket_prefix and key.startswith(bucket_prefix + "/") else key
                    results.append(
                        RestoreSource(
                            path=relative_key,
                            size_bytes=obj.get("Size", 0),
                            modified_at=obj["LastModified"],
                            extension=suffix,
                        )
                    )
        except (BotoCoreError, ClientError) as exc:
            LOGGER.error("Failed to list S3 objects: %s", exc)

        results.sort(key=lambda item: item.modified_at, reverse=True)
        return results[: self.settings.max_backup_listing]

    # ------------------------------------------------------------------ validation

    def validate_restore_request(self, restore_request: RestoreJobRequest) -> RestoreValidationResponse:
        errors: list[str] = []
        warnings: list[str] = []
        return self._validate_s3_request(restore_request, errors, warnings)

    def _validate_s3_request(
        self, restore_request: RestoreJobRequest, errors: list[str], warnings: list[str]
    ) -> RestoreValidationResponse:
        if not self._resolve_bucket_name(environment=restore_request.environment, namespace=restore_request.namespace):
            errors.append("Configure MINIO_BUCKET before submitting a MinIO restore job")
        if not self.settings.minio_endpoint_url:
            errors.append("MINIO_ENDPOINT_URL must be configured before submitting a MinIO restore job")

        suffix = Path(restore_request.source_path).suffix
        if suffix not in self.settings.allowed_backup_extensions:
            errors.append(
                f"Unsupported backup extension '{suffix}'. Allowed extensions: {', '.join(self.settings.allowed_backup_extensions)}"
            )
        if not self.settings.minio_credentials_secret_name:
            errors.append("MINIO_CREDENTIALS_SECRET_NAME must be set so the restore job can authenticate with S3")
        if not self.kubernetes_connected:
            warnings.append("Kubernetes connectivity is unavailable; restore job submission will fail until cluster access is configured")

        return RestoreValidationResponse(valid=not errors, errors=errors, warnings=warnings, resolved_source_path=restore_request.source_path)

    # ------------------------------------------------------------------ job submission

    def submit_restore_job(self, restore_request: RestoreJobRequest) -> RestoreJobResponse:
        if not self._batch_api:
            raise RuntimeError("Kubernetes connectivity is unavailable")

        validation = self.validate_restore_request(restore_request)
        if not validation.valid:
            raise ValueError("; ".join(validation.errors))

        return self._submit_s3_restore_job(restore_request)

    # ------------------------------------------------------------------ helpers

    def _pg_env_vars(self, secret_name: str, target_database: str) -> list[client.V1EnvVar]:
        return [
            client.V1EnvVar(
                name="POSTGRES_HOST",
                value_from=client.V1EnvVarSource(secret_key_ref=client.V1SecretKeySelector(name=secret_name, key="host")),
            ),
            client.V1EnvVar(
                name="POSTGRES_PORT",
                value_from=client.V1EnvVarSource(secret_key_ref=client.V1SecretKeySelector(name=secret_name, key="port")),
            ),
            client.V1EnvVar(name="POSTGRES_DB", value=target_database),
            client.V1EnvVar(
                name="POSTGRES_USER",
                value_from=client.V1EnvVarSource(secret_key_ref=client.V1SecretKeySelector(name=secret_name, key="username")),
            ),
            client.V1EnvVar(
                name="POSTGRES_PASSWORD",
                value_from=client.V1EnvVarSource(secret_key_ref=client.V1SecretKeySelector(name=secret_name, key="password")),
            ),
        ]

    @staticmethod
    def _restore_shell_command(backup_file_expr: str) -> str:
        return f"""set -euo pipefail
backup_file={backup_file_expr}
export PGPASSWORD="$POSTGRES_PASSWORD"
case "$backup_file" in
  *.sql)
    psql -v ON_ERROR_STOP=1 -e -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f "$backup_file"
    ;;
  *.dump|*.backup|*.tar)
    pg_restore --verbose --clean --if-exists --no-owner --no-privileges -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DB" "$backup_file"
    ;;
  *)
    echo "Unsupported backup extension: $backup_file"
    exit 1
    ;;
esac""".strip()

    def _job_name(self, prefix: str) -> str:
        timestamp = datetime.now(tz=UTC).strftime("%Y%m%d%H%M%S")
        return f"{prefix}-{timestamp}"[:63]

    def _submit_s3_restore_job(self, restore_request: RestoreJobRequest) -> RestoreJobResponse:
        bucket_name = self._resolve_bucket_name(environment=restore_request.environment, namespace=restore_request.namespace)
        job_name = self._job_name(restore_request.job_name_prefix)
        creds_secret = self.settings.minio_credentials_secret_name
        bucket_prefix = self._resolve_bucket_prefix(environment=restore_request.environment, namespace=restore_request.namespace)
        full_key = f"{bucket_prefix}/{restore_request.source_path.lstrip('/')}" if bucket_prefix else restore_request.source_path.lstrip("/")
        local_filename = Path(restore_request.source_path).name
        local_backup_path = f"/work/{local_filename}"
        quoted_local = shlex.quote(local_backup_path)

        s3_env_vars = [
            client.V1EnvVar(
                name="MINIO_ACCESS_KEY",
                value_from=client.V1EnvVarSource(secret_key_ref=client.V1SecretKeySelector(name=creds_secret, key="MINIO_ACCESS_KEY")),
            ),
            client.V1EnvVar(
                name="MINIO_SECRET_KEY",
                value_from=client.V1EnvVarSource(secret_key_ref=client.V1SecretKeySelector(name=creds_secret, key="MINIO_SECRET_KEY")),
            ),
            client.V1EnvVar(name="MINIO_ENDPOINT_URL", value=self.settings.minio_endpoint_url),
            client.V1EnvVar(
                name="MINIO_REGION",
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(name=creds_secret, key="MINIO_REGION", optional=True)
                ),
            ),
        ]

        download_cmd = (
            'mkdir -p "$MC_CONFIG_DIR" && '
            f'mc alias set src "$MINIO_ENDPOINT_URL" "$MINIO_ACCESS_KEY" "$MINIO_SECRET_KEY" && '
            f'mc cp {shlex.quote(f"src/{bucket_name}/{full_key}")} {quoted_local}'
        )

        download_env_vars = s3_env_vars + [
            client.V1EnvVar(name="MC_CONFIG_DIR", value="/work/.mc"),
            client.V1EnvVar(name="HOME", value="/work"),
        ]

        restore_cmd = self._restore_shell_command(quoted_local)

        job = client.V1Job(
            metadata=client.V1ObjectMeta(
                name=job_name,
                labels={"app": "grafanadb-restore", "operation": "postgres-restore-s3"},
            ),
            spec=client.V1JobSpec(
                backoff_limit=0,
                ttl_seconds_after_finished=3600,
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(
                        labels={"app": "grafanadb-restore", "job-name": job_name},
                        annotations=RESTORE_JOB_POD_ANNOTATIONS,
                    ),
                    spec=client.V1PodSpec(
                        restart_policy="Never",
                        volumes=[
                            client.V1Volume(
                                name="restore-workdir",
                                empty_dir=client.V1EmptyDirVolumeSource(),
                            )
                        ],
                        init_containers=[
                            client.V1Container(
                                name="download-backup",
                                image=self.settings.minio_mc_image,
                                command=["/bin/sh", "-c", download_cmd],
                                env=download_env_vars,
                                volume_mounts=[client.V1VolumeMount(name="restore-workdir", mount_path="/work")],
                            )
                        ],
                        containers=[
                            client.V1Container(
                                name="restore",
                                image=self.settings.restore_image,
                                command=["/bin/sh", "-c", restore_cmd],
                                env=self._pg_env_vars(restore_request.database_secret_name, restore_request.target_database),
                                volume_mounts=[client.V1VolumeMount(name="restore-workdir", mount_path="/work")],
                            )
                        ],
                    ),
                ),
            ),
        )

        self._batch_api.create_namespaced_job(namespace=restore_request.namespace, body=job)
        return RestoreJobResponse(
            status="submitted",
            job_name=job_name,
            namespace=restore_request.namespace,
            source_path=restore_request.source_path,
            created_at=datetime.now(tz=UTC),
        )

    # ------------------------------------------------------------------ job status

    def get_job_status(self, namespace: str, job_name: str) -> JobStatusResponse:
        if not self._batch_api:
            raise RuntimeError("Kubernetes connectivity is unavailable")

        job = self._batch_api.read_namespaced_job(name=job_name, namespace=namespace)
        conditions = []
        if job.status and job.status.conditions:
            conditions = [condition.type for condition in job.status.conditions if condition.type]

        status = job.status
        return JobStatusResponse(
            job_name=job_name,
            namespace=namespace,
            active=(status.active or 0) if status else 0,
            succeeded=(status.succeeded or 0) if status else 0,
            failed=(status.failed or 0) if status else 0,
            conditions=conditions,
        )
