# gmail-cleanup-agent-n8n

An [n8n](https://n8n.io) workflow that uses a large language model to triage an
overgrown Gmail inbox. It walks old mail in batches, asks an LLM to decide
**keep** (and apply a category label) or **trash** for each message, applies
those decisions through the Gmail API, and then re-triggers itself until the
entire backlog is processed.

It is built to chew through *tens of thousands* of old emails unattended, and
to keep doing a small weekly pass afterwards.

> **Safety first.** "Trash" moves a message to Gmail's Trash, where it stays
> recoverable for 30 days — nothing is permanently deleted. The classification
> rules deliberately bias toward *keep* whenever a message is ambiguous.

---

## Features

- **LLM-powered triage** — every old email is classified *keep* (with a single
  best-fit category label) or *trash*, using rules you control.
- **Self-rechaining loop** — one trigger drains an arbitrarily large backlog
  over many short, crash-safe executions instead of one fragile mega-run.
- **Idempotent** — every processed message is stamped with an `LLM Reviewed`
  label, so re-runs never re-classify the same mail.
- **Model-agnostic** — talks to any OpenAI-compatible chat-completions endpoint
  (Ollama, llama.cpp, vLLM, LM Studio, or the OpenAI API).
- **Self-contained export** — the classification rules and label catalog are
  embedded into the workflow JSON at build time; importing the file is enough.
- **Resilient** — failed Gmail calls retry, transient errors never abort a run,
  and a per-run push notification reports what happened.

## How it works

A *run* is a single n8n execution that processes up to `PER_RUN_LIMIT` emails.

### 1. Triggers

Two entry points feed the same graph:

- **Weekly schedule** — a cron trigger (default: Saturdays 03:00).
- **Re-chain webhook** — an HTTP trigger the workflow calls on *itself* to
  start the next batch (see [The self-rechaining loop](#the-self-rechaining-loop)).

### 2. Setup stage

- **Constants** — a Code node holding all run configuration: the Gmail search,
  the per-run limit, the LLM endpoint and model, plus the embedded
  classification rules and label catalog. It also clears the cross-batch
  accumulator held in workflow static data.
- **List labels → Build label index** — fetches your Gmail labels and builds a
  name→ID map, including the ID of the `LLM Reviewed` bookkeeping label.
- **List messages** — a paginated Gmail search (default
  `older_than:30d -label:llm-reviewed`) returning up to `PER_RUN_LIMIT`
  message IDs.
- **Extract IDs → Get metadata → Parse metadata** — for each message, fetches
  the `From` / `Subject` / `Date` headers and snippet, and derives the
  message's age in days and whether it carries a `List-Unsubscribe` header (a
  strong "bulk mail" signal handed to the model).

### 3. Classify and apply (the batch loop)

Messages are split into batches of 20 and looped:

- **Build prompt** — assembles one prompt containing the classification rules,
  the available label catalog, and the 20 emails' metadata. Emails whose
  metadata fetch failed (no ID) are dropped here so they cannot corrupt a batch.
- **Ask LLM** — POSTs the prompt to an OpenAI-compatible chat-completions
  endpoint, requesting a strict JSON response.
- **Parse decisions** — parses the response (with a regex fallback if the JSON
  is malformed) and *validates* every decision: unknown IDs, duplicates, bad
  actions, and invalid labels are rejected; any message the model omitted is
  defaulted to a safe *keep*.
- **Route action** — a Switch sends each message down one of three branches:
  - **Trash message → Mark reviewed (post-trash)** — trashes the message, then
    stamps it `LLM Reviewed`.
  - **Add label** — applies the chosen category label *and* `LLM Reviewed` in a
    single Gmail call.
  - **Mark reviewed (skip)** — for a kept message whose label could not be
    resolved; stamps `LLM Reviewed` only, so it is not re-evaluated forever.

### 4. Summary and re-chain

When every batch in the run is done:

- **Tally** — counts kept vs. trashed and builds a summary, broken down by
  label.
- **ntfy: summary** — sends one push notification per run.
- **Re-chain** — if the Gmail search still returned a *full* batch, the
  workflow POSTs its own webhook to start a fresh execution.

## The self-rechaining loop

Processing tens of thousands of emails in one execution would be fragile: any
restart mid-run loses all progress. Instead, each run handles a bounded
`PER_RUN_LIMIT` (default 2000) and then, if more mail remains, calls its own
**Re-chain webhook** to launch the next run.

This works because the loop is **naturally resumable**: every processed message
leaves the candidate pool (it is trashed, or it gains the `LLM Reviewed`
label), so the next run's Gmail search returns a *different* set with no
overlap. If a run dies, the next one simply picks up whatever is still
unprocessed.

**Kill switch:** deactivate the workflow in n8n. The re-chain webhook
unregisters, the next re-trigger call gets a 404, and the chain stops cleanly.

## The `LLM Reviewed` label

`LLM Reviewed` is a bookkeeping label the workflow creates and applies to every
message it touches — kept, trashed, or skipped. The default Gmail search
excludes it (`-label:llm-reviewed`), which makes the whole system idempotent:

- A message is never classified twice.
- The re-chain makes guaranteed forward progress.
- Re-running the workflow after a backlog is cleared only ever picks up genuinely
  new mail.

To exempt a message from the LLM entirely, apply `LLM Reviewed` to it by hand.

## Repository layout

```
build_workflow.py        Generator — embeds config/ and writes gmail-cleanup.json
gmail-cleanup.json       The importable n8n workflow (generated; do not hand-edit)
config/
  rules.md               Classification rules — the prompt the LLM follows
  labels.yaml            Label catalog: existing labels + auto-created categories
tests/
  test_structure.py      Structural tests for the workflow graph (pytest)
  codenodes.test.mjs     Logic tests for the Code nodes (node:test)
  harness.mjs            Mock n8n runtime used by the Code-node tests
.github/workflows/ci.yml Runs both test suites on every push and pull request
```

`gmail-cleanup.json` is **generated**. Edit `config/` or `build_workflow.py`
and re-run the generator rather than editing the JSON directly.

## Setup

### Prerequisites

- An [n8n](https://n8n.io) instance (2.x).
- An OpenAI-compatible LLM endpoint. A local [Ollama](https://ollama.com) server
  is a good free option; any instruction-following model that can return JSON
  works.
- A Google account, with a Google Cloud project that has the Gmail API enabled
  and an OAuth 2.0 client.
- Python 3 with `pyyaml` (`pip install pyyaml`) to run the generator.

### 1. Create the Gmail credential in n8n

The workflow authenticates with a **generic OAuth2 API** credential. In n8n,
create one with Google's endpoints:

- Authorization URL: `https://accounts.google.com/o/oauth2/v2/auth`
- Access Token URL: `https://oauth2.googleapis.com/token`
- Scope: `https://www.googleapis.com/auth/gmail.modify`
- Auth URI query parameters: `access_type=offline&prompt=consent`

(The `gmail.modify` scope allows labeling and trashing, but **not** permanent
deletion.)

### 2. Configure

Edit the configuration block at the top of `build_workflow.py`:

| Setting | Purpose |
|---|---|
| `LLM_API_URL`, `LLM_MODEL` | Your chat-completions endpoint and model. |
| `N8N_BASE_URL` | URL n8n can reach itself on (for the re-chain webhook). |
| `NTFY_SERVER`, `NTFY_TOPIC` | Push notifications. Set `NTFY_TOPIC = ""` to disable. |
| `PER_RUN_LIMIT` | Emails per execution before re-chaining. |
| `GMAIL_QUERY` | The Gmail search selecting candidate mail. |
| `SCHEDULE_CRON`, `TIMEZONE` | When the weekly trigger fires. |

Then tune `config/rules.md` and `config/labels.yaml` (see below).

### 3. Generate and import

```bash
pip install pyyaml
python build_workflow.py        # writes gmail-cleanup.json
```

Import `gmail-cleanup.json` into n8n (Workflows → Import from File). Open any
Gmail HTTP node, select your OAuth2 credential — n8n applies it to all of them.
Save and activate the workflow.

### 4. First run

Trigger the workflow manually once ("Execute Workflow"). It will process the
first `PER_RUN_LIMIT` emails and, if more remain, re-chain automatically until
the backlog is clear. After that the weekly schedule keeps the inbox tidy.

## Customizing the classification

- **`config/rules.md`** is the heart of the system — it is the instruction set
  the LLM follows for *keep* vs. *trash*. Rewrite it to match your own
  priorities. The shipped version is a general-purpose example.
- **`config/labels.yaml`** lists the category labels:
  - `existing` — labels already in your Gmail that the model may use.
  - `auto_create` — categories with a one-line description; the model may
    apply them, and you create the labels in Gmail before the first run.

Re-run `python build_workflow.py` after any change.

## Design notes

A few non-obvious choices, documented so they are not "fixed" by accident:

- **Batch loop convergence.** n8n's *Loop Over Items* (SplitInBatches) "done"
  output can fire several times when multiple branches converge on a plain
  No-Op. `Tally` is therefore idempotent, and `ntfy gate` / `Re-chain gate`
  ensure the notification and the re-trigger each fire exactly once per run.
- **"More remain?" is judged on the query result, not the processed count.**
  Emails with a failed metadata fetch are dropped from a batch, so the
  processed total can dip below `PER_RUN_LIMIT` on a genuinely full run. The
  re-chain decision uses the number of IDs the Gmail search returned instead.
- **ID-less emails are filtered early.** A failed `Get metadata` call yields an
  item with no message ID; `Build prompt` drops these so they cannot desync the
  prompt from the decision parser.
- **Notification failure is non-fatal.** The `ntfy` node continues on error — a
  missed push notification never breaks the run or the chain.

## Tests

Two suites guard core functionality so a future change cannot quietly break it:

- **`tests/test_structure.py`** (pytest) — verifies the generator runs, the
  committed `gmail-cleanup.json` is up to date, every connection resolves to a
  real node, the batch loop and re-chain wiring are intact, the config is
  embedded correctly, and no environment-specific values leak in.
- **`tests/codenodes.test.mjs`** (`node:test`) — runs the *actual* JavaScript
  from each Code node in `gmail-cleanup.json` against a mock n8n runtime. It
  covers prompt building, decision parsing and validation, the tally, and the
  re-chain/notification gates — including explicit regression tests for the
  three bugs found while bringing the workflow up.

Run them locally:

```bash
pip install -r requirements-dev.txt
python build_workflow.py     # regenerate before testing
pytest -q                    # structural tests
node --test                  # Code-node logic tests   (or: npm test)
```

[GitHub Actions](.github/workflows/ci.yml) runs both suites on every push and
pull request. **Regenerate `gmail-cleanup.json` and commit it** whenever you
change `build_workflow.py` or anything in `config/` — CI fails if it is stale.

## License

[MIT](LICENSE)
