/**
 * BotClusters Pro 8.0 - Client Interactive Controller
 * Maintainer: Goutham Josh
 */

let socket;
let updateInterval;
let reconnectAttempts = 0;
const MAX_RECONNECT_ATTEMPTS = 5;

let allProcesses = [];
let currentFilter = 'all';
let currentSearchQuery = '';
let currentSort = 'slot';

let currentViewingProcess = null;
let currentConsoleProcess = null;

document.addEventListener('DOMContentLoaded', function () {
    initSocket();
    initGlobalEvents();
    loadCronSetting();
});

// ── SocketIO Initialization ─────────────────────────────────────
function initSocket() {
    socket = io({
        reconnection: true,
        reconnectionDelay: 1000,
        reconnectionDelayMax: 5000,
        reconnectionAttempts: MAX_RECONNECT_ATTEMPTS
    });

    socket.on('connect', function () {
        console.log('[BotClusters] Connected to SocketIO');
        reconnectAttempts = 0;
        requestStatus();
        if (updateInterval) clearInterval(updateInterval);
        updateInterval = setInterval(requestStatus, 3000);
    });

    socket.on('disconnect', function () {
        console.warn('[BotClusters] Disconnected from SocketIO');
        if (updateInterval) clearInterval(updateInterval);
    });

    socket.on('connect_error', function (err) {
        console.warn('[BotClusters] SocketIO connection error:', err);
        reconnectAttempts++;
    });

    socket.on('status_update', function (data) {
        if (data && data.status === 'error' && data.message === 'Unauthorized') {
            window.location.href = '/login';
            return;
        }

        if (data.system) {
            updateSystemBar(data.system);
        }

        if (data.processes && Array.isArray(data.processes)) {
            allProcesses = data.processes;
            renderBotDashboard();
        } else {
            renderEmptyState();
        }
    });
}

function requestStatus() {
    if (socket && socket.connected) {
        socket.emit('request_status');
    } else {
        socket.connect();
    }
}

// ── Global DOM Events ───────────────────────────────────────────
function initGlobalEvents() {
    // Close dropdowns when clicking outside
    document.addEventListener('click', function (e) {
        const batchBtn = document.getElementById('btn-batch-actions');
        const batchMenu = document.getElementById('batch-dropdown-menu');
        if (batchMenu && batchBtn && !batchBtn.contains(e.target) && !batchMenu.contains(e.target)) {
            batchMenu.classList.remove('show');
        }
    });

    // Close modals on Escape key
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') {
            closeAllModals();
        }
    });

    // Handle visibility changes
    document.addEventListener('visibilitychange', function () {
        if (document.hidden) {
            if (updateInterval) clearInterval(updateInterval);
        } else {
            requestStatus();
            updateInterval = setInterval(requestStatus, 3000);
        }
    });

    // Log modal download hook
    const downloadBtn = document.getElementById('download-log-btn');
    if (downloadBtn) {
        downloadBtn.onclick = function () {
            if (currentViewingProcess) downloadLogs(currentViewingProcess);
        };
    }
}

function closeAllModals() {
    closeBotModal();
    closeConsoleModal();
    closeLogModal();
    closeCronModal();
    closeSettingsModal();
    closeBackupModal();
}

