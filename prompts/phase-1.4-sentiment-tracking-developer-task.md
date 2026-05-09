# Task: Phase 1.4 — Sentiment tracking (profanity + frustration + appreciation)

Name: codex-developer
Profile: Codex CLI 0.128+ (gpt-5.5, xhigh reasoning, --sandbox workspace-write)
Goal: Add sentiment tracking (regex profanity counter + ИИ frustration/appreciation scores) to existing complexity estimation pipeline. Один subprocess = и complexity и sentiment.
Constraints: stdlib only, UTF-8, atomic write, **counter-only storage** (no raw examples for privacy)
Watches: issue #13 + existing `tracker/estimate-task.py`, `tracker/summary.py`, `tracker/oracle-prompt.txt`, `tracker/note-task.py`
Produces: 5 modified files (no new files)

## Operational backstory

You are running with `workspace-write` sandbox in `F:/WorkAI/multi-agent`. Phase 1.0/1.3/1.0.1/1.0.2/1.0.3 уже в main. Phase 1.4 расширяет существующий sentiment pipeline без новых файлов.

**Privacy критично**: храним только counters (`profanity_count: 7`), НЕ raw примеры матов. При публикации на блог (Phase 4) — только aggregated numbers.

**Sandbox limitation** (Phase 1.0.2 lesson): тесты pytest/etc запускает architect на host, не Codex inside sandbox. Codex делает только static checks (py_compile + manual fixture tests без external deps).

## Working directory

`F:/WorkAI/multi-agent` (already your `--cd`).

## Project context

Read `CLAUDE.md`, `README.md`. Phase 1.3 уже реализовала estimate-task.py с oracle prompt → этот task расширяет тот же pipeline.

## Goal

Третья ось метрик (после $-saved Phase 1.0 и time-saved Phase 1.3) — **stress / sentiment** per session. Дашборд (Phase 3) будет показывать stress meter, frustration trend, profanity counter — wow-метрика для блог-витрины «сколько раз я матерился на ИИ».

## Deliverables (изменения в existing файлах, **без новых файлов**)

### 1. `tracker/oracle-prompt.txt`

Extend output schema с новыми sentiment полями. Текущая schema:

```json
{
  "brief_description": "...",
  "ai_baseline_hours": 0.0,
  "estimation_confidence": "high|medium|low",
  "needs_manual_review": false
}
```

Расширенная:

```json
{
  "brief_description": "...",
  "ai_baseline_hours": 0.0,
  "estimation_confidence": "high|medium|low",
  "needs_manual_review": false,
  "frustration_score": 0.0,
  "appreciation_score": 0.0,
  "mood_arc": "calm|frustrated→calm|stable→joyful|exhausted|mixed",
  "sentiment_intensity": "low|medium|high"
}
```

Добавить guidance в prompt:

```
Sentiment guidance (analyze user messages tone):
- frustration_score: 0-1, scale based on signs of impatience, capslock, swearing, repeated demands
- appreciation_score: 0-1, scale based on signs of thanks, acknowledgment of progress, positive emoji
- mood_arc: brief arc describing emotional trajectory (max 30 chars)
- sentiment_intensity: how strongly the emotional signal is present overall

Don't quote specific user phrases. Output scores only.
```

### 2. `tracker/estimate-task.py`

**A. Добавить regex profanity counter:**

```python
import re

PROFANITY_RU_PATTERNS = [
    re.compile(r"\bбля[а-яё]*", re.IGNORECASE),
    re.compile(r"\bёб[а-яё]*", re.IGNORECASE),
    re.compile(r"\bеб[а-яё]+", re.IGNORECASE),
    re.compile(r"\bхуй[а-яё]*|\bхрен[а-яё]*|\bхер[а-яё]*", re.IGNORECASE),
    re.compile(r"\bпизд[а-яё]*", re.IGNORECASE),
    re.compile(r"\bсук[а-яё]*", re.IGNORECASE),
    re.compile(r"\bговн[а-яё]*", re.IGNORECASE),
    re.compile(r"\bжоп[а-яё]*", re.IGNORECASE),
    re.compile(r"\bнах(уй|ер|рен)[а-яё]*", re.IGNORECASE),
    re.compile(r"\bпошёл\s+на\b|\bпошел\s+на\b", re.IGNORECASE),
]

PROFANITY_EN_PATTERNS = [
    re.compile(r"\bfuck[a-z]*", re.IGNORECASE),
    re.compile(r"\bshit[a-z]*", re.IGNORECASE),
    re.compile(r"\bdamn[a-z]*", re.IGNORECASE),
    re.compile(r"\bbitch[a-z]*", re.IGNORECASE),
    re.compile(r"\bcrap[a-z]*", re.IGNORECASE),
]

ALL_PROFANITY = PROFANITY_RU_PATTERNS + PROFANITY_EN_PATTERNS

def count_profanity(user_messages: list[str]) -> int:
    """Count profanity matches across user messages (counter only, не storing raw)."""
    total = 0
    for msg in user_messages:
        for pattern in ALL_PROFANITY:
            total += len(pattern.findall(msg))
    return total
```

**B. Extend `normalize_oracle_payload`:**

Добавить парсинг новых полей с defaults:

