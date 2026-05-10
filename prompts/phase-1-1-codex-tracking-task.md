# Task: Phase 1.1 — Codex CLI tracking layer

Name: codex-developer (orchestrator session)
Profile: Codex CLI 0.128+, run interactively from user's terminal (НЕ из Claude bash subprocess)
Goal: Реализовать tracking каждого `codex exec` invocation — analog к `hooks/claude-track-calls.py`. Каждый run пишет JSONL event в `tracker/codex-events.jsonl` с usage stats, cost, model, session_id, working_dir.
Constraints: workspace-write, не ломать существующий Claude tracking (`tracker/claude-events.jsonl`), не вводить deps кроме stdlib
Watches: `tracker/codex-events.jsonl` (новый), `bin/codex-track.cmd` или `tracker/codex-track.py` (wrapper)
Produces: tracking infrastructure готовая к интеграции в backend (Phase 2 уже умеет читать events)

## Operational backstory

Architect (Claude) построил Phase 1.0 (Claude tracking via Stop hook), Phase 2 (aggregator backend), Phase 3.5 (snapshot pipeline). Codex (ты сейчас) build'ил их по spec'ам. Snapshot/dashboard сейчас показывает **только Claude** данные — Codex calls невидимы.

**Phase 1.1 цель**: Сделать Codex calls видимыми в общем dashboard. Wow-метрика для Phase 4 publish — «AI экономит мне N× через **обе** подписки» (Claude Max + ChatGPT Pro).

Ты сейчас работаешь в Codex CLI напрямую (user в твоём терминале). Когда упрёшься в архитектурный выбор — escalate to Claude (helper architect): user скопирует вопрос в Claude и принесёт ответ.

## Working directory

`F:/WorkAI/multi-agent/`

## Архитектурные параметры (от architect — НЕ менять без escalation)

### Event schema — JSONL

Каждый Codex run = одна строка в `tracker/codex-events.jsonl`:

```json
{
  "ts": "2026-05-10T12:34:56+03:00",
  "session_id": "<codex session UUID>",
  "model": "gpt-5.5",
  "input_tokens": 12345,
  "cached_input_tokens": 6789,
  "output_tokens": 2345,
  "reasoning_tokens": 5000,
  "total_tokens": 22479,
  "duration_ms": 45678,
  "cost_estimate_usd": 0.087,
  "exit_code": 0,
  "working_dir": "F:/WorkAI/multi-agent",
  "subscription_type": "chatgpt-pro",
  "approval_mode": "never",
  "sandbox_mode": "workspace-write"
}
```

**Schema notes:**
- `subscription_type`: `chatgpt-pro` если Codex auth=chatgpt (default для нас); `api-key` если ANTHROPIC_API_KEY/OPENAI_API_KEY
- `cost_estimate_usd`: для ChatGPT-подписки фактически `0` (paid через подписку), но фиксируем «по API rate» для wow-метрики
- Поля `*_tokens` — из codex JSON output, fallback `0` если не пришло

### Pricing — для cost_estimate_usd

OpenAI gpt-5.5 (placeholder rates на 2026-05; обновим когда официальные):
- Input: ~$10 / 1M tokens
- Cached input: ~$2.5 / 1M tokens
- Output: ~$30 / 1M tokens
- Reasoning: ~$30 / 1M tokens (счётчик отдельный)

Формула:
```python
cost = (
    input_tokens * 10 / 1_000_000
    + cached_input_tokens * 2.5 / 1_000_000
    + output_tokens * 30 / 1_000_000
    + reasoning_tokens * 30 / 1_000_000
)
```

### Wrapper, не hook

Codex CLI **на момент 0.128 не имеет hooks** (как у Claude Code). Поэтому tracking через **wrapper script**:

`tracker/codex-track.py` — Python wrapper:
1. Принимает `argv[1:]` (всё что user передал бы в `codex`)
2. Forward'ит args в real `codex.cmd` через subprocess
3. Capture'ит stdout (JSON если `--json` флаг был передан)
4. Parse'ит stats из JSON output
5. Append'ит event в `tracker/codex-events.jsonl` атомарно (через `tracker/.codex-events.lock` файл-лок)
6. Возвращает exit code real codex