// ── System Resource Bar ─────────────────────────────────────────
function updateSystemBar(sys) {
    const cpuVal = Math.round(sys.cpu_percent || 0);
    const cpuBar = document.getElementById('sys-cpu-bar');
    const cpuText = document.getElementById('sys-cpu-val');
    if (cpuBar) {
        cpuBar.style.width = `${Math.min(100, cpuVal)}%`;
        cpuBar.style.backgroundColor = cpuVal > 85 ? '#ef4444' : (cpuVal > 65 ? '#f59e0b' : '#6366f1');
    }
    if (cpuText) cpuText.textContent = `${cpuVal}%`;

    const ramPercent = Math.round(sys.ram_percent || 0);
    const ramBar = document.getElementById('sys-ram-bar');
    const ramText = document.getElementById('sys-ram-val');
    if (ramBar) {
        ramBar.style.width = `${Math.min(100, ramPercent)}%`;
        ramBar.style.backgroundColor = ramPercent > 85 ? '#ef4444' : (ramPercent > 70 ? '#f59e0b' : '#3b82f6');
    }
    if (ramText) ramText.textContent = `${sys.ram_used_mb} / ${sys.ram_total_mb} MB (${ramPercent}%)`;

    const diskPercent = Math.round(sys.disk_percent || 0);
    const diskBar = document.getElementById('sys-disk-bar');
    const diskText = document.getElementById('sys-disk-val');
    if (diskBar) {
        diskBar.style.width = `${Math.min(100, diskPercent)}%`;
        diskBar.style.backgroundColor = diskPercent > 90 ? '#ef4444' : '#06b6d4';
    }
    if (diskText) diskText.textContent = `${sys.disk_used_gb} / ${sys.disk_total_gb} GB (${diskPercent}%)`;

    const uptimeEl = document.getElementById('sys-uptime');
    if (uptimeEl && sys.uptime) uptimeEl.textContent = `⏱️ Up: ${sys.uptime}`;

    const pyEl = document.getElementById('sys-python');
    if (pyEl && sys.python_version) pyEl.textContent = `🐍 Python ${sys.python_version}`;
}

// ── Bot Dashboard Rendering & Filtering ─────────────────────────
function handleSearchFilter() {
    const input = document.getElementById('bot-search-input');
    currentSearchQuery = (input ? input.value : '').toLowerCase().trim();

    const clearBtn = document.getElementById('clear-search-btn');
    if (clearBtn) clearBtn.style.display = currentSearchQuery ? 'inline-block' : 'none';

    const sortSelect = document.getElementById('sort-select');
    if (sortSelect) currentSort = sortSelect.value;

    renderBotDashboard();
}

function clearSearch() {
    const input = document.getElementById('bot-search-input');
    if (input) {
        input.value = '';
        handleSearchFilter();
    }
}

function setFilter(filter) {
    currentFilter = filter;
    document.querySelectorAll('.filter-chip').forEach(btn => {
        btn.classList.toggle('active', btn.getAttribute('data-filter') === filter);
    });
    renderBotDashboard();
}

function renderBotDashboard() {
    const botGrid = document.getElementById('bot-grid');
    if (!botGrid) return;

    let online = 0, offline = 0, paused = 0, fatal = 0;
    allProcesses.forEach(p => {
        const isRunning = p.status === 'RUNNING';
        const isPaused = Boolean(p.paused);
        const isAutoPaused = Boolean(p.auto_paused);

        if (isAutoPaused || p.status === 'FATAL' || p.status === 'BACKOFF') fatal++;
        else if (isPaused) paused++;
        else if (isRunning) online++;
        else offline++;
    });

    const setEl = (id, text) => { const el = document.getElementById(id); if (el) el.textContent = text; };
    setEl('chip-count-all', allProcesses.length);
    setEl('chip-count-online', online);
    setEl('chip-count-offline', offline);
    setEl('chip-count-paused', paused);
    setEl('chip-count-fatal', fatal);
    setEl('bot-count-badge', `${allProcesses.length} Bot${allProcesses.length !== 1 ? 's' : ''}`);

    let filtered = allProcesses.filter(p => {
        const isRunning = p.status === 'RUNNING';
        const isPaused = Boolean(p.paused);
        const isAutoPaused = Boolean(p.auto_paused);
        const isFatal = isAutoPaused || p.status === 'FATAL' || p.status === 'BACKOFF';

        if (currentFilter === 'online' && (!isRunning || isPaused || isAutoPaused)) return false;
        if (currentFilter === 'offline' && (isRunning || isPaused || isFatal)) return false;
        if (currentFilter === 'paused' && !isPaused) return false;
        if (currentFilter === 'fatal' && !isFatal) return false;

        if (currentSearchQuery) {
            const str = `${p.name} ${p.slot || ''} ${p.git_url || ''} ${p.run_command || ''}`.toLowerCase();
            if (!str.includes(currentSearchQuery)) return false;
        }
        return true;
    });

    // Sorting
    filtered.sort((a, b) => {
        if (currentSort === 'name') return a.name.localeCompare(b.name);
        if (currentSort === 'status') return a.status.localeCompare(b.status);
        if (currentSort === 'memory') return (b.memory_mb || 0) - (a.memory_mb || 0);
        if (currentSort === 'uptime') return (b.uptime || '').localeCompare(a.uptime || '');
        // default: slot
        const numA = parseInt((a.slot || '').replace(/\D/g, '') || 999);
        const numB = parseInt((b.slot || '').replace(/\D/g, '') || 999);
        return numA - numB;
    });

    if (filtered.length === 0) {
        if (allProcesses.length === 0) {
            renderEmptyState();
        } else {
            botGrid.innerHTML = `
                <div class="empty-state" style="grid-column: 1 / -1; text-align: center; padding: 48px; background: #111723; border: 1px dashed #222d3f; border-radius: 14px;">
                    <p style="color: #94a3b8; font-size: 15px;">No bots match the selected filter or search query.</p>
                    <button class="btn btn-secondary btn-sm" style="margin-top: 12px;" onclick="clearSearch(); setFilter('all');">Reset Filters</button>
                </div>
            `;
        }
        return;
    }

    botGrid.innerHTML = '';
    filtered.forEach(p => {
        botGrid.appendChild(createBotCard(p));
    });
}

