let socket;
let updateInterval;
let reconnectAttempts = 0;
const MAX_RECONNECT_ATTEMPTS = 5;
let currentViewingProcess = null;

// Keep a reference to the last known process list so cards never flicker/disappear
let lastKnownProcesses = [];

document.addEventListener('DOMContentLoaded', function () {
    socket = io({
        reconnection: true,
        reconnectionDelay: 1000,
        reconnectionDelayMax: 5000,
        reconnectionAttempts: MAX_RECONNECT_ATTEMPTS
    });

    socket.on('connect', function () {
        console.log('Connected to BotClusters SocketIO server');
        reconnectAttempts = 0;
        requestStatus();
        if (updateInterval) clearInterval(updateInterval);
        updateInterval = setInterval(requestStatus, 3000);
    });

    socket.on('disconnect', function () {
        console.log('Disconnected from BotClusters SocketIO server');
        if (updateInterval) clearInterval(updateInterval);
    });

    socket.on('connect_error', function (error) {
        console.warn('SocketIO connection error:', error);
        reconnectAttempts++;
        if (reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
            console.error('Max SocketIO reconnection attempts reached');
        }
    });

    socket.on('status_update', function (data) {
        if (data && data.status === 'error' && data.message === 'Unauthorized') {
            window.location.href = '/login';
            return;
        }
        if (data.processes && Array.isArray(data.processes) && data.processes.length > 0) {
            lastKnownProcesses = data.processes;
            updateBotCards(data.processes);
        } else if (lastKnownProcesses.length > 0) {
            updateBotCards(lastKnownProcesses);
        } else {
            renderEmptyState();
        }
    });

    // Window clicks to close modals
    window.onclick = function (event) {
        const logModal = document.getElementById('log-modal');
        if (event.target === logModal) closeLogModal();
        const cronModal = document.getElementById('cron-modal');
        if (event.target === cronModal) closeCronModal();
    };

    // Modal download button hook
    const downloadBtn = document.getElementById('download-log-btn');
    if (downloadBtn) {
        downloadBtn.onclick = function () {
            if (currentViewingProcess) {
                downloadLogs(currentViewingProcess);
            }
        };
    }

    loadCronSetting();
});

function requestStatus() {
    if (socket && socket.connected) {
        socket.emit('request_status');
    } else {
        socket.connect();
    }
}

function getBotNumber(processName) {
    const match = processName.match(/bot(\d+)$/i);
    return match ? parseInt(match[1]) : null;
}

function sortProcesses(processes) {
    return processes.sort((a, b) => {
        const numA = getBotNumber(a.name) || 0;
        const numB = getBotNumber(b.name) || 0;
        if (numA && numB) return numA - numB;
        return a.name.localeCompare(b.name);
    });
}

function formatBotName(processName) {
    const botNumber = getBotNumber(processName);
    if (botNumber !== null) {
        return `Bot #${botNumber}`;
    }
    // Clean up random adjective prefixes if present
    const parts = processName.replace(/_/g, ' ').split(' ');
    if (parts.length > 2) {
        return parts.slice(2).join(' ').toUpperCase();
    }
    return processName.replace(/_/g, ' ');
}

function renderEmptyState() {
    const botGrid = document.getElementById('bot-grid');
    if (!botGrid) return;
    botGrid.innerHTML = `
        <div class="empty-state" style="grid-column: 1 / -1; text-align: center; padding: 48px; background: #161b22; border: 1px dashed #30363d; border-radius: 12px;">
            <h3 style="color: #8b949e; margin-bottom: 8px;">No Bots Running</h3>
            <p style="color: #6e7681; font-size: 14px;">Configure CLUSTER_01, CLUSTER_02, etc., in your cluster.env file to start bots.</p>
        </div>
    `;
    const el = (id) => document.getElementById(id);
    if (el('stat-online')) el('stat-online').textContent = '0';
    if (el('stat-offline')) el('stat-offline').textContent = '0';
    if (el('stat-paused')) el('stat-paused').textContent = '0';
    if (el('bot-count-badge')) el('bot-count-badge').textContent = '0 bots';
}

