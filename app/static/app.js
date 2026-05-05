const healthStatus = document.querySelector('#health-status');
const clusterStatus = document.querySelector('#cluster-status');
const sourceSelect = document.querySelector('#source-path');
const sourceEnvironment = document.querySelector('#source-environment');
const sourceSection = document.querySelector('#source-section');
const targetDatabaseInput = document.querySelector('#target-database');
const jobNamePrefixInput = document.querySelector('#job-name-prefix');
const validationOutput = document.querySelector('#validation-output');
const jobOutput = document.querySelector('#job-output');
const restoreForm = document.querySelector('#restore-form');
const refreshButton = document.querySelector('#refresh-button');
const validateButton = document.querySelector('#validate-button');

let latestJob = null;

function formatJson(value) {
    return JSON.stringify(value, null, 2);
}

async function fetchJson(url, options = {}) {
    const response = await fetch(url, options);
    if (!response.ok) {
        const errorBody = await response.text();
        throw new Error(errorBody || `Request failed with status ${response.status}`);
    }
    return response.json();
}

function currentPayload() {
    return {
        namespace: 'mon-metric-grafana',
        source_path: sourceSelect.value,
        database_secret_name: 'grafana-db-credentials',
        target_database: targetDatabaseInput.value.trim(),
        job_name_prefix: jobNamePrefixInput.value.trim() || 'postgres-restore',
    };
}

function selectedObjectPrefix() {
    const environment = sourceEnvironment.value;
    const section = sourceSection.value;
    if (environment === "dev") {
        return `grafana_${section}_`;
    } else if (environment === "prod") {
        return `grafana_${environment}_${section}_`;
    }
    return "";
}

async function loadHealth() {
    const health = await fetchJson('/health');
    healthStatus.textContent = health.status;
    clusterStatus.textContent = health.kubernetes_connected ? 'Connected' : 'Offline';
}

async function loadNamespaces() { }

async function loadSources() {
    const prefix = encodeURIComponent(selectedObjectPrefix());
    const sources = await fetchJson(`/api/restore-sources?prefix=${prefix}`);
    sourceSelect.innerHTML = '';
    if (!sources.length) {
        const option = document.createElement('option');
        option.value = '';
        option.textContent = 'No MinIO objects found (check bucket, endpoint, or prefix config)';
        sourceSelect.appendChild(option);
        return;
    }

    for (const source of sources) {
        const option = document.createElement('option');
        option.value = source.path;
        const sizeLabel = source.size_bytes < 1024
            ? `${source.size_bytes} B`
            : `${Math.round(source.size_bytes / 1024)} KiB`;
        option.textContent = `${source.path} (${sizeLabel})`;
        sourceSelect.appendChild(option);
    }
}

async function runValidation() {
    const payload = currentPayload();
    if (!payload.target_database) {
        validationOutput.textContent = 'Target database is required.';
        return;
    }
    validationOutput.textContent = 'Validating restore request...';
    const response = await fetchJson('/api/restore/validate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    });
    validationOutput.textContent = formatJson(response);
}

async function submitRestore(event) {
    event.preventDefault();
    jobOutput.textContent = 'Submitting restore job...';
    const payload = currentPayload();
    const response = await fetchJson('/api/restore', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    });
    latestJob = response;
    jobOutput.textContent = formatJson(response);
    window.setTimeout(loadLatestJobStatus, 2500);
}

async function loadLatestJobStatus() {
    if (!latestJob) {
        return;
    }

    const status = await fetchJson(`/api/jobs/${latestJob.namespace}/${latestJob.job_name}`);
    jobOutput.textContent = formatJson({ ...latestJob, status });
}

async function refreshAll() {
    try {
        await Promise.all([loadHealth(), loadNamespaces(), loadSources()]);
    } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        validationOutput.textContent = message;
    }
}

refreshButton.addEventListener('click', refreshAll);
validateButton.addEventListener('click', async () => {
    try {
        await runValidation();
    } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        validationOutput.textContent = message;
    }
});
sourceEnvironment.addEventListener('change', loadSources);
sourceSection.addEventListener('change', loadSources);
restoreForm.addEventListener('submit', async (event) => {
    try {
        await submitRestore(event);
    } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        jobOutput.textContent = message;
    }
});

refreshAll();