function renderEmptyState() {
    const botGrid = document.getElementById('bot-grid');
    if (!botGrid) return;
    botGrid.innerHTML = `
        <div class="empty-state" style="grid-column: 1 / -1; text-align: center; padding: 60px 24px; background: #111723; border: 1px dashed #222d3f; border-radius: 14px;">
            <div style="font-size: 40px; margin-bottom: 12px;">🤖</div>
            <h3 style="color: #f1f5f9; margin-bottom: 8px; font-size: 18px;">No Bots Configured Yet</h3>
            <p style="color: #94a3b8; font-size: 14px; max-width: 480px; margin: 0 auto 20px;">
                Deploy Telegram bots, Discord bots, or background scripts with automatic restart, process isolation, and live monitoring.
            </p>
            <button class="btn btn-primary" onclick="openAddBotModal()">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="12" y1="5" x2="12" y2="19"></line><line x1="5" y1="12" x2="19" y2="12"></line></svg>
                Deploy First Bot
            </button>
        </div>
    `;
}

function createBotCard(p) {
    const isRunning = p.status === 'RUNNING';
    const isPaused = Boolean(p.paused);
    const isAutoPaused = Boolean(p.auto_paused);
    const isFatal = isAutoPaused || p.status === 'FATAL' || p.status === 'BACKOFF';

    let statusClass, statusLabel;
    if (isFatal) {
        statusClass = 'status-fatal';
        statusLabel = isAutoPaused ? 'Auto-Paused' : (p.status || 'Failed');
    } else if (isPaused) {
        statusClass = 'status-paused';
        statusLabel = 'Paused';
    } else if (isRunning) {
        statusClass = 'status-online';
        statusLabel = 'Online';
    } else {
        statusClass = 'status-offline';
        statusLabel = 'Stopped';
    }

    const card = document.createElement('div');
    card.className = 'bot-card' + (isFatal ? ' card-fatal' : '');

    const gitShort = p.git_info && p.git_info.commit_hash ?
        `<span class="git-badge">#${p.git_info.commit_hash}</span> ${p.git_info.commit_message ? p.git_info.commit_message.slice(0, 32) + '...' : ''}` :
        (p.branch ? `Branch: ${p.branch}` : 'Git Linked');

    const displayName = p.name.replace(/_/g, ' ');
    const slotTag = p.slot ? `<span class="bot-slot-tag">${p.slot}</span>` : '';

    let primaryControls = '';
    if (isAutoPaused || isFatal) {
        primaryControls = `
            <button onclick="clearFailure('${p.name}')" class="action-btn btn-restart" style="grid-column: span 3;">
                🔄 Reset Failure &amp; Start
            </button>
        `;
    } else {
        primaryControls = `
            <button onclick="toggleBot('${p.name}', '${p.status}')" class="action-btn ${isRunning ? 'btn-stop' : 'btn-start'}">
                ${isRunning ? '⏹ Stop' : '▶ Start'}
            </button>
            <button onclick="restartBot('${p.name}')" class="action-btn btn-restart" ${!isRunning ? 'disabled' : ''}>
                🔄 Restart
            </button>
            <button onclick="${isPaused ? `resumeBot('${p.name}')` : `pauseBot('${p.name}')`}" class="action-btn" ${!isRunning ? 'disabled' : ''}>
                ${isPaused ? '▶ Resume' : '⏸ Pause'}
            </button>
        `;
    }

    card.innerHTML = `
        <div>
            <div class="bot-header">
                <div class="bot-title-group">
                    <h2>${displayName}</h2>
                    ${slotTag}
                </div>
                <div class="bot-status-pill ${statusClass}">
                    <span class="status-dot"></span>
                    ${statusLabel}
                </div>
            </div>

            <div class="bot-info-grid">
                <div class="info-item">
                    <span class="info-label">PID</span>
                    <span class="info-value mono">${p.pid || '—'}</span>
                </div>
                <div class="info-item">
                    <span class="info-label">UPTIME</span>
                    <span class="info-value mono">${p.uptime || '0:00:00'}</span>
                </div>
                <div class="info-item">
                    <span class="info-label">RAM USAGE</span>
                    <span class="info-value mono">${p.memory_mb ? p.memory_mb + ' MB' : '0 MB'}</span>
                </div>
                <div class="info-item">
                    <span class="info-label">CPU LOAD</span>
                    <span class="info-value mono">${p.cpu_percent ? p.cpu_percent + '%' : '0%'}</span>
                </div>
                <div class="info-git-row">
                    <span>📦</span>
                    <span>${gitShort}</span>
                </div>
            </div>
        </div>

        <div>
            <div class="bot-actions-primary">
                ${primaryControls}
            </div>

            <div class="bot-actions-secondary">
                <button class="sub-btn" onclick="viewLogs('${p.name}')" title="View Recent Logs">📄 Logs</button>
                <button class="sub-btn" onclick="openBotConsole('${p.name}')" title="Terminal Console">💻 Console</button>
                <button class="sub-btn" onclick="gitPullBot('${p.name}')" title="Pull latest Git commit & reload">⬇ Pull</button>
                <button class="sub-btn" onclick="openEditBotModal('${p.slot || p.name}')" title="Edit Configuration">✏️ Edit</button>
                <button class="sub-btn sub-btn-danger" onclick="deleteBot('${p.slot || p.name}', '${p.name}')" title="Delete Bot Cluster">🗑</button>
            </div>
        </div>
    `;

    return card;
}

