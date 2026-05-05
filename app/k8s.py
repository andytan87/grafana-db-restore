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

    def list_restore_sources(self, prefix: str = "") -> list[RestoreSource]:
        return self.list_minio_sources(prefix=prefix)

    def list_minio_sources(self, prefix: str = "") -> list[RestoreSource]:
        if not self.settings.minio_bucket or not self.settings.minio_endpoint_url:
            LOGGER.warning("MinIO listing requested but MINIO_BUCKET or MINIO_ENDPOINT_URL is not configured")
            return []

        minio_prefix = self.settings.minio_prefix.rstrip("/")
        key_prefix = f"{minio_prefix}/{prefix.lstrip('/')}" if minio_prefix else prefix.lstrip("/")
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
            for page in paginator.paginate(Bucket=self.settings.minio_bucket, Prefix=key_prefix):
                for obj in page.get("Contents", []):
                    key: str = obj["Key"]
                    suffix = Path(key).suffix
                    if suffix not in allowed_extensions:
                        continue
                    relative_key = key[len(minio_prefix) + 1 :] if minio_prefix and key.startswith(minio_prefix + "/") else key
                    results.append(
                        RestoreSource(
                            path=relative_key,
                            size_bytes=obj.get("Size", 0),
                            modified_at=obj["LastModified"],
                            extension=suffix,
                        )
                    )
        except (BotoCoreError, ClientError) as exc:
            LOGGER.error("Failed to list MinIO objects: %s", exc)

        results.sort(key=lambda item: item.modified_at, reverse=True)
        return results[: self.settings.max_backup_listing]

    # ------------------------------------------------------------------ validation

    def validate_restore_request(self, restore_request: RestoreJobRequest) -> RestoreValidationResponse:
        errors: list[str] = []
        warnings: list[str] = []
        return self._validate_minio_request(restore_request, errors, warnings)

    def _validate_minio_request(
        self, restore_request: RestoreJobRequest, errors: list[str], warnings: list[str]
    ) -> RestoreValidationResponse:
        if not self.settings.minio_bucket:
            errors.append("MINIO_BUCKET must be configured before submitting a MinIO restore job")
        if not self.settings.minio_endpoint_url:
            errors.append("MINIO_ENDPOINT_URL must be configured before submitting a MinIO restore job")

        suffix = Path(restore_request.source_path).suffix
        if suffix not in self.settings.allowed_backup_extensions:
            errors.append(
                f"Unsupported backup extension '{suffix}'. Allowed extensions: {', '.join(self.settings.allowed_backup_extensions)}"
            )
        if not self.settings.minio_credentials_secret_name:
            errors.append("MINIO_CREDENTIALS_SECRET_NAME must be set so the restore job can authenticate with MinIO")
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

        return self._submit_minio_restore_job(restore_request)

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

    def _submit_minio_restore_job(self, restore_request: RestoreJobRequest) -> RestoreJobResponse:
        job_name = self._job_name(restore_request.job_name_prefix)
        creds_secret = self.settings.minio_credentials_secret_name
        minio_prefix = self.settings.minio_prefix.rstrip("/")
        full_key = f"{minio_prefix}/{restore_request.source_path.lstrip('/')}" if minio_prefix else restore_request.source_path.lstrip("/")
        local_filename = Path(restore_request.source_path).name

        download_cmd = (
            'mc alias set restore "$MINIO_ENDPOINT_URL" "$MINIO_ACCESS_KEY" "$MINIO_SECRET_KEY" && '
            f'mc cp {shlex.quote(f"restore/{self.settings.minio_bucket}/{full_key}")} {shlex.quote(f"/restore/{local_filename}")}'
        )

        minio_env_vars = [
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
        quoted_local = shlex.quote(f"/restore/{local_filename}")

        job = client.V1Job(
            metadata=client.V1ObjectMeta(
                name=job_name,
                labels={"app": "grafanadb-restore", "operation": "postgres-restore-minio"},
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
                        init_containers=[
                            client.V1Container(
                                name="minio-download",
                                image=self.settings.minio_mc_image,
                                command=["/bin/sh", "-c", download_cmd],
                                env=minio_env_vars,
                                volume_mounts=[client.V1VolumeMount(name="restore-scratch", mount_path="/restore")],
                            )
                        ],
                        containers=[
                            client.V1Container(
                                name="restore",
                                image=self.settings.restore_image,
                                command=["/bin/sh", "-c", self._restore_shell_command(quoted_local)],
                                env=self._pg_env_vars(restore_request.database_secret_name, restore_request.target_database),
                                volume_mounts=[client.V1VolumeMount(name="restore-scratch", mount_path="/restore")],
                            )
                        ],
                        volumes=[client.V1Volume(name="restore-scratch", empty_dir=client.V1EmptyDirVolumeSource())],
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