User then aliases (либо в bashrc/profile):
```bash
alias codex='py -3.14 F:/WorkAI/multi-agent/tracker/codex-track.py'
```

или вызывает напрямую `py tracker/codex-track.py exec ...` для ручных runs.

### Idempotency / dedup

Используй **session_id** из codex JSON output как primary key. Если event с тем же session_id уже есть — append (codex может писать события incrementally? уточни у Claude если непонятно).

### Тест на сессию

После реализации:
1. Прогон `py tracker/codex-track.py exec --sandbox read-only --skip-git-repo-check "Echo hello world as JSON: {\"hi\":1}"`
2. Проверка `tracker/codex-events.jsonl` — должна появиться строка с реальными tokens/cost
3. Прогон `tracker/summary.py` — должен подцепить codex events (если поправишь summary, см. ниже)

## Deliverables

### 1. `tracker/codex-track.py` (новый)

Wrapper script, ~200 LOC. Структура:

```python
#!/usr/bin/env python
"""Codex CLI tracking wrapper — Phase 1.1.

Wraps `codex` invocations, capturing usage stats from --json output and
appending JSONL events to tracker/codex-events.jsonl. Forwards stdin/stdout/stderr
and returns the same exit code as the wrapped codex process.

Usage: py codex-track.py <codex args>
Example: py codex-track.py exec --sandbox read-only "prompt"
"""
import argparse, json, os, subprocess, sys, time, shutil
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVENTS_FILE = PROJECT_ROOT / "tracker" / "codex-events.jsonl"
LOCK_FILE = PROJECT_ROOT / "tracker" / ".codex-events.lock"
PRICING = {
    "input": 10.0 / 1_000_000,
    "cached_input": 2.5 / 1_000_000,
    "output": 30.0 / 1_000_000,
    "reasoning": 30.0 / 1_000_000,
}

def main(argv):
    codex_cmd = shutil.which("codex")
    if not codex_cmd:
        sys.stderr.write("codex CLI not found in PATH\n")
        return 127

    # Force --json so we can parse usage stats. If user already passed --json
    # leave it; otherwise inject for `exec` subcommand.
    args = list(argv)
    if args and args[0] == "exec" and "--json" not in args:
        args.insert(1, "--json")

    start_ts = time.time()
    cwd = os.getcwd()

    # Run codex, capture stdout while echoing to user
    proc = subprocess.Popen(
        [codex_cmd] + args,
        stdout=subprocess.PIPE, stderr=sys.stderr,
        text=True, encoding="utf-8", errors="replace",
    )
    captured = []
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        captured.append(line)
    proc.wait()
    duration_ms = int((time.time() - start_ts) * 1000)

    # Parse JSON-stream output for tokens / session_id
    event = build_event_from_codex_jsonl(
        "".join(captured), proc.returncode, duration_ms, cwd, args,
    )
    if event is not None:
        append_jsonl_atomic(EVENTS_FILE, event)

    return proc.returncode

# ... build_event_from_codex_jsonl, append_jsonl_atomic, lock helpers
```

Важно:
- Stream-parse JSON Lines из codex `--json` output (не batch parse!)
- Session ID extract'ить из первого event'а с `session_id` ключом
- Tokens — из последнего event с `usage` объектом (codex агрегирует в конце)
- Lock через `O_CREAT|O_EXCL` (проверь как `claude-track-calls.py` делает в `hooks/`)

**ESCALATE TO CLAUDE (architect):** Codex JSON output schema может отличаться от Claude'а. Если запутаешься в формате полей — попроси user'а скопировать из транскрипта примеры real `codex exec --json` output, я подскажу маппинг.

### 2. `tracker/codex-track.cmd` (опц.)

Тонкая cmd-обёртка для удобства:
```cmd
@echo off
"C:\Windows\py.exe" -3.14 "F:\WorkAI\multi-agent\tracker\codex-track.py" %*
```