// ── Bot Actions ─────────────────────────────────────────────────
function toggleBot(processName, currentStatus) {
    const action = currentStatus === 'RUNNING' ? 'stop' : 'start';
    showToast(`${action === 'start' ? 'Starting' : 'Stopping'} ${processName}...`, 'info');

    fetch(`/supervisor/${action}/${encodeURIComponent(processName)}`, { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            if (data.status === 'success') {
                showToast(`Successfully ${action}ed ${processName}!`, 'success');
                setTimeout(requestStatus, 800);
            } else {
                showToast(`Error: ${data.message}`, 'error');
            }
        })
        .catch(err => showToast(`Network error: ${err.message}`, 'error'));
}

function restartBot(processName) {
    showToast(`Restarting ${processName}...`, 'info');
    fetch(`/supervisor/restart/${encodeURIComponent(processName)}`, { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            if (data.status === 'success') {
                showToast(`Successfully restarted ${processName}!`, 'success');
                setTimeout(requestStatus, 1500);
            } else {
                showToast(`Error: ${data.message}`, 'error');
            }
        })
        .catch(err => showToast(`Failed to restart: ${err.message}`, 'error'));
}

function pauseBot(processName) {
    showToast(`Pausing ${processName}...`, 'info');
    fetch(`/supervisor/pause/${encodeURIComponent(processName)}`, { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            if (data.status === 'success') {
                showToast(`Paused ${processName}`, 'success');
                setTimeout(requestStatus, 800);
            } else {
                showToast(`Error: ${data.message}`, 'error');
            }
        });
}

