# Task: Implement `--public` privacy-hardening mode for snapshot writer

Name: codex-developer
Profile: Codex CLI 0.128+ (gpt-5.5, xhigh reasoning, --sandbox workspace-write --skip-git-repo-check)
Goal: Добавить privacy-hardening mode `--public` к существующему snapshot writer'у в `backend/server.py`. Когда флаг включён — `session_id` хешируется с локальным salt, текстовые поля (`desc`, `top_session`) проходят scrubbing для удаления абсолютных путей, email-адресов и имён клиентов. Без флага (default) — поведение не меняется (для LAN-снимков).
Constraints: workspace-write only `backend/server.py` и `README.md`; не трогать tracker/, hooks/, dashboard/; не вводить новые dependencies (только stdlib); тесты архитектор запускает сам, не запускай pip install
Watches: `backend/server.py` (~750 LOC, есть `build_wp_snapshot`, `write_snapshot`, `snapshot_loop`, `_sanitize_desc`, `_models_with_pct`)
Produces: modified `backend/server.py` + раздел в `README.md`

## Operational backstory

Phase 3.5 (snapshot pipeline) уже работает: backend пишет JSON в `<wp_uploads>/multi-agent/snapshot.json` каждые 15 мин, WP-страница на NAS:8080 фетчит. Сейчас snapshot живёт **только в LAN**, всё OK.

Phase 4 (будет позже) — публикация на `<your-blog>.example` production. Тогда тот же JSON станет публично доступен. Codex review (issue D1) предупредил: `session_id_short` (первые 8 hex от UUID) и `desc` (brief_description из tasks.json) в публичном виде = **privacy leak**:

- `session_id_short` коррелирует временные ряды активности (когда пользователь работал/отдыхал)
- `desc` может содержать абсолютные пути (`<workspace>/...`), email'ы, имена клиентов («<client>», «<client>»)
- `top_session` (today panel) — то же самое

Архитектор хочет реализовать `--public` режим **сейчас**, чтобы не забыть. Default остаётся small-scope (LAN), но при флаге включается hardening.

## Working directory

`<project_root>/` (--cd при запуске).

## Deliverables

### 1. CLI args в `parse_args()`

Добавить три новых аргумента:

```python
parser.add_argument(
    "--public",
    action="store_true",
    help="Enable privacy hardening: hash session_id, scrub paths/emails/customer names. "
         "Use for snapshots intended for public publishing.",
)
parser.add_argument(
    "--salt-file",
    default=str(Path.home() / ".multi-agent-snapshot-salt"),
    help="Path to file with salt for session_id hashing. Auto-generated (32 random bytes) "
         "if missing. Required when --public is set.",
)
parser.add_argument(
    "--customers-blocklist",
    default=None,
    help="Optional path to file listing customer names to scrub from desc/top_session "
         "(one name per line, # comments allowed). Only used with --public.",
)
```

### 2. Salt management

Добавить helper:

```python
import secrets

def load_or_create_salt(path):
    """Read salt from file, generating 32 random bytes if file doesn't exist.
    Returns bytes. Atomic create (mkstemp + replace) to avoid race with concurrent writers.
    Permissions: 0600 on POSIX; on Windows write-only via Path.write_bytes.
    """
    p = Path(path)
    if p.exists():
        data = p.read_bytes().strip()
        if len(data) >= 16:
            return data
        # Too short — regenerate
    p.parent.mkdir(parents=True, exist_ok=True)
    salt = secrets.token_bytes(32)
    # Atomic create
    tmp = p.with_suffix(p.suffix + f".tmp.{os.getpid()}")
    tmp.write_bytes(salt)
    if hasattr(os, "chmod"):
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    os.replace(tmp, p)
    return salt
```

### 3. Hashing function

```python
import hashlib

def _hash_session_id(uuid_str, salt):
    """Stable 8-hex-char hash of session_id with local salt.
    Same uuid → same hash for the same salt (lets timeline correlation within
    one snapshot's session list). Different salts = unlinkable hashes."""
    if not isinstance(uuid_str, str) or not uuid_str:
        return ""
    h = hashlib.blake2b(salt + uuid_str.encode("utf-8"), digest_size=4)
    return h.hexdigest()
```