function updateBotCards(processes) {
    const botGrid = document.getElementById('bot-grid');
    if (!botGrid) return;

    const sorted = sortProcesses(processes);

    let online = 0, offline = 0, paused = 0;
    botGrid.innerHTML = '';

    sorted.forEach((process) => {
        const isRunning = process.status === 'RUNNING';
        const isPaused = Boolean(process.paused);
        const isAutoPaused = Boolean(process.auto_paused);

        if (isRunning && !isPaused && !isAutoPaused) online++;
        else if (isPaused || isAutoPaused) paused++;
        else offline++;

        const displayName = formatBotName(process.name);
        const now = new Date();
        const utcTime = now.toISOString().replace('T', ' ').slice(0, 19);

        let statusClass, statusLabel;
        if (isAutoPaused) {
            statusClass = 'status-fatal';
            statusLabel = 'Failed';
        } else if (isPaused) {
            statusClass = 'status-paused';
            statusLabel = 'Paused';
        } else if (isRunning) {
            statusClass = 'status-online';
            statusLabel = 'Online';
        } else {
            statusClass = 'status-offline';
            statusLabel = process.status || 'Offline';
        }

        const card = document.createElement('div');
        card.className = 'bot-card' + (isAutoPaused ? ' card-fatal' : '');

        let controlsHTML = '';
        if (isAutoPaused) {
            controlsHTML = `
                <button onclick="clearFailure('${process.name}')" class="control-btn clear-btn">Clear &amp; Restart</button>
                <button onclick="viewLogs('${process.name}')" class="control-btn log-btn">Logs</button>
            `;
        } else {
            controlsHTML = `
                <button onclick="toggleBot('${process.name}', '${process.status}')"
                        class="control-btn ${isRunning ? 'stop-btn' : 'start-btn'}">
                    ${isRunning ? 'Stop' : 'Start'}
                </button>
                <button onclick="restartBot('${process.name}')"
                        class="control-btn restart-btn" ${!isRunning ? 'disabled' : ''}>
                    Restart
                </button>
                <button onclick="${isPaused ? `resumeBot('${process.name}')` : `pauseBot('${process.name}')`}"
                        class="control-btn pause-btn" ${!isRunning ? 'disabled' : ''}>
                    ${isPaused ? 'Resume' : 'Pause'}
                </button>
                <button onclick="viewLogs('${process.name}')" class="control-btn log-btn">Logs</button>
            `;
        }

        card.innerHTML = `
            <div class="bot-header">
                <h2>${displayName}</h2>
                <span class="bot-status ${statusClass}">${statusLabel}</span>
            </div>
            <div class="bot-info">
                <p><strong>Process:</strong> <code>${process.name}</code></p>
                <p><strong>Status:</strong> ${process.status}</p>
                <p><strong>PID:</strong> ${process.pid || 'N/A'}</p>
                <p><strong>Uptime:</strong> ${process.uptime || '0:00:00'}</p>
                <p><strong>Last Sync:</strong> ${utcTime}</p>
            </div>
            <div class="bot-controls">${controlsHTML}</div>
        `;

        botGrid.appendChild(card);
    });

    // Update header stats
    const el = (id) => document.getElementById(id);
    if (el('stat-online')) el('stat-online').textContent = online;
    if (el('stat-offline')) el('stat-offline').textContent = offline;
    if (el('stat-paused')) el('stat-paused').textContent = paused;
    if (el('bot-count-badge')) el('bot-count-badge').textContent = `${sorted.length} bot${sorted.length !== 1 ? 's' : ''}`;
}

// ── Bot Actions ──────────────────────────────────────────

function toggleBot(processName, currentStatus) {
    const action = currentStatus === 'RUNNING' ? 'stop' : 'start';
    if (action === 'stop' && !confirm(`Stop ${formatBotName(processName)}?`)) return;

    fetch(`/supervisor/${action}/${encodeURIComponent(processName)}`, { method: 'POST' })
        .then(r => {
            if (r.status === 401) { window.location.href = '/login'; return null; }
            return r.json();
        })
        .then(data => {
            if (!data) return;
            if (data.status === 'success') setTimeout(requestStatus, 1000);
            else alert(`Error: ${data.message}`);
        })
        .catch(() => alert(`Failed to ${action} the process.`));
}

function restartBot(processName) {
    if (!confirm(`Restart ${formatBotName(processName)}?`)) return;
    fetch(`/supervisor/restart/${encodeURIComponent(processName)}`, { method: 'POST' })
        .then(r => {
            if (r.status === 401) { window.location.href = '/login'; return null; }
            return r.json();
        })
        .then(data => {
            if (!data) return;
            if (data.status === 'success') setTimeout(requestStatus, 2000);
            else alert(`Error: ${data.message}`);
        })
        .catch(() => alert('Failed to restart the process.'));
}