function resumeBot(processName) {
    showToast(`Resuming ${processName}...`, 'info');
    fetch(`/supervisor/resume/${encodeURIComponent(processName)}`, { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            if (data.status === 'success') {
                showToast(`Resumed ${processName}`, 'success');
                setTimeout(requestStatus, 800);
            } else {
                showToast(`Error: ${data.message}`, 'error');
            }
        });
}

function clearFailure(processName) {
    showToast(`Resetting failure state for ${processName}...`, 'info');
    fetch(`/supervisor/clear_failure/${encodeURIComponent(processName)}`, { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            if (data.status === 'success') {
                showToast(`Cleared failures. Starting ${processName}...`, 'success');
                setTimeout(requestStatus, 1500);
            } else {
                showToast(`Error: ${data.message}`, 'error');
            }
        });
}

function gitPullBot(processName) {
    showToast(`Pulling latest Git commits for ${processName}...`, 'info');
    fetch(`/api/bots/pull/${encodeURIComponent(processName)}`, { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            if (data.status === 'success') {
                showToast(`Git pull complete! ${data.git_output || ''}`, 'success');
                setTimeout(requestStatus, 1500);
            } else {
                showToast(`Git pull failed: ${data.message}`, 'error');
            }
        })
        .catch(err => showToast(`Error: ${err.message}`, 'error'));
}

// ── Batch Actions ───────────────────────────────────────────────
function toggleBatchDropdown() {
    const menu = document.getElementById('batch-dropdown-menu');
    if (menu) menu.classList.toggle('show');
}

function executeBatchAction(action) {
    toggleBatchDropdown();
    const actionNames = { start_all: 'Start All', stop_all: 'Stop All', restart_all: 'Restart All' };
    if (!confirm(`Are you sure you want to ${actionNames[action]}?`)) return;

    showToast(`Executing ${actionNames[action]} on all bots...`, 'info');
    fetch(`/supervisor/batch/${action}`, { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            if (data.status === 'success') {
                showToast(data.message, 'success');
                setTimeout(requestStatus, 1500);
            } else {
                showToast(`Batch error: ${data.message}`, 'error');
            }
        })
        .catch(err => showToast(`Error: ${err.message}`, 'error'));
}

// ── Add / Edit Bot Modal ────────────────────────────────────────
function openAddBotModal() {
    document.getElementById('bot-modal-title').textContent = 'Add New Bot Cluster';
    document.getElementById('bot-modal-desc').textContent = 'Configure git repository, entry script, and environment variables.';
    document.getElementById('bot-modal-icon').textContent = '🤖';
    document.getElementById('btn-save-bot').textContent = 'Save & Deploy';
    document.getElementById('form-edit-slot').value = '';
    document.getElementById('form-bot-name').value = '';
    document.getElementById('form-slot').value = '';
    document.getElementById('form-git-url').value = '';
    document.getElementById('form-branch').value = 'main';
    document.getElementById('form-run-cmd').value = 'bot.py';
    document.getElementById('form-py-ver').value = '';
    document.getElementById('form-auto-start').checked = true;

    const envContainer = document.getElementById('env-rows-container');
    envContainer.innerHTML = '';
    // Preload an empty row
    addEnvRow();

    document.getElementById('bot-modal').style.display = 'block';
}

function openEditBotModal(slot) {
    showToast(`Loading configuration for ${slot}...`, 'info');
    fetch(`/api/bots/${encodeURIComponent(slot)}`)
        .then(r => r.json())
        .then(data => {
            if (data.status !== 'success') {
                showToast(`Failed to load bot config: ${data.message}`, 'error');
                return;
            }
            const b = data.bot;
            document.getElementById('bot-modal-title').textContent = `Edit Bot: ${b.bot_name} (${b.slot})`;
            document.getElementById('bot-modal-desc').textContent = 'Update repository, commands, or environment variables.';
            document.getElementById('bot-modal-icon').textContent = '✏️';
            document.getElementById('btn-save-bot').textContent = 'Update & Reload';

            document.getElementById('form-edit-slot').value = b.slot;
            document.getElementById('form-bot-name').value = b.bot_name;
            document.getElementById('form-slot').value = b.slot;
            document.getElementById('form-slot').disabled = true;
            document.getElementById('form-git-url').value = b.git_url || '';
            document.getElementById('form-branch').value = b.branch || 'main';
            document.getElementById('form-run-cmd').value = b.run_command || 'bot.py';
            document.getElementById('form-py-ver').value = b.python_version || '';

            const envContainer = document.getElementById('env-rows-container');
            envContainer.innerHTML = '';
            const envObj = b.env || {};
            const keys = Object.keys(envObj);
            if (keys.length > 0) {
                keys.forEach(k => addEnvRow(k, envObj[k]));
            } else {
                addEnvRow();
            }

            document.getElementById('bot-modal').style.display = 'block';
        })
        .catch(err => showToast(`Error: ${err.message}`, 'error'));
}