### 4. Scrubbing function

```python
import re

# Compiled patterns reused across calls.
_PATTERN_PATH_WIN = re.compile(r'(?<![\w/])[A-Za-z]:[\\/][\w\\/.\-+~]*', re.IGNORECASE)
_PATTERN_PATH_UNIX = re.compile(r'(?<![\w/])(?:~|/(?:home|usr|opt|var|etc)(?:/[\w\-+./]*)?)', re.IGNORECASE)
_PATTERN_EMAIL = re.compile(r'\b[\w.+\-]+@[\w\-]+\.[\w\-.]+\b')
_PATTERN_TOKEN = re.compile(r'\b(sk_|pk_|ghp_|gho_|github_pat_)\w{16,}\b')


def _build_customer_pattern(blocklist_path):
    """Read customer names (one per line, # comments OK), return compiled regex
    or None if file missing/empty. Match is case-insensitive, word-boundary aware
    (Cyrillic-friendly via \\b alternative)."""
    if not blocklist_path:
        return None
    try:
        text = Path(blocklist_path).read_text(encoding="utf-8")
    except OSError:
        return None
    names = [
        line.strip() for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not names:
        return None
    # Sort by length desc so longer matches win when overlap
    names.sort(key=len, reverse=True)
    escaped = [re.escape(n) for n in names]
    # \b doesn't fire on Cyrillic — use lookarounds for non-word boundaries
    return re.compile(
        r'(?<![\wЀ-ӿ])(?:' + '|'.join(escaped) + r')(?![\wЀ-ӿ])',
        re.IGNORECASE,
    )


def _scrub_for_public(text, customer_pattern=None):
    """Strip absolute paths, emails, secret tokens, customer names. Idempotent."""
    if not isinstance(text, str) or not text:
        return ""
    text = _PATTERN_PATH_WIN.sub('[path]', text)
    text = _PATTERN_PATH_UNIX.sub('[path]', text)
    text = _PATTERN_EMAIL.sub('[email]', text)
    text = _PATTERN_TOKEN.sub('[token]', text)
    if customer_pattern is not None:
        text = customer_pattern.sub('[client]', text)
    return text
```

### 5. Wire into build_wp_snapshot

`build_wp_snapshot` сейчас принимает `days, sessions_limit`. Добавить `public_mode=False, salt=None, customer_pattern=None`. Применить:

```python
def build_wp_snapshot(days=SNAPSHOT_DEFAULT_DAYS, sessions_limit=SNAPSHOT_DEFAULT_SESSIONS,
                     public_mode=False, salt=None, customer_pattern=None):
    # ... existing code ...

    # Sessions section
    sessions_compact = []
    for sess in sessions_data["sessions"]:
        task = sess.get("task") or {}
        sid = sess.get("session_id") or ""
        if public_mode:
            sid_short = _hash_session_id(sid, salt) if salt else ""
            desc = _scrub_for_public(_sanitize_desc(task.get("brief_description")), customer_pattern)
        else:
            sid_short = sid[:8]
            desc = _sanitize_desc(task.get("brief_description"))
        sessions_compact.append({
            "session_id_short": sid_short,
            # ... остальное как было
            "desc": desc,
            # ... etc
        })

    # Today section: scrub top_session in public mode
    # _today_payload получает public_mode/salt/customer_pattern или scrubbing делается после
```

Аналогично для `today.top_session`.

### 6. Wire CLI args в `main()`