function pauseBot(processName) {
    fetch(`/supervisor/pause/${encodeURIComponent(processName)}`, { method: 'POST' })
        .then(r => {
            if (r.status === 401) { window.location.href = '/login'; return null; }
            return r.json();
        })
        .then(data => {
            if (!data) return;
            if (data.status === 'success') setTimeout(requestStatus, 1000);
            else alert(`Error: ${data.message}`);
        });
}

function resumeBot(processName) {
    fetch(`/supervisor/resume/${encodeURIComponent(processName)}`, { method: 'POST' })
        .then(r => {
            if (r.status === 401) { window.location.href = '/login'; return null; }
            return r.json();
        })
        .then(data => {
            if (!data) return;
            if (data.status === 'success') setTimeout(requestStatus, 1000);
            else alert(`Error: ${data.message}`);
        });
}

function clearFailure(processName) {
    fetch(`/supervisor/clear_failure/${encodeURIComponent(processName)}`, { method: 'POST' })
        .then(r => {
            if (r.status === 401) { window.location.href = '/login'; return null; }
            return r.json();
        })
        .then(data => {
            if (!data) return;
            if (data.status === 'success') setTimeout(requestStatus, 1500);
            else alert(`Error: ${data.message}`);
        })
        .catch(() => alert('Failed to clear failure state.'));
}

// ── Log Modal & Log Download ─────────────────────────────

function viewLogs(processName) {
    currentViewingProcess = processName;
    const modal = document.getElementById('log-modal');
    const title = document.getElementById('log-modal-title');
    const content = document.getElementById('log-content');

    title.textContent = `Logs: ${formatBotName(processName)} (${processName})`;
    content.textContent = 'Loading logs from server...';
    modal.style.display = 'block';

    fetch(`/supervisor/log/${encodeURIComponent(processName)}`)
        .then(r => {
            if (r.status === 401) { window.location.href = '/login'; return null; }
            if (r.status === 404) return 'No logs recorded yet for this process.';
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            return r.text();
        })
        .then(text => {
            if (text !== null) {
                content.textContent = text || 'No output recorded in log files.';
                content.scrollTop = content.scrollHeight;
            }
        })
        .catch(err => {
            content.textContent = `Failed to load logs: ${err.message}`;
        });
}

function closeLogModal() {
    const modal = document.getElementById('log-modal');
    if (modal) modal.style.display = 'none';
    currentViewingProcess = null;
}

function downloadLogs(processName) {
    fetch(`/supervisor/log/${encodeURIComponent(processName)}`)
        .then(response => {
            if (response.status === 401) { window.location.href = '/login'; return null; }
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            return response.blob();
        })
        .then(blob => {
            if (!blob) return;
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.style.display = 'none';
            a.href = url;
            a.download = `${processName}_log.txt`;
            document.body.appendChild(a);
            a.click();
            window.URL.revokeObjectURL(url);
            document.body.removeChild(a);
        })
        .catch(err => alert(`Failed to download logs: ${err.message}`));
}

// ── Cron Modal ───────────────────────────────────────────

function openCronModal() {
    document.getElementById('cron-modal').style.display = 'block';
}

function closeCronModal() {
    document.getElementById('cron-modal').style.display = 'none';
}

function loadCronSetting() {
    fetch('/config/cron')
        .then(r => {
            if (r.status === 401) return null;
            return r.json();
        })
        .then(data => {
            if (data && data.hours !== undefined) {
                const input = document.getElementById('cron-hours');
                if (input) input.value = data.hours;
            }
        })
        .catch(() => { });
}

function saveCron() {
    const hours = parseInt(document.getElementById('cron-hours').value, 10) || 0;
    fetch('/config/cron', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ hours: hours })
    })
        .then(r => {
            if (r.status === 401) { window.location.href = '/login'; return null; }
            return r.json();
        })
        .then(data => {
            if (data && data.status === 'success') {
                closeCronModal();
                alert(`Bot restart schedule updated: Every ${hours === 0 ? 'Disabled' : hours + ' hour(s)'}`);
            } else {
                alert('Failed to save cron setting.');
            }
        })
        .catch(() => alert('Failed to save cron setting.'));
}

// ── Visibility & Teardown ────────────────────────────────

document.addEventListener('visibilitychange', function () {
    if (document.hidden) {
        if (updateInterval) clearInterval(updateInterval);
    } else {
        requestStatus();
        updateInterval = setInterval(requestStatus, 3000);
    }
});

window.onbeforeunload = function () {
    if (socket) socket.disconnect();
    if (updateInterval) clearInterval(updateInterval);
};