function closeBotModal() {
    const modal = document.getElementById('bot-modal');
    if (modal) modal.style.display = 'none';
    const slotInput = document.getElementById('form-slot');
    if (slotInput) slotInput.disabled = false;
}

function addEnvRow(key = '', val = '') {
    const container = document.getElementById('env-rows-container');
    if (!container) return;

    const row = document.createElement('div');
    row.className = 'env-row';
    row.innerHTML = `
        <input type="text" class="env-key" placeholder="KEY (e.g. BOT_TOKEN)" value="${escapeHtml(key)}">
        <input type="text" class="env-val" placeholder="VALUE" value="${escapeHtml(val)}">
        <button type="button" class="btn-del-env" onclick="this.parentElement.remove()" title="Remove variable">&times;</button>
    `;
    container.appendChild(row);
}

function handleBotFormSubmit(e) {
    e.preventDefault();

    const editSlot = document.getElementById('form-edit-slot').value;
    const botName = document.getElementById('form-bot-name').value.trim();
    const slot = document.getElementById('form-slot').value.trim();
    const gitUrl = document.getElementById('form-git-url').value.trim();
    const branch = document.getElementById('form-branch').value.trim() || 'main';
    const runCmd = document.getElementById('form-run-cmd').value.trim() || 'bot.py';
    const pyVer = document.getElementById('form-py-ver').value || null;
    const autoStart = document.getElementById('form-auto-start').checked;

    const env = {};
    document.querySelectorAll('#env-rows-container .env-row').forEach(row => {
        const k = row.querySelector('.env-key').value.trim();
        const v = row.querySelector('.env-val').value.trim();
        if (k) env[k] = v;
    });

    const payload = {
        slot: slot || undefined,
        bot_name: botName,
        git_url: gitUrl,
        branch: branch,
        run_command: runCmd,
        python_version: pyVer,
        env: env,
        auto_start: autoStart
    };

    const isEdit = Boolean(editSlot);
    const endpoint = isEdit ? `/api/bots/update/${encodeURIComponent(editSlot)}` : '/api/bots/add';

    showToast(isEdit ? `Updating ${botName}...` : `Deploying ${botName}...`, 'info');

    fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
    })
        .then(r => r.json())
        .then(data => {
            if (data.status === 'success') {
                showToast(data.message, 'success');
                closeBotModal();
                setTimeout(requestStatus, 1200);
            } else {
                showToast(`Failed: ${data.message}`, 'error');
            }
        })
        .catch(err => showToast(`Error saving bot: ${err.message}`, 'error'));
}

