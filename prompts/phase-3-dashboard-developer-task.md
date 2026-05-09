# Task: Phase 3 — Cyberpunk live dashboard (frontend + backend static handler)

Name: codex-developer
Profile: Codex CLI 0.128+ (gpt-5.5, xhigh reasoning, --sandbox workspace-write)
Goal: Single-file HTML dashboard с inline CSS+JS, который polls backend Phase 2 endpoints и показывает три оси экономии (money/time/sentiment) в киберпанк-эстетике. Backend extends static-file handler для serving dashboard.
Constraints: vanilla HTML/CSS/JS (без React/Vue/build tools), self-contained single-file, киберпанк-стиль (синий неон + розовые/фиолетовые glitch'ы + monospace), polling без WebSocket, all-in-one для browser open
Watches: issue #N + Phase 2 backend (7 endpoints), Phase 1.0/1.0.3/1.4 metrics в JSONL/tasks.json
Produces: `dashboard/index.html` (новый, single-file со всем) + extended `backend/server.py` (static handler) + `dashboard/README.md`

## Operational backstory

You are running with `workspace-write` sandbox. Phase 2 backend `backend/server.py` уже в main и работает на `http://127.0.0.1:8089`. Frontend будет polling эти endpoints. Backend нужно extend'нуть — static file serving для `dashboard/index.html` через `/` или `/dashboard` route. CORS allow-all уже настроен.

**Sandbox limitation**: Codex не запускает реальный сервер для browser-test. Static checks (HTML valid, CSS valid, JS syntax) — Codex; runtime test (open `http://127.0.0.1:8089` в браузере) — architect на host.

## Working directory

`F:/WorkAI/multi-agent` (already your `--cd`).

## Project context

Read `CLAUDE.md`, `README.md`, `backend/server.py` (понять endpoints), `backend/README.md`. Также — для дизайн-стиля можно посмотреть существующие визуальные проекты пользователя (vanilla cyberpunk):

- `F:/WorkAI/visual/cyberpunk-anim-demo/` (если доступно) — пример cyberpunk-themed анимация
- Memory `user_design_preferences.md` — синий, киберпанк, баланс минимализма и wow

## Goal

Live dashboard — открываешь в браузере и видишь real-time картину твоей AI-экономии. Три hero-цифры наверху, графики ниже, recent sessions таблица. Каждые 5-30 секунд endpoint polled, UI обновляется без reload.

Используется как:
1. Local dev — пользователь смотрит на свою активность
2. **Phase 4** — обезличенный snapshot на блог-витрину (отдельная задача, не сейчас)

## Deliverables

### 1. `dashboard/index.html` (main file, single-file со всем)

Self-contained HTML с inline CSS и JS. Никаких external зависимостей (CDN). Открывается напрямую в браузере через `http://127.0.0.1:8089/` (после backend extension).

#### Layout

```
┌─────────────────────────────────────────────────────────────────┐
│ // MULTI-AGENT TRACKER · LIVE          agent ONLINE · 79.7k events │
├─────────────────────────────────────────────────────────────────┤
│   HERO row (3 big-number widgets)                              │
│                                                                 │
│      $59,949            ×7.3              216.6h               │
│   saved vs API      productivity      active time              │
│                                                                 │
├─────────────────────────────────────────────────────────────────┤
│   5h-budget remaining widget (live, polls every 5s)            │
│   ███████████░░░░░  1365% (burst mode active)                  │
│   tokens: 1,201,422 / 88k (Max 5x estimate) · cache: 162M     │
├─────────────────────────────────────────────────────────────────┤
│   Stress meter         │   Activity timeline (60 days)         │
│                        │                                       │
│   frustration  0.34    │   ▁▁▁▂▂▃▃▅▅▆▇█▇▆▅▄▃▂▁ (cost/day)     │
│   appreciation 0.62    │                                       │
│   profanity   47       │                                       │
│   stress trend ↘       │                                       │
├─────────────────────────────────────────────────────────────────┤
│   Models breakdown     │   Recent sessions (last 10)          │
│                        │                                       │
│   Opus 4.7 ████ 67%    │   2026-05-09 phase-2 backend $0.51   │
│   Opus 4.6 ██   12%    │   2026-05-09 phase-1.4 sentiment $0.45│
│   Sonnet 4.6 █  21%    │   ...                                 │
└─────────────────────────────────────────────────────────────────┘
```

#### Style guide (cyberpunk)

- **Background**: `#030712` (almost black) с radial gradient до `#0a1628`
- **Primary neon**: cyan `#06b6d4` / `#22d3ee`
- **Accent**: violet `#a855f7`, magenta `#ec4899`, pink-warning `#f472b6`
- **Text**: `#cbd5e1` body, `#67e8f9` labels, `#dbeafe` numbers
- **Font**: `'JetBrains Mono', 'Consolas', monospace` для всего (cyberpunk vibe)
- **Background pattern**: subtle grid (24px) `#164e63` 0.6 stroke 0.3 opacity
- **Big numbers**: `text-shadow: 0 0 20px <neon>` для glow effect
- **Glitch effect** на hero numbers (RGB-split анимация при load + раз в 30с):
  - Pseudo-elements `::before` `::after` с `text-shadow: 2px 0 #ec4899, -2px 0 #06b6d4`
  - keyframes `glitch` 0.3s
- **HUD-метки в углах**: `// LIVE`, `// LOCAL`, `agent ONLINE` мелким шрифтом + letter-spacing
- **Scanlines overlay** (опционально): linear-gradient horizontal lines 2px every 4px, opacity 0.03

#### Charts

**Activity timeline** — SVG bar chart, daily cost (или calls) over period. 60 days = 60 bars. Hover показывает date + cost.

**Models breakdown** — horizontal bars или donut. Donut проще через SVG circles с stroke-dasharray.

**Stress meter** — simple progress bars для frustration / appreciation. Или radial gauge (полукруг SVG).

**Budget bar** — full-width progress bar. Цвет переключается:
- 0-50%: cyan (normal)
- 50-100%: violet (warning)
- >100%: pink-magenta pulse animation (burst mode active)

#### JS — polling logic

```js
const POLL_FAST = 5000;   // budget, health
const POLL_MEDIUM = 30000;  // summary, productivity, sentiment
const POLL_SLOW = 60000;  // timeseries, sessions

async function pollHealth() {
  const r = await fetch('/api/health');
  const d = await r.json();
  document.getElementById('events-total').textContent = d.events_total.toLocaleString();
  document.getElementById('agent-status').classList.toggle('online', d.status === 'ok');
}
// ... аналогично для остальных endpoints
// setInterval queues для каждого
```

Error handling — если endpoint падает, indicator показывает «OFFLINE», UI не падает.

Initial data load — все polls вызываются сразу при load + setInterval.

#### Number formatting

- Money: `$59,949.83` (две decimal)
- Tokens: `1,201,422` (thousands separator)
- Hours: `216.6h`
- Percent: `1365%`
- Multiplier: `×7.3` (Greek × symbol)

### 2. Update `backend/server.py` — static file handler

Добавить в `do_GET()` маршрутизацию:
- `path == "/" or path == "/dashboard"` → serve `dashboard/index.html`
- `path.startswith("/dashboard/")` → serve файл из `dashboard/` (для CSS/JS если когда split'нём, future-proof)
- Остальное (e.g. `/api/...`) — current logic

Static handler:
```python
def serve_static(self, file_path: Path):
    if not file_path.exists() or not file_path.is_file():
        self.send_error_json(404, "static file not found")
        return
    content = file_path.read_bytes()
    content_type = "text/html; charset=utf-8" if file_path.suffix == ".html" else \
                   "text/css" if file_path.suffix == ".css" else \
                   "application/javascript" if file_path.suffix == ".js" else \
                   "application/octet-stream"
    self.send_response(200)
    self.send_header("Content-Type", content_type)
    self.send_header("Content-Length", str(len(content)))
    self.end_headers()
    self.wfile.write(content)
```

Path traversal protection: использовать `Path.resolve()` и проверять что resolved path внутри `dashboard/` directory.

### 3. `dashboard/README.md`

```markdown
# Dashboard

Single-file cyberpunk live dashboard для Phase 1.0+ метрик.

## Run

1. Запустить backend: `py -3.14 backend/server.py --port 8089`
2. Открыть `http://127.0.0.1:8089/` в браузере

## Features

- Hero: $-saved, productivity multiplier, active time
- 5h-budget remaining (real-time, every 5s)
- Stress meter (Phase 1.4 sentiment)
- Activity timeline (60 days, daily aggregates)
- Models breakdown (donut)
- Recent sessions (last 10)

## Tech

- Vanilla HTML + CSS + JS (single file, no build)
- Polling fetch() — fast (5s) for budget/health, medium (30s) for stats, slow (60s) for sessions/timeseries
- Cyberpunk theme: cyan #06b6d4 + violet/magenta accents + JetBrains Mono + grid pattern + glitch on hero numbers

## Phase 4 (отдельно)

Public-share snapshot на androman.pro — обезличенные числа, без working_dir / session_id.
```

## Constraints

- Vanilla HTML/CSS/JS (no React/Vue/Tailwind/CDN)
- Self-contained single file (CSS+JS inline) для `dashboard/index.html`
- Polling intervals: 5s/30s/60s (configurable JS const'ами)
- Path traversal protection в static handler
- UTF-8 encoding везде
- Стdlib only для backend changes
- Никаких external HTTP calls (всё через `fetch('/api/...')` to same origin)

## Acceptance criteria

- [ ] `dashboard/index.html` exists, valid HTML5
- [ ] Inline CSS — киберпанк theme (см. style guide)
- [ ] Inline JS — polling logic для всех 7 endpoint'ов
- [ ] Hero row с 3 big-number widgets
- [ ] 5h-budget bar с цветовой динамикой
- [ ] Stress meter, timeline chart, models breakdown, sessions list
- [ ] Glitch animation на hero numbers
- [ ] Backend serve `/` → `dashboard/index.html`
- [ ] Path traversal protection в static handler
- [ ] `dashboard/README.md` exists с usage

## Test plan

**Codex responsibilities** (sandbox, no network):
- [ ] py_compile `backend/server.py` (после static handler)
- [ ] HTML5 validity (basic — open/close tags balanced, valid attributes)
- [ ] JS syntax check через `node --check` (если node доступен) или regex для basic syntax
- [ ] CSS basic checks (no unclosed braces)
- [ ] Static handler unit-test с fixture (mock self.wfile, проверить content-type для .html/.css/.js)
- [ ] Path traversal test: запрос `/dashboard/../etc/passwd` → 404 (not 200 with file content)

**Architect responsibilities** (host, after merge):
- [ ] Запуск `py -3.14 backend/server.py --port 8089` на host
- [ ] Открыть `http://127.0.0.1:8089/` в браузере
- [ ] Visual review: hero numbers видны с glow, glitch animation работает, layout responsive
- [ ] Polling работает (DevTools Network tab — periodic /api/* requests)
- [ ] Все widgets показывают real data (не undefined / NaN)

## Out of scope

- WebSocket / SSE для real-time push (polling достаточно для MVP)
- Service Worker / offline mode
- Multi-tenant (single-user local)
- Phase 4 public-share (отдельная задача, anonymization будет позже)
- Advanced charts (D3.js etc.) — vanilla SVG достаточно

## Final report

Conform to `--output-schema`. Required: `files_created`, `summary`, `tested`, `test_results`, `open_questions`, `deviations_from_spec`.
