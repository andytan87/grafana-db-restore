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
const BACKUP_NAME_PATTERNS = [
    /^grafana_(.+)-(\d{4}\.\d{2}\.\d{2})-.+\.backup$/,
    /^grafana_(.+)_(\d{4}-\d{2}-\d{2})[-_].+\.backup$/,
];

const ENVIRONMENT_NAMESPACES = {
    dev: 'mon-metric-grafana',
    prod: 'mon-metric-grafana',
};

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
    const environment = sourceEnvironment.value;
    const namespace = ENVIRONMENT_NAMESPACES[environment] || ENVIRONMENT_NAMESPACES.prod;

    return {
        namespace,
        environment,
        source_path: sourceSelect.value,
        database_secret_name: 'grafana-db-credentials',
        target_database: targetDatabaseInput.value.trim(),
        job_name_prefix: jobNamePrefixInput.value.trim() || 'postgres-restore',
    };
}

function parseBackupMetadata(path) {
    const fileName = path.split('/').pop() || path;
    let middle = null;

    for (const pattern of BACKUP_NAME_PATTERNS) {
        const match = fileName.match(pattern);
        if (match) {
            middle = (match[1] || '').trim();
            break;
        }
    }

    if (!middle) {
        return null;
    }

    const parts = middle.split('_').filter(Boolean);
    if (!parts.length) {
        return null;
    }

    const section = (parts[0] === 'dev' || parts[0] === 'prod') ? parts[1] : parts[0];
    if (!section) {
        return null;
    }

    return { fileName, section };
}

function parseMatchingBackups(sources) {
    return sources
        .map((source) => {
            const metadata = parseBackupMetadata(source.path);
            return metadata ? { source, metadata } : null;
        })
        .filter(Boolean);
}

function populateSectionOptions(parsedBackups) {
    const sections = [...new Set(parsedBackups.map((entry) => entry.metadata.section))].sort();
    const currentSection = sourceSection.value;

    sourceSection.innerHTML = '';
    for (const section of sections) {
        const option = document.createElement('option');
        option.value = section;
        option.textContent = section;
        if (section === currentSection) {
            option.selected = true;
        }
        sourceSection.appendChild(option);
    }

    if (sections.length && !sections.includes(currentSection)) {
        sourceSection.value = sections[0];
    }
}

function filterSourcesBySection(parsedBackups) {
    if (!parsedBackups.length) {
        return [];
    }

    const section = sourceSection.value;
    if (!section) {
        return parsedBackups.map((entry) => entry.source);
    }

    const filtered = parsedBackups.filter((entry) => {
        return entry.metadata.section === section;
    });
    return filtered.length ? filtered.map((entry) => entry.source) : parsedBackups.map((entry) => entry.source);
}

async function loadHealth() {
    const health = await fetchJson('/health');
    healthStatus.textContent = health.status;
    clusterStatus.textContent = health.kubernetes_connected ? 'Connected' : 'Offline';
}

async function loadNamespaces() { }

async function loadSources() {
    const environment = sourceEnvironment.value;
    const namespace = encodeURIComponent(ENVIRONMENT_NAMESPACES[environment] || environment);
    const sources = await fetchJson(`/api/restore-sources?environment=${encodeURIComponent(environment)}&namespace=${namespace}`);
    const parsedBackups = parseMatchingBackups(sources);
    populateSectionOptions(parsedBackups);
    const displayedSources = filterSourcesBySection(parsedBackups);
    sourceSelect.innerHTML = '';
    if (!displayedSources.length) {
        const option = document.createElement('option');
        option.value = '';
        option.textContent = 'No backups found matching pattern grafana_(section)-YYYY.MM.DD-...backup';
        sourceSelect.appendChild(option);
        return;
    }

    for (const source of displayedSources) {
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
