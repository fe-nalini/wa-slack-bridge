# Conversation monitor

Private channel identifiers, approved identities and source selection belong in
protected deployment variables. No customer or organizational data is in this repository.
Sources remain read-only. Existing bridges and webhooks are unchanged.

This first implementation polls Evolution 2.3.7, inventories accessible chats,
imports recoverable messages with deduplication, checkpoints and reconciliation,
and exposes six read-only MCP tools at `/mcp`. No send/mark-read/delete endpoints
exist. PostgreSQL enforces read-only transactions for consumer queries. Separate
tokens protect queries and the internal import of source Slack reports.

Run `python -m unittest discover -s cx-monitor/tests` with `PYTHONPATH=cx-monitor`.
Build from repository root: `docker build -f cx-monitor/Dockerfile .`.
One instance/replica is required for the embedded polling worker.

Required variables: `DATABASE_URL`, `EVOLUTION_URL`, `EVOLUTION_API_KEY`,
`QUERY_TOKEN`, `INGEST_TOKEN`, `EVOLUTION_INSTANCE` (exact approved instance name),
`SLACK_SOURCE_CHANNEL` (approved source ID, never committed).
Tokens and keys are only in Railway variables, never git.
Use `POLL_SECONDS=300`, `BACKFILL_PAGES_PER_CYCLE=20` initially.

## Known limitations / activation gates

- No claim of history before joining, deleted messages, or provider omissions.
- Polling does not guarantee all edits/deletes; authenticated event capture is a later activation gate.
- Repeated full scans reconcile shifted offsets. Counts match only current recoverable records.
- Groups without messages stay visible; classification and staff/customer identity require validation.
- Media metadata is preserved, but downloads/transcripts are not implemented.
- Candidate signals are lexical and require contextual review. They are not confirmed failures or automated causal diagnosis.
- Automatic Slack delivery and continuous report import require a dedicated Slack app credential, channel membership and privacy validation before each publish. Existing webhooks must not be reused for a new destination.
- Backups/restore, retention, alert calibration, automated report comparison and MCP account registration are pending. This is a data foundation, not completion of all architectural acceptance criteria.
- `/health` is liveness only; `ingestion_health` supplies capture health and gaps.

Rollback: stop/remove only the new monitor service. Original Evolution, bridge,
Postgres and Redis configurations are untouched. Preserve the pilot database for
evidence; do not delete it as a rollback shortcut.

## Deployment

Select `cx-monitor/Dockerfile` as the Dockerfile. Set the service start command to
`uvicorn monitor.app:app --host 0.0.0.0 --port 8080 --no-access-log`.
Keep the PORT variable and public routing aligned at 8080.

## Private delivery adapter

The delivery adapter verifies a private, unshared destination containing only
the configured owner and the authenticated app before sending. Additional
tests cover unexpected humans/bots, public/shared channels and a missing owner.
It is disabled by default and not wired to automatic report publication yet.
Credentials, scheduling and the reviewed-report workflow are activation gates.
