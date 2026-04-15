// ── Flip logic ────────────────────────────────────────────────────────────────
const flipper   = document.getElementById('flipper');
const backFace  = document.getElementById('back-face');

function flip(showBack) {
  flipper.classList.toggle('is-flipped', showBack);
  // Sync back-face height to front so the scene doesn't collapse
  if (showBack) {
    // let it render first
    requestAnimationFrame(() => {
      backFace.style.minHeight = flipper.offsetHeight + 'px';
    });
  }
}

document.getElementById('btn-flip-to-back').addEventListener('click', () => flip(true));
document.getElementById('btn-flip-to-front').addEventListener('click', () => flip(false));

// ── Clock + date ──────────────────────────────────────────────────────────────
const DAYS = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
function updateClock() {
  const now = new Date();
  document.getElementById('day-label').textContent = DAYS[now.getDay()];
  document.getElementById('time-label').textContent =
    now.toLocaleTimeString('en-GB', {hour:'2-digit', minute:'2-digit'});
}
updateClock();
setInterval(updateClock, 10000);

// ── Weather + status sentence: applied from SSE ───────────────────────────────
function applyWeather(w) {
  if (!w) return;
  document.getElementById('weather-icon').textContent = w.icon ?? '🌡️';
  document.getElementById('weather-desc').textContent = w.desc ?? '';
}
function applyStatusSentence(msg) {
  if (msg.status_sentence != null)
    document.getElementById('status-sentence').textContent = msg.status_sentence;
}

// ── Chart ─────────────────────────────────────────────────────────────────────
const dayStart = new Date(); dayStart.setHours(0,0,0,0);
const dayEnd   = new Date(); dayEnd.setHours(23,59,59,999);

const STATE_COLORS = {
  heating: 'rgba(255,140,66,.18)',
  resting: 'rgba(110,231,160,.10)',
  dhw:     'rgba(96,205,255,.13)',
  idle:    'rgba(255,255,255,.03)',
  waiting: 'rgba(255,255,255,.03)',
};

// Build annotation boxes from a list of {ts, state} events.
// Each event starts a band; the next event (or now) ends it.
function buildAnnotations(events) {
  const annotations = {};
  const now = new Date();
  for (let i = 0; i < events.length; i++) {
    const start = new Date(events[i].ts.replace('T', ' '));
    const end   = i + 1 < events.length
      ? new Date(events[i + 1].ts.replace('T', ' '))
      : now;
    const color = STATE_COLORS[events[i].state] ?? 'rgba(255,255,255,.03)';
    annotations[`band${i}`] = {
      type: 'box',
      xMin: start, xMax: end,
      yMin: -Infinity, yMax: Infinity,
      backgroundColor: color,
      borderWidth: 0,
    };
  }
  return annotations;
}

let stateEvents = [];

const chart = new Chart(document.getElementById('chart').getContext('2d'), {
  type: 'line',
  data: {
    datasets: [{
      data: [],
      borderColor: '#4fc3f7',
      backgroundColor: 'transparent',
      borderWidth: 1.5, pointRadius: 0, tension: 0.4, fill: false,
      spanGaps: 10 * 60 * 1000,
    }]
  },
  options: {
    animation: false, responsive: true, maintainAspectRatio: true,
    plugins: {
      legend: { display: false },
      annotation: { annotations: {} },
    },
    scales: {
      x: {
        type: 'time', min: dayStart, max: dayEnd,
        time: { unit: 'hour', displayFormats: { hour: 'HH:mm' } },
        ticks: { color: '#7a8099', font: { family: "'DM Mono'", size: 10 }, maxTicksLimit: 8, maxRotation: 0 },
        grid: { color: 'rgba(255,255,255,.04)' }, border: { color: 'transparent' }
      },
      y: {
        ticks: { color: '#7a8099', font: { family: "'DM Mono'", size: 10 } },
        grid: { color: 'rgba(255,255,255,.04)' }, border: { color: 'transparent' }
      }
    }
  }
});

function refreshAnnotations() {
  chart.options.plugins.annotation.annotations = buildAnnotations(stateEvents);
  chart.update('none');
}

// ── State update ──────────────────────────────────────────────────────────────
let lastWriteTime = null;

function applyState(msg) {
  if (msg.room_temp != null) {
    const temp = msg.room_temp.toFixed(1); // "21.5"
    const [num, dec] = temp.split('.');

    document.getElementById('lcd-num').textContent = num;
    document.getElementById('lcd-dec').textContent = dec;
  }

  if (msg.badge) {
  //   document.getElementById('action-badge').className = msg.badge.cls;
  //   document.getElementById('action-label').textContent = msg.badge.label;
    document.getElementById('status-dot').className =
      'status-dot ' + (msg.badge.active ? 'ok' : '');
  }
  if (msg.state) {
    const el = document.getElementById('info-state');
    if (el) {
      el.textContent = msg.state;
      el.className = 'chip-val ' + (msg.badge?.active ? 'ok' : 'warn');
    }
  }

  if (msg.status)
    document.getElementById('status-text').textContent = msg.status;

  if (msg.last_write) {
    lastWriteTime = new Date(msg.last_write.replace('T',' '));
    document.getElementById('info-write').textContent =
      lastWriteTime.toLocaleTimeString('en-GB', {hour:'2-digit', minute:'2-digit'});
  }
}

// ── SSE ───────────────────────────────────────────────────────────────────────
let es;
let lastSseMessage = Date.now();

function connectSSE() {
  if (es) { es.onmessage = null; es.onerror = null; es.close(); }
  es = new EventSource('/api/stream');
  es.onmessage = (e) => {
    lastSseMessage = Date.now();
    const msg = JSON.parse(e.data);
    if (msg.type === 'snapshot') {
      for (const p of (msg.history || []))
        chart.data.datasets[0].data.push({ x: new Date(p.ts.replace('T',' ')), y: p.value });
      stateEvents = msg.state_events || [];
      refreshAnnotations();
      applyState(msg);
      if (msg.last_write) applyState(msg);
      applyWeather(msg.weather);
      applyStatusSentence(msg);
    }
    if (msg.type === 'update') {
      if (msg.room_temp != null)
        chart.data.datasets[0].data.push({ x: new Date(msg.ts.replace('T',' ')), y: msg.room_temp });
      if (msg.state_event) {
        stateEvents.push(msg.state_event);
        refreshAnnotations();
      } else {
        chart.update('none');
      }
      applyState(msg);
      applyStatusSentence(msg);
      if (msg.wrote) lastWriteTime = new Date(msg.ts.replace('T',' '));
    }
    if (msg.type === 'weather') applyWeather(msg);
  };
}

connectSSE();

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState !== 'visible') return;
  const stale = es.readyState === EventSource.CLOSED
             || Date.now() - lastSseMessage > 60_000;
  if (stale) connectSSE();
});
