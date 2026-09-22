# Railway Staging Deployment — MA7.7B

This is the manual runbook for deploying the Multi-Agent AI Platform to a
Railway **staging** environment, using the architecture approved in
MA7.7A and implemented in MA7.7B. Nothing in this document has been
executed as part of MA7.7B — no Railway resources exist yet. This is the
"what to do" reference for whoever runs MA7.7B's actual deployment.

## Architecture (recap)

One Railway service, one persistent volume. The FastAPI Web process and
the `app.worker` Worker process run as sibling subprocesses of one
container, started by `scripts/hosted_entrypoint.py`. SQLite (unchanged),
on the volume. This is Option C from the MA7.7A investigation: Railway
volumes cannot be attached to more than one service, so a separate
Web-service/Worker-service split is not available without either
PostgreSQL or an object-storage migration, neither of which is in scope
here.

```
Railway service "multi-agent-ai-platform-staging"
├── Dockerfile build (python:3.11-slim)
├── one volume, mounted at $MAP_DATA_ROOT (e.g. /data)
└── scripts/hosted_entrypoint.py
    ├── alembic upgrade head (with a pre-migration backup)
    ├── app.worker (Worker)
    └── uvicorn app.main:app (Web)
```

## Prerequisites

- MA7.6 merged to `main` and CI green (this repo currently has nothing
  pushed — push and merge before attempting a Railway deploy; Railway
  deploys from the GitHub remote).
- A Railway account/project with permission to create services and
  volumes.
- A staging-only OpenRouter API key (do not reuse the local/production
  key) with a spend limit set on OpenRouter's side.

## Step 1 — Create the service

1. In the Railway project, create a new service from the
   `TSFLLC/multi-agent-ai-platform` GitHub repo, on the merged branch.
2. Railway will detect the repo's `Dockerfile` and `railway.toml`
   automatically (`railway.toml` sets `builder = "DOCKERFILE"` explicitly,
   so Railway does not fall back to Railpack — Railpack's Python builder
   does not support this repo's Python 3.8 baseline; the Dockerfile pins
   3.11 instead).

## Step 2 — Attach a volume

1. Add a volume to the service. Note its mount path (Railway assigns one,
   e.g. `/data`, or lets you choose).
2. **This mount path is permanent.** Every artifact's `storage_ref` is an
   absolute path baked in the moment it is written — changing the mount
   path later orphans every artifact written before the change.
3. If the container image runs as a non-root user and volume writes fail
   with a permissions error, set `RAILWAY_RUN_UID=0` on the service (see
   Railway's own volumes documentation).

## Step 3 — Set environment variables

Set these as Railway **service variables** (not committed anywhere — see
`.env.example` for the full reference with explanations):

| Variable | Value |
|---|---|
| `MAP_HOSTED_MODE` | `true` |
| `MAP_AUTH_TOKEN` | generate: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `MAP_SECRET_ENCRYPTION_KEY` | generate: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `MAP_DATA_ROOT` | the volume's mount path from Step 2, e.g. `/data` |
| `MAP_HOST` | `0.0.0.0` |
| `MAP_ALLOW_REMOTE_BIND` | `true` |
| `MAP_ENVIRONMENT` | `staging` |
| `MAP_LOG_TO_FILE` | `false` |
| `MAP_WORKER_CONCURRENCY` | `3` (1–4; see MA7.7A's SQLite-single-writer note before going higher) |
| `MAP_DEFAULT_AGENT_RUN_TIMEOUT_SECONDS` | `900` (lower than the 1800s local default, so a hard-killed Worker's in-flight job recovers faster) |

Save `MAP_AUTH_TOKEN` and `MAP_SECRET_ENCRYPTION_KEY` somewhere durable
outside Railway (e.g. a password manager) before moving on — Railway does
not display a secret value again after it is set, and losing
`MAP_SECRET_ENCRYPTION_KEY` makes every stored provider credential
permanently unrecoverable (by design).

## Step 4 — Deploy

Trigger the deploy. `scripts/hosted_entrypoint.py` runs automatically as
the container's `CMD`:

1. Backs up the database if one already exists (first deploy: skipped,
   there is nothing yet).
2. Runs `alembic upgrade head` and confirms the schema landed at head —
   the deploy fails loudly here if it did not, rather than starting a
   Worker/Web pair against a stale schema.
3. Starts the Worker and the Web process together. If either exits
   unexpectedly, the entrypoint stops the other and exits non-zero
   (Railway's restart policy — `ON_FAILURE`, 10 retries by default, see
   `railway.toml` — takes it from there).

## Step 5 — Verify readiness

```
curl https://<the-railway-url>/health   # expect 200, {"status": "ok", ...}
curl https://<the-railway-url>/ready    # expect 200, {"ready": true, ...}
```

`/ready` returns HTTP 503 (not just a JSON `ready: false`) when the
schema is not yet current — if you see 503 here, check the deploy logs
for a migration failure before doing anything else.

Confirm `/docs` and `/openapi.json` are **not** reachable (expect 404) —
this is hosted-mode-only behavior; both are open in local dev.

## Step 6 — Log in to the console

Open `https://<the-railway-url>/` in a browser. Unlike local dev, the
owner token is not pre-injected — you'll see a login prompt. Enter the
`MAP_AUTH_TOKEN` value from Step 3. The token is held only in that
browser tab's `sessionStorage` (cleared when the tab closes) and is never
written back to the server or any file.

## Step 7 — Provision the Provider/Model catalog

```
STAGING_API_BASE_URL=https://<the-railway-url> \
MAP_AUTH_TOKEN=<the same token as Step 3> \
STAGING_OPENROUTER_API_KEY=<the staging-only OpenRouter key> \
python -m scripts.provision_staging
```

This creates the OpenRouter Provider (first run only) and triggers a
model-catalog refresh through the application's own API — never by
writing database rows directly. Re-running it later without
`STAGING_OPENROUTER_API_KEY` just re-triggers a catalog refresh. Re-running
it *with* `STAGING_OPENROUTER_API_KEY` set (e.g. after rotating the key on
OpenRouter's side) rotates the existing provider's credential in place via
`POST /providers/{id}/rotate-credential` — the provider row/id and model
catalog are untouched, and the next Test Connection / Agent run picks up
the new key automatically (MA7.7C).

## Step 8 — Create the Evaluation Definition and flagship Workflow

Deliberately not scripted (see `scripts/provision_staging.py`'s
docstring for why): pick the specific starter Agents, models, and
Evaluation Definition through the console the same way the local UAT
workflow ("Software Development Workflow" v2) was built by hand. Do not
copy the local UAT database — its `artifacts.storage_ref` values are
absolute Windows paths that would not resolve inside the container, and
its `secret_references` point at OS-keychain entries that do not exist
there either.

## Step 9 — Smoke test

1. A minimal single-agent Ask run, end to end.
2. The full flagship workflow up to Human Approval, then approve it and
   confirm it completes.
3. Confirm an artifact produced during the run is readable from the
   console (proves the Worker and Web process really share the same
   volume path).

## Step 10 — Durability UAT (MA7.7C, not part of this slice)

Restart/redeploy scenarios, lease-recovery timing, and backup/restore
rehearsal are scoped to MA7.7C, not this deployment. Do not consider
staging production-ready until that slice has run.

## Rollback

If a migration or deploy goes wrong: stop the service, restore the
`data/backups/*.db` file `scripts/hosted_entrypoint.py` created just
before migrating (copy it back over `$MAP_DATA_ROOT/multi_agent_platform.db`
on the volume), then redeploy the previous known-good image/commit.