function deleteBot(slot, botName) {
    if (!confirm(`Are you sure you want to delete bot "${botName}" (${slot})?\nThis removes its supervisor configuration.`)) return;

    const cleanFiles = confirm("Would you also like to delete the cloned repository directory and virtual environment to free up disk space?");

    showToast(`Deleting ${botName}...`, 'info');
    fetch(`/api/bots/delete/${encodeURIComponent(slot)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ clean_files: cleanFiles })
    })
        .then(r => r.json())
        .then(data => {
            if (data.status === 'success') {
                showToast(data.message, 'success');
                setTimeout(requestStatus, 800);
            } else {
                showToast(`Error: ${data.message}`, 'error');
            }
        })
        .catch(err => showToast(`Error: ${err.message}`, 'error'));
}

// ── Bot Terminal / Console ──────────────────────────────────────
function openBotConsole(processName) {
    currentConsoleProcess = processName;
    document.getElementById('console-modal-title').textContent = `Terminal Console: ${processName}`;
    document.getElementById('console-modal-desc').textContent = `Executing commands inside bot directory.`;
    document.getElementById('console-output').textContent = `Connected to bot environment: ${processName}\nReady. Select a preset or type a command below...\n`;
    document.getElementById('console-modal').style.display = 'block';
    document.getElementById('console-input').focus();
}

function closeConsoleModal() {
    const modal = document.getElementById('console-modal');
    if (modal) modal.style.display = 'none';
    currentConsoleProcess = null;
}

function runPresetCommand(cmd) {
    document.getElementById('console-input').value = cmd;
    handleConsoleSubmit(new Event('submit'));
}

function handleConsoleSubmit(e) {
    if (e && e.preventDefault) e.preventDefault();
    if (!currentConsoleProcess) return;

    const input = document.getElementById('console-input');
    const cmd = input.value.trim();
    if (!cmd) return;

    const screen = document.getElementById('console-output');
    screen.textContent += `\n$ ${cmd}\nRunning...`;
    screen.scrollTop = screen.scrollHeight;
    input.value = '';

    fetch(`/api/bots/exec/${encodeURIComponent(currentConsoleProcess)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ command: cmd })
    })
        .then(r => r.json())
        .then(data => {
            if (data.status === 'success') {
                const out = (data.stdout || '') + (data.stderr ? `\n[STDERR]\n${data.stderr}` : '');
                screen.textContent += `\n${out || '(No output produced)'}\n[Exit code: ${data.exit_code}]\n`;
            } else {
                screen.textContent += `\n[Error]: ${data.message}\n`;
            }
            screen.scrollTop = screen.scrollHeight;
        })
        .catch(err => {
            screen.textContent += `\n[Network Error]: ${err.message}\n`;
            screen.scrollTop = screen.scrollHeight;
        });
}

// ── Log Modal ───────────────────────────────────────────────────
function viewLogs(processName) {
    currentViewingProcess = processName;
    const modal = document.getElementById('log-modal');
    const title = document.getElementById('log-modal-title');
    const content = document.getElementById('log-content');

    title.textContent = `Logs: ${processName}`;
    content.textContent = 'Fetching logs from server...';
    modal.style.display = 'block';

    refreshCurrentLogs();
}

function refreshCurrentLogs() {
    if (!currentViewingProcess) return;
    const content = document.getElementById('log-content');

    fetch(`/supervisor/log/${encodeURIComponent(currentViewingProcess)}`)
        .then(r => {
            if (r.status === 401) { window.location.href = '/login'; return null; }
            if (r.status === 404) return 'No logs recorded yet for this process.';
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            return r.text();
        })
        .then(text => {
            if (text !== null) {
                content.textContent = text || 'Log file is currently empty.';
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
        .then(r => r.blob())
        .then(blob => {
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.style.display = 'none';
            a.href = url;
            a.download = `${processName}_log.txt`;
            document.body.appendChild(a);
            a.click();
            window.URL.revokeObjectURL(url);
            document.body.removeChild(a);
            showToast(`Downloaded logs for ${processName}`, 'success');
        })
        .catch(err => showToast(`Download failed: ${err.message}`, 'error'));
}

// ── Settings & Telegram Alerts Modal ────────────────────────────
function openSettingsModal() {
    fetch('/api/settings')
        .then(r => r.json())
        .then(data => {
            if (data.status === 'success') {
                const s = data.settings;
                document.getElementById('set-username').value = s.admin_username || '';
                document.getElementById('set-password').value = '';
                document.getElementById('set-app-url').value = s.app_url || '';
                document.getElementById('set-ping-interval').value = s.ping_interval || 240;
                document.getElementById('set-telegram-chat').value = s.telegram_chat_id || '';
                document.getElementById('set-telegram-token').value = s.telegram_token_configured ? '••••••••••••' : '';
                document.getElementById('settings-modal').style.display = 'block';
            }
        })
        .catch(() => {
            document.getElementById('settings-modal').style.display = 'block';
        });
}

function closeSettingsModal() {
    const modal = document.getElementById('settings-modal');
    if (modal) modal.style.display = 'none';
}

function handleSettingsSubmit(e) {
    e.preventDefault();
    const payload = {
        admin_username: document.getElementById('set-username').value.trim() || undefined,
        admin_password: document.getElementById('set-password').value.trim() || undefined,
        app_url: document.getElementById('set-app-url').value.trim(),
        ping_interval: parseInt(document.getElementById('set-ping-interval').value, 10) || 240,
        telegram_chat_id: document.getElementById('set-telegram-chat').value.trim()
    };

    const tokenInput = document.getElementById('set-telegram-token').value.trim();
    if (tokenInput && !tokenInput.includes('•••')) {
        payload.telegram_token = tokenInput;
    }

    showToast('Saving settings...', 'info');
    fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
    })
        .then(r => r.json())
        .then(data => {
            if (data.status === 'success') {
                showToast('Settings saved successfully!', 'success');
                closeSettingsModal();
            } else {
                showToast(`Error: ${data.message}`, 'error');
            }
        })
        .catch(err => showToast(`Failed: ${err.message}`, 'error'));
}

function testTelegramAlert() {
    showToast('Sending test message to Telegram...', 'info');
    fetch('/api/notifications/test', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            if (data.status === 'success') {
                showToast(data.message, 'success');
            } else {
                showToast(data.message, 'error');
            }
        })
        .catch(err => showToast(`Test failed: ${err.message}`, 'error'));
}

