# Deployment

How to run NovelPilot: locally, as a service, and at scale.

---

## Local

### Requirements

- Python 3.11+
- An API key (or a local model server)
- `libcairo2` for PNG cover rendering — optional; the cover generator writes
  SVG if it is missing

```bash
sudo apt-get install -y libcairo2      # Debian/Ubuntu
brew install cairo                      # macOS
```

### Install

```bash
git clone https://github.com/Amrree/novelpilot.git
cd novelpilot
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[server,dev]"
```

### Configure

```bash
export OPENAI_API_KEY=sk-...
# or
export ANTHROPIC_API_KEY=sk-ant-...
```

Check it:

```bash
novelpilot doctor
```

`doctor` reports which providers have credentials, which model each role
resolves to, and which plugins are discovered.

### Run

```bash
novelpilot new "The Salt Road" --genre fantasy --premise "..."
novelpilot plan the-salt-road
novelpilot write the-salt-road
novelpilot export the-salt-road --author "Jane Doe"
```

---

## Local models (no API key, no per-token cost)

The single most useful deployment for development and for cost-sensitive
batch work.

### Ollama

```bash
ollama pull llama3.1:70b
export NOVELPILOT_PROVIDER=ollama
export NOVELPILOT_BASE_URL=http://localhost:11434/v1
export NOVELPILOT_MODEL_PLAN=llama3.1:70b
export NOVELPILOT_MODEL_PROSE=llama3.1:70b
export NOVELPILOT_MODEL_SUMMARY=qwen2.5:14b
export NOVELPILOT_MODEL_JUDGE=qwen2.5:14b
```

Ollama ignores the API key, so `OPENAI_API_KEY` is not needed.

### LM Studio

```bash
export NOVELPILOT_PROVIDER=lmstudio
export NOVELPILOT_BASE_URL=http://localhost:1234/v1
```

### vLLM

```bash
vllm serve meta-llama/Llama-3.1-70B-Instruct --port 8000
export NOVELPILOT_PROVIDER=vllm
export NOVELPILOT_BASE_URL=http://localhost:8000/v1
```

### A note on local models

They are free per token, but not free per book:

- A 70B model on a consumer GPU generates prose slowly. A chapter can take
  several minutes.
- Smaller models (7B–14B) produce flatter prose and violate the style
  directives more often. Expect to raise `words_per_chapter` and lower
  expectations.

Local inference is excellent for development, for evaluating whether the
*architecture* works, and for books where privacy matters. It is less
competitive on raw prose quality.

---

## Server

### Docker

```dockerfile
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        libcairo2 build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .
RUN pip install --no-cache-dir ".[server]"

ENV NOVELPILOT_DB_PATH=/data/novelpilot.db
ENV NOVELPILOT_DATA_DIR=/data
VOLUME ["/data"]

EXPOSE 8000
CMD ["uvicorn", "novelpilot.server.app:app", "--host", "0.0.0.0", "--port", "8000"]
```

```bash
docker build -t novelpilot .
docker run -p 8000:8000 -v novelpilot-data:/data \
    -e OPENAI_API_KEY=sk-... novelpilot
```

**Mount `/data` as a volume.** Without it, every container restart loses the
Story Bible — which means the book cannot be resumed and the cost ledger
resets.

### Bare metal

```bash
pip install ".[server]"
export NOVELPILOT_DB_PATH=/var/lib/novelpilot/novelpilot.db
uvicorn novelpilot.server.app:app --host 0.0.0.0 --port 8000 --workers 2
```

**On workers:** each worker process opens its own SQLite connection. SQLite in
WAL mode handles concurrent readers and a single writer well, but generation
is write-heavy. With `--workers 2` you can get `database is locked` under
load. Either use `--workers 1` with an in-process async worker (fine for
moderate volume), or move to Postgres before scaling out. See below.

---

## Scaling

### The current shape

The API server runs generation **in-process**, in a background task, and
reports progress over SSE. The job's state lives in the same SQLite database
as the books.

This is the right shape up to a few hundred concurrent generations, and it
requires no Redis, no broker and no separate worker fleet.

### When to move to a queue

Move when you hit one of these:

- Generation regularly takes longer than a request timeout.
- You want to survive a server restart mid-book.
- You need to scale generation independently of the API.
- Multiple API instances write to the same database.

The upgrade path:

1. Introduce a `jobs` table (`id`, `project_id`, `type`, `status`,
   `attempts`, `payload_json`, `created_at`, `started_at`, `finished_at`).
2. The API enqueues a job row and returns `202` with the job id.
3. A worker loop (`async while True: claim next pending job → run → mark done`)
   processes them. Claiming uses `UPDATE ... WHERE status='pending' LIMIT 1`
   inside a transaction, which is safe with SQLite's write lock.
4. `GET /projects/{slug}/stream` polls the job row and emits SSE.

### Moving to Postgres

The schema is portable. The changes needed:

- `sqlite3` → `asyncpg` or `psycopg`.
- `PRAGMA journal_mode=WAL` → nothing (Postgres has its own MVCC).
- `sqlite3.Row` → `psycopg` row factory.
- `?` placeholders → `%s`.
- `ON CONFLICT ... DO UPDATE` → same syntax, works in Postgres.
- The `costs` table gains a `created_at` index for daily-budget queries.

Nothing in `core/` needs to change beyond `BibleStore` — which is deliberate.
That file is the only module that knows about storage.

---

## Operational notes

### Secrets

Environment variables only. The config file names the variables, never their
values. In practice:

```bash
export OPENAI_API_KEY=sk-...
export ANTHROPIC_API_KEY=sk-ant-...
```

For Kubernetes, use a Secret mounted as environment variables. For systemd, use
`EnvironmentFile=`.

### Backups

The SQLite file is the whole book. Back it up:

```bash
sqlite3 /var/lib/novelpilot/novelpilot.db ".backup '/backups/np-$(date +%F).db'"
```

The `costs` table is also your accounting record — back it up with the same
schedule.

### Health checks

```bash
curl -s http://localhost:8000/health | jq
```

Returns provider availability and the plugin list. Note that it reports
*credential* availability, not live reachability — `providers.available_providers()`
checks for the presence of an environment variable.

### Logging

The package uses `logging` with the `novelpilot` logger. It emits warnings on
provider retries and errors on progress-callback failures.

```python
import logging
logging.getLogger("novelpilot").setLevel(logging.DEBUG)
```

Do not log at DEBUG in production: prompts contain story content.

---

## Performance notes

| Operation | Time |
|---|---|
| Plan a 32-chapter book | ~1–3 min (1–2 model calls per batch) |
| Write one chapter | ~30–90 s (3 scenes + 1 sync) |
| Write a full 80k-word book | ~30–60 min |
| Verify a chapter | ~3–8 s |
| Export | < 1 s |

The dominant cost is generation. Everything else is fast enough not to
optimise yet.
