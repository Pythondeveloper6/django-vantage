# Agent activity

What agents did, visible to the people who manage them: an export command, a read-only
page, and a badge in the audit log. It needs `VANTAGE["AGENTS"]` on (see
[Agents](agents.md)) and reads data that already exists: audit entries made through an
agent identity, and the run each belongs to. It adds no setting and no table.

## The command

```
python manage.py vantage_agent_activity --since 7d --agent order-sync
python manage.py vantage_agent_activity --since 2026-01-01 --until 2026-01-31 --format csv --output jan.csv
python manage.py vantage_agent_activity --run nightly-2026-01-31 --model shop.order --action delete
python manage.py vantage_agent_activity --runs --since 24h
```

| Option | Meaning |
|---|---|
| `--since`, `--until` | `7d`, `24h`, `2w`, `30m`, an ISO date or an ISO date-time. `--until` is exclusive; a bare date includes that whole day |
| `--agent NAME` | Only this agent. Repeat for several |
| `--run RUN_KEY` | Only this run (the `X-Vantage-Run` value) |
| `--model app.model` | Only entries about this model |
| `--action` | `create`, `update`, `delete`, `trash`, `restore` or `action` |
| `--runs` | One row per run instead of per entry |
| `--format` | `ndjson` (default), `csv` or `json` (one array) |
| `--output FILE` | Write to a file instead of standard output. An existing file is never overwritten unless you also pass `--force` |

Rows are ordered by time, then id, so two exports of the same data are identical. The
queryset is streamed with `.iterator()`; an export of millions of entries holds one chunk
in memory.

### Entry rows

| Column | Content |
|---|---|
| `timestamp` | ISO 8601, when the entry was written |
| `agent`, `owner` | The identity's name, and the username of the person it acts for |
| `service_user` | The account the agent ran as |
| `run_key` | The run the entry belongs to |
| `action`, `action_name` | The verb, and the bulk action's name for `action` entries |
| `model`, `object_id`, `object_repr` | `app_label.model`, the primary key, and the record's label at the time |
| `changes` | `field: old -> new` pairs joined by `; ` |
| `ip` | The client address |

### Run rows (`--runs`)

`agent`, `run_key`, `started`, `last_seen`, `writes`, `rows_changed`, `deletes`.

## The page

"Agent activity" in the System group of the navigation. It lists every identity (name,
owner, service user, access, whether it may delete, whether it needs approval, scopes,
last used, expiry, revocation, and its runs and writes over the last seven days), then
the recent runs, which you can filter by agent. A run opens a view of the audit entries it
made. The page offers CSV and NDJSON downloads built by the same functions as the
command; each download is itself written to the audit log.

The page is read-only. Revoking and rotating an identity stays in `vantage_agent`.

Who can open it: a superuser, or anyone holding `vantage.view_vantagelogentry`, the same
gate as the [audit log](permissions.md), because this is audit data. While `AGENTS` is
off the page is absent from the navigation and its URLs answer 404. The download leaves
out fields hidden from the person downloading, as the audit page does.

## In the audit log

An entry made by an agent shows an "Agent" badge next to the actor, and the run key in
the audit list. The record history panel carries the badge too. The actor text already
reads "Agent NAME (for OWNER)".

## What is recorded, and what is not

Recorded: the audit diff of each write, who it was done as, on whose behalf, in which
run, from which address. Not recorded: prompts, model output, tool-call arguments, request
bodies, or anything the agent read. The panel never sees the first three, and reads are
not audited for any caller. If you need those, keep them in the agent's own logs, keyed by
the same run key you send in `X-Vantage-Run`.

## Sensitive fields and untrusted text

A sensitive field is exported as `field: changed (value not recorded)` with no value in
any format. This is re-applied when the export runs, so a field declared sensitive after
the entry was written is masked too. Text is passed through the same cleaner as other
agent-facing output (invisible and control characters removed, HTML flattened, long values
truncated). In CSV, a cell that starts with `=`, `+`, `-` or `@` is prefixed with a quote
so a spreadsheet does not run it.

## Retention

The package does not delete audit entries or runs; prune them from your own scheduled
job if you must. Deleting audit entries removes them from every export. Deleting an
identity deletes its runs and detaches its remaining entries from the agent: they stay in
the audit log with the actor text, but no longer appear in this export. Export before you
remove an identity, and keep the audit log at least as long as your longest review cycle.