User может положить в PATH или использовать как `F:\WorkAI\multi-agent\tracker\codex-track.cmd exec ...`.

### 3. `tracker/summary.py` — расширить чтение Codex events

Сейчас `read_events()` читает только `claude-events.jsonl`. Добавить:
- `read_codex_events(start, end)` — analog для codex
- `summarize_codex_by_model(events)` — agg per-model
- В `summarize_by_model` объединять Claude + Codex с тегом `provider`

**ESCALATE TO CLAUDE:** Какой формат лучше — добавить ключ `provider: "anthropic"|"openai"` в существующие events (миграция) или держать раздельные файлы и аггрегировать на уровне summary? Architect: спроси Claude'а перед миграцией existing JSONL, иначе ломаешь Phase 2.

### 4. `backend/server.py` — расширить snapshot

В `build_wp_snapshot()` добавить блок:

```python
"providers": {
    "anthropic_claude": { "calls": ..., "cost_usd": ..., "models": [...] },
    "openai_codex": { "calls": ..., "cost_usd": ..., "models": [...] },
},
"totals": { ..., "cost_usd_combined": claude_cost + codex_cost, ... },
```

WP-page integration в Phase 1.1 — **out of scope**. Достаточно чтобы snapshot имел эти поля, WP-rendering — отдельный Phase 1.1.5 task.

### 5. README обновить

В `README.md` Phase 1.1 status → done, описать wrapper + alias instructions.

## Constraints

- Только stdlib (`subprocess`, `json`, `pathlib`, `os`, `time`, `datetime`, `shutil`)
- НЕ ломать `tracker/claude-events.jsonl` чтение/запись
- НЕ ломать `tracker/tasks.json` (используется estimator'ом + summary'ёй)
- НЕ запускать `pip install` (sandbox blocks)
- НЕ вводить новые dependencies
- UTF-8 везде, без BOM

## Acceptance criteria

- [ ] `py tracker/codex-track.py exec --sandbox read-only "test prompt"` → stdout совпадает с native codex, plus JSONL event добавился
- [ ] Event имеет все поля schema выше (особенно `cost_estimate_usd > 0`)
- [ ] `tracker/summary.py` показывает Codex events отдельно или merged (по выбору architect, см. ESCALATE)
- [ ] PHP syntax check / Python syntax check pass
- [ ] README обновлён

## Test plan

**Codex (sandbox):**
- [ ] Python syntax: `python -c "import ast; ast.parse(open('tracker/codex-track.py').read())"`
- [ ] Smoke: `py tracker/codex-track.py exec --sandbox read-only "Output JSON {\"ok\":1}"`
- [ ] Проверить event записан в `tracker/codex-events.jsonl`
- [ ] Прогон `py tracker/summary.py` — не ломается

**Architect (host):**
- Architect ревью diff
- Smoke в реальной среде
- Snapshot regen + проверка `providers` блока

## ESCALATE TO CLAUDE — точки эскалации

Когда упрёшься в эти вопросы — попроси user'а скопировать в Claude:

1. **Codex `--json` output schema** — какие именно поля `usage`, как названы (input/output/cached/reasoning)? Architect не знает точный shape; user может прислать sample.
2. **Provider tagging миграция** — добавить `provider` ключ в существующие events (требует backfill) или держать файлы раздельно?
3. **Wrapper alias или explicit invocation** — какой UX лучше для user'а? Alias скрытый (опасно если потеряется), explicit (надёжнее, но user должен помнить путь).
4. **Cost для ChatGPT-подписки** — фиксируем `$0` (subscription pays) или `cost_estimate_usd` по API rates (для wow-метрики)? Architect default — оба, в разных полях. Скоординируй с user'ом.
5. **Backend snapshot fields** — `providers` объект vs flat fields? Phase 4 WP-rendering зависит от этого.

## Final report

Conform к стандарту: `files_created`, `summary`, `tested` (true/false), `test_results`, `open_questions`, `deviations_from_spec`.

Если поднял вопрос к Claude'у через user'а — фиксируй в `open_questions` чтобы architect видел что было решено вне spec'а.