```python
def main():
    args = parse_args()

    salt = None
    customer_pattern = None
    if args.public:
        salt = load_or_create_salt(args.salt_file)
        customer_pattern = _build_customer_pattern(args.customers_blocklist)
        print(f"[snapshot] PUBLIC mode — salt loaded ({len(salt)} bytes), "
              f"customers blocklist: {args.customers_blocklist or 'none'}")

    if args.snapshot_once:
        if not args.snapshot_path:
            print("--snapshot-once requires --snapshot-path", file=sys.stderr)
            sys.exit(2)
        payload = build_wp_snapshot(
            days=args.snapshot_days,
            public_mode=args.public,
            salt=salt,
            customer_pattern=customer_pattern,
        )
        write_snapshot(args.snapshot_path, payload)
        print(f"[snapshot] wrote {args.snapshot_path}")
        return

    # ... background thread case: pass public_mode + salt + customer_pattern to snapshot_loop ...
```

`snapshot_loop()` тоже принимает эти параметры и передаёт в `build_wp_snapshot` каждую итерацию.

### 7. README раздел

В `<project_root>/README.md` найти раздел `Phase 3.5 — snapshot pipeline (WP ↔ backend)` и добавить подсекцию **«Privacy hardening для Phase 4»** с описанием:

- Зачем нужен `--public` (публикация на <your-blog>.example)
- Как генерится salt (auto-create в `~/.multi-agent-snapshot-salt`, 32 random bytes, 0600)
- Что scrub'ится (paths, emails, tokens, customer names)
- Пример команды с `--public` и `--customers-blocklist`
- Пример формата blocklist-файла (одно имя на строку, `#` комментарии)
- Note: snapshot file containing salt-hash — keep `~/.multi-agent-snapshot-salt` private

## Constraints

- Только stdlib (`hashlib`, `secrets`, `re`, `os`, `pathlib`)
- НЕ трогать tracker/, hooks/, dashboard/, prompts/
- НЕ ломать default (non-public) поведение — все существующие тесты/команды работают как раньше
- НЕ запускать pip install (sandbox blocks network)
- НЕ удалять existing functions, не менять их сигнатуры (только добавлять keyword args с defaults)
- Salt-файл генерится атомарно, без race с другим writer'ом
- Customer blocklist case-insensitive, Cyrillic-aware

## Acceptance criteria

- [ ] `python backend/server.py --help` показывает новые args
- [ ] `python backend/server.py --snapshot-once --snapshot-path /tmp/test.json` без `--public` работает как раньше
- [ ] `python backend/server.py --snapshot-once --snapshot-path /tmp/test_public.json --public` создаёт public snapshot:
  - `session_id_short` — 8 hex deterministic, ≠ original UUID prefix
  - `desc`/`top_session` — scrubbed (paths/emails/tokens removed)
- [ ] `~/.multi-agent-snapshot-salt` создан с 32 байтами + 0600 permission (POSIX)
- [ ] Customer blocklist пример: `echo -e "<client-a>\n<client-b>\n# comment" > /tmp/customers.txt`, `--customers-blocklist /tmp/customers.txt` → имена scrub'нуты
- [ ] README обновлён с разделом про `--public`
- [ ] Architect сможет сравнить два snapshot'а (private vs public) и убедиться в разнице

## Test plan

**Codex (sandbox)**:
- [ ] Python синтаксис: `python -c "import ast; ast.parse(open('backend/server.py').read())"`
- [ ] Smoke без `--public`: `python backend/server.py --snapshot-once --snapshot-path /tmp/private.json`
- [ ] Smoke с `--public`: `python backend/server.py --snapshot-once --snapshot-path /tmp/public.json --public`
- [ ] Diff: показать `session_id_short` и `desc` отличаются между private и public
- [ ] Customer blocklist: создать `/tmp/customers.txt` со словом «<client>», прогнать с blocklist'ом, убедиться что в public.json нет «<client>»

Если sandbox blocks `/tmp` — использовать `F:/temp/` или текущую директорию.

**Architect (host)**:
- Architect ревью diff'а в backend/server.py
- Smoke в реальной среде
- Apply real customer-blocklist (не в репо)

## Final report

Conform к стандарту: `files_created` (список изменённых), `summary` (что сделано), `tested` (true/false), `test_results` (вывод smoke'ов), `open_questions` (если что-то не уверен), `deviations_from_spec` (если отклонился).
