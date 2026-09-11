/**
 * app.js — Main Application Controller & Router
 * 
 * Coordinates:
 *   - Navigation tab routing & active panel transitions
 *   - Global toast notification alert system
 *   - WebSocket connection lifecycle to /ws/telemetry with auto-reconnect
 *   - Dispatching live telemetry packets to sub-controllers
 */

const App = (() => {
    let ws = null;
    let wsReconnectAttempts = 0;
    let wsReconnectTimer = null;
    let startTime = Date.now();

    function init() {
        initTabs();
        initUptimeTimer();

        // Initialize sub-controllers
        if (typeof Telemetry !== 'undefined') Telemetry.init();
        if (typeof Isolation !== 'undefined') Isolation.init();
        if (typeof DeadlockGraph !== 'undefined') DeadlockGraph.init();
        if (typeof Simulator !== 'undefined') Simulator.init();
        if (typeof Workloads !== 'undefined') Workloads.init();

        // Connect WebSocket
        connectWebSocket();
    }

    // ── Tab Routing ─────────────────────────────────────────────────────────

    function initTabs() {
        const tabs = document.querySelectorAll('.nav-tab');
        tabs.forEach(tab => {
            tab.addEventListener('click', () => {
                const targetTab = tab.getAttribute('data-tab');
                switchTab(targetTab);
            });
        });
    }

    function switchTab(tabName) {
        const tabs = document.querySelectorAll('.nav-tab');
        const panels = document.querySelectorAll('.tab-panel');

        tabs.forEach(t => {
            const isActive = t.getAttribute('data-tab') === tabName;
            t.classList.toggle('active', isActive);
            t.setAttribute('aria-selected', isActive ? 'true' : 'false');
        });

        panels.forEach(p => {
            const isTarget = p.id.toLowerCase() === `panel${tabName.toLowerCase()}`;
            p.classList.toggle('active', isTarget);
        });

        // If switching to Deadlock tab, trigger canvas redraw after panel becomes visible
        if (tabName === 'deadlock' && typeof DeadlockGraph !== 'undefined') {
            setTimeout(() => {
                DeadlockGraph.loadWfgScenario(document.getElementById('wfgScenarioSelect')?.value || 'circular_wait');
            }, 50);
        }
    }

    // ── WebSocket Telemetry Stream ──────────────────────────────────────────

    function connectWebSocket() {
        if (wsReconnectTimer) clearTimeout(wsReconnectTimer);

        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/ws/telemetry`;

        const dot = document.getElementById('wsStatusDot');
        const text = document.getElementById('wsStatusText');

        if (dot) dot.className = 'status-dot offline';
        if (text) text.textContent = 'Connecting...';

        try {
            ws = new WebSocket(wsUrl);

            ws.onopen = () => {
                wsReconnectAttempts = 0;
                if (dot) dot.className = 'status-dot';
                if (text) text.textContent = 'Live Kernel Feed';
                console.log('[WebSocket] Connected to host telemetry stream');
            };

            ws.onmessage = (event) => {
                try {
                    const data = JSON.parse(event.data);
                    if (data.type === 'telemetry') {
                        // Dispatch to telemetry sub-controller
                        if (typeof Telemetry !== 'undefined') {
                            Telemetry.update(data);
                        }
                        // Update active sandboxes in isolation panel if present
                        if (typeof Isolation !== 'undefined' && data.sandboxes) {
                            Isolation.updateFromTelemetry(data.sandboxes);
                        }
                    }
                } catch (e) {
                    console.error('[WebSocket] Failed to parse message:', e);
                }
            };

            ws.onclose = () => {
                if (dot) dot.className = 'status-dot offline';
                if (text) text.textContent = 'Disconnected';
                scheduleReconnect();
            };

            ws.onerror = (err) => {
                console.warn('[WebSocket] Connection error:', err);
                if (ws) ws.close();
            };

        } catch (e) {
            console.error('[WebSocket] Setup exception:', e);
            scheduleReconnect();
        }
    }

    function scheduleReconnect() {
        wsReconnectAttempts++;
        const delay = Math.min(5000, 1000 * Math.pow(1.5, wsReconnectAttempts));
        const text = document.getElementById('wsStatusText');
        if (text) text.textContent = `Reconnecting in ${(delay / 1000).toFixed(0)}s...`;

        wsReconnectTimer = setTimeout(connectWebSocket, delay);
    }

    // ── Global Toast Notifications ──────────────────────────────────────────

    function showToast(message, type = 'info') {
        const container = document.getElementById('toastContainer');
        if (!container) return;

        const toast = document.createElement('div');
        toast.className = `toast ${type}`;

        const icons = {
            success: '✅',
            error: '❌',
            warning: '⚠️',
            info: 'ℹ️'
        };

        toast.innerHTML = `<span style="margin-right: 6px;">${icons[type] || 'ℹ️'}</span>${escapeHtml(message)}`;
        container.appendChild(toast);

        // Auto remove after animation completes (3s)
        setTimeout(() => {
            if (toast.parentElement) {
                toast.parentElement.removeChild(toast);
            }
        }, 3000);
    }

    // ── Uptime Timer ────────────────────────────────────────────────────────

    function initUptimeTimer() {
        const el = document.getElementById('systemUptime');
        if (!el) return;

        setInterval(() => {
            const elapsedSec = Math.floor((Date.now() - startTime) / 1000);
            const hrs = Math.floor(elapsedSec / 3600);
            const mins = Math.floor((elapsedSec % 3600) / 60);
            const secs = elapsedSec % 60;

            const pad = (n) => String(n).padStart(2, '0');
            el.textContent = `Uptime: ${pad(hrs)}:${pad(mins)}:${pad(secs)}`;
        }, 1000);
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

    return {
        init,
        switchTab,
        showToast
    };
})();

// Initialize once DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    App.init();
});