// ── Backup & Restore Modal ──────────────────────────────────────
function openBackupModal() {
    document.getElementById('backup-modal').style.display = 'block';
}

function closeBackupModal() {
    const modal = document.getElementById('backup-modal');
    if (modal) modal.style.display = 'none';
}

function handleImportFile(e) {
    const file = e.target.files[0];
    if (!file) return;

    const reader = new FileReader();
    reader.onload = function (evt) {
        try {
            const json = JSON.parse(evt.target.result);
            showToast('Importing clusters...', 'info');
            fetch('/api/config/import', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(json)
            })
                .then(r => r.json())
                .then(data => {
                    if (data.status === 'success') {
                        showToast(`Successfully imported ${data.imported_count} bot cluster(s)!`, 'success');
                        closeBackupModal();
                        setTimeout(requestStatus, 1200);
                    } else {
                        showToast(`Import error: ${data.message}`, 'error');
                    }
                });
        } catch (err) {
            showToast('Invalid JSON file format.', 'error');
        }
    };
    reader.readAsText(file);
}

// ── Cron Schedule Modal ─────────────────────────────────────────
function openCronModal() {
    document.getElementById('cron-modal').style.display = 'block';
}

function closeCronModal() {
    const modal = document.getElementById('cron-modal');
    if (modal) modal.style.display = 'none';
}

function loadCronSetting() {
    fetch('/config/cron')
        .then(r => r.json())
        .then(data => {
            if (data && data.hours !== undefined) {
                const el = document.getElementById('cron-hours');
                if (el) el.value = data.hours;
            }
        })
        .catch(() => {});
}

function saveCron() {
    const hours = parseInt(document.getElementById('cron-hours').value, 10) || 0;
    fetch('/config/cron', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ hours: hours })
    })
        .then(r => r.json())
        .then(data => {
            if (data && data.status === 'success') {
                closeCronModal();
                showToast(`Auto-restart schedule set to: ${hours === 0 ? 'Disabled' : hours + ' hour(s)'}`, 'success');
            } else {
                showToast('Failed to save cron schedule', 'error');
            }
        })
        .catch(() => showToast('Failed to save cron schedule', 'error'));
}

// ── Toast Notification Engine ───────────────────────────────────
function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;

    let icon = 'ℹ️';
    if (type === 'success') icon = '✅';
    if (type === 'error') icon = '⚠️';

    toast.innerHTML = `
        <span class="toast-icon">${icon}</span>
        <span class="toast-msg">${escapeHtml(message)}</span>
    `;

    container.appendChild(toast);

    setTimeout(() => {
        toast.style.opacity = '0';
        setTimeout(() => toast.remove(), 300);
    }, 4500);
}

function escapeHtml(str) {
    if (!str) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}