```python
def normalize_oracle_payload(payload: dict) -> dict:
    # ... existing parsing for description, hours, confidence, review_flag ...
    
    # Sentiment fields (Phase 1.4)
    try:
        frustration = float(payload.get("frustration_score", 0))
    except (TypeError, ValueError):
        frustration = 0.0
    frustration = max(0.0, min(1.0, frustration))
    
    try:
        appreciation = float(payload.get("appreciation_score", 0))
    except (TypeError, ValueError):
        appreciation = 0.0
    appreciation = max(0.0, min(1.0, appreciation))
    
    mood_arc = payload.get("mood_arc", "")
    if not isinstance(mood_arc, str):
        mood_arc = ""
    mood_arc = mood_arc[:30]  # cap length
    
    intensity = payload.get("sentiment_intensity", "low")
    if intensity not in {"low", "medium", "high"}:
        intensity = "low"
    
    return {
        # ... existing fields ...
        "frustration_score": frustration,
        "appreciation_score": appreciation,
        "mood_arc": mood_arc,
        "sentiment_intensity": intensity,
    }
```

**C. Объединить regex и oracle results в `estimate_session`:**

```python
def estimate_session(session_id, transcript_path):
    user_messages, assistant_messages = read_transcript(Path(transcript_path))
    
    # Phase 1.4 — regex profanity count (cheap, локальный)
    profanity = count_profanity(user_messages)
    
    # Phase 1.3 — oracle for complexity + sentiment (Phase 1.4 extends prompt)
    context = build_truncated_context(...)
    prompt = oracle_prompt + "\n\n=== TRANSCRIPT (truncated) ===\n" + context
    entry = run_oracle(prompt)
    entry["transcript_path"] = transcript_path
    entry["profanity_count"] = profanity  # add regex result
    
    update_task_entry(session_id, entry)
```

### 3. `tracker/summary.py` — Sentiment block

После Productivity (Phase 1.3) блока, если в period есть tasks с sentiment data:

```markdown
## Sentiment (Phase 1.4)

**Profanity total**: 47 across 12 sessions (avg 3.9/session)
**Frustration avg**: 0.34 (medium-low)
**Appreciation avg**: 0.62 (medium-high)
**Stress trend**: ↘ improving (если последние сессии с lower frustration)
**Top day**: 2026-04-22 (12 mat'ов в 3 сессиях)
**Mood arcs (top-3)**: 'frustrated→calm' (5), 'stable' (3), 'calm→frustrated' (2)

Sessions covered: 9 of 248 (239 pending sentiment estimation)
```

Logic:
- Aggregate per-period: avg frustration_score, avg appreciation_score, total profanity
- Stress trend: comparison первая половина period vs вторая половина (по средним frustration)
- Top day: by total profanity_count
- Mood arcs: count occurrences of each mood_arc string

Skip Sentiment block if no tasks с sentiment data в period.

### 4. `tracker/note-task.py` — extend `--list`

Добавить колонки в markdown table:

```
| Session ID | Description | AI baseline (h) | Profanity | Mood |
```

(Hours columns можно сократить, sentiment важнее для review)

### 5. `tracker/README.md` — обновить tasks.json schema docs

Добавить новые поля в schema-section:

```markdown
### tasks.json fields (Phase 1.4 extended)

- `profanity_count` — int, regex match count of swear words in user messages
- `frustration_score` — float 0-1, ИИ assessment
- `appreciation_score` — float 0-1, ИИ assessment
- `mood_arc` — string (max 30 chars), brief emotional trajectory
- `sentiment_intensity` — "low"|"medium"|"high"
```

## Constraints

- **Privacy**: storing counters and scores ONLY, NO raw quotes from user messages. ИИ prompt explicitly says «Don't quote specific user phrases»
- Performance: profanity regex compile один раз module-level, scan O(n) на user messages
- Robustness: missing/malformed sentiment fields → defaults (0.0 / "" / "low")
- UTF-8 encoding везде
- Stdlib only (re, json, datetime, pathlib)

## Acceptance criteria

- [ ] `oracle-prompt.txt` extended с 4 новыми полями + guidance + «no quotes» rule
- [ ] `estimate-task.py` имеет `count_profanity()` + extended normalize + integration
- [ ] Profanity counter правильный на fixture (несколько вариантов матов)
- [ ] `summary.py` показывает Sentiment блок (если есть данные)
- [ ] `summary.py` skip'ает Sentiment блок если нет данных
- [ ] `note-task.py --list` включает sentiment columns
- [ ] `README.md` schema-section обновлён
- [ ] Static check: py_compile всех изменённых файлов

## Test plan

**Codex responsibilities** (sandbox, no network):
- [ ] py_compile все изменённые .py файлы
- [ ] Profanity regex unit-test на fixture: input = list of test user messages, expected count
  - Example fixture: ["блядь, опять упало", "fuck this shit", "обычное сообщение"] → expected 3 (1 рус, 2 англ)
- [ ] normalize_oracle_payload unit-test с extended payload (включая edge cases: missing fields, invalid types)
- [ ] Summary fixture: tasks.json с sentiment data → Sentiment блок появляется; без — пропускается

**Architect responsibilities** (host, after merge):
- Real-data test через summary.py --days 60 на live JSONL после следующих SessionStart fires (когда оценки накопятся естественно)

**НЕ запускать `claude -p --bare` во время Codex run** — это Phase 1.3 production trigger, не unit test.

## Out of scope

- Real-time dashboard (Phase 3)
- Public anonymization (Phase 4)
- Multi-language profanity (только RU + EN, остальные — позже)
- Sentiment trends visualization (Phase 3)

## Final report

Conform to `--output-schema` (`F:/WorkAI/multi-agent/schemas/phase-1.0-developer-output.json`). Required: `files_created`, `summary`, `tested`, `test_results`, `open_questions`, `deviations_from_spec`.

`files_created` должен включать **5 modified файлов** (нет новых файлов в этом phase).
