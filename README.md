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

- **LLM-powered triage** — every email is classified *keep* or *trash* using
  rules you control, but only mail at least `TRASH_AGE_DAYS` old (default 30)
  is ever trashed. Younger mail is categorised now and judged later, once the
  age signals in your rules (e.g. "verification codes: keep only if recent")
  actually apply. A kept email gets one category label, or two when
  two categories are genuinely true of it (a hotel booking receipt is both
  travel and a receipt). Never a third, and never a second to avoid choosing.
- **Self-rechaining loop** — one trigger drains an arbitrarily large backlog
  over many short, crash-safe executions instead of one fragile mega-run.
- **Idempotent** — two bookkeeping labels, `LLM Categorized` (a category was
  decided) and `LLM Reviewed` (a verdict was acted on), keep re-runs from
  repeating work; `Needs Review` collects the few emails the model could not
  categorise.
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
  name→ID map, including the three [control labels](#the-control-labels). If a
  control label or any label in your catalog is missing from Gmail, it stops
  the run here with an error naming them, before any mail is fetched (see
  [step 3](#3-create-the-gmail-labels)).
- **List messages** — a paginated Gmail search returning up to
  `PER_RUN_LIMIT` message IDs. The default is the union of two passes, each
  arm parenthesised:
  `(-label:"LLM Categorized") OR (older_than:30d -label:"LLM Reviewed")`,
  meaning "anything not yet categorised, at any age" plus "anything old enough
  that has no verdict yet".
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
  endpoint, requesting a strict JSON response. By default it also sends
  `chat_template_kwargs: {enable_thinking: false}`, so reasoning models answer
  instead of spending the token budget thinking (see `LLM_DISABLE_THINKING`).
- **Parse decisions** — parses the response (with a regex fallback if the JSON
  is malformed) and *validates* every decision: unknown IDs, duplicates, bad
  actions, and invalid labels are rejected; any message the model omitted is
  defaulted to a safe *keep*. Each decision carries a `labels` array (a legacy
  scalar `label` is still accepted); a third label is cut, and an unknown
  label is dropped with a parse error. It then applies the **age gate**: a
  *trash* verdict on an email younger than `TRASH_AGE_DAYS` (or already
  carrying `LLM Reviewed`) is **deferred**, not acted on: the email is kept,
  marked `LLM Categorized` only, and judged again once it is old enough.
  Deferred trash gets no category label unless that label is listed in
  `LABEL_ON_DEFERRED`.
- **Route action** — a Switch sends each message down one of three branches:
  - **Trash message → Mark reviewed (post-trash)** — trashes the message, then
    stamps it `LLM Reviewed` and `LLM Categorized`.
  - **Add label** — applies the chosen category label(s) and `LLM Categorized`
    in a single Gmail call, plus `LLM Reviewed` only if the email was old enough
    for its verdict to count.
  - **Mark needs-review** — for an email with no category to apply: deferred
    trash (stamped `LLM Categorized` only) or a genuine failure where the model
    gave no usable label (also `Needs Review`, so a human can find it). Old
    failures also get `LLM Reviewed` so they are not re-evaluated forever.

### 4. Summary and re-chain

When every batch in the run is done:

- **Tally** — counts kept, deferred, trashed and needs-review emails and
  builds a summary broken down by label (an email with two labels counts under
  both).
- **ntfy: summary** — sends one push notification per run.
- **Re-chain** — if the Gmail search still returned a *full* batch, the
  workflow POSTs its own webhook to start a fresh execution.

## The self-rechaining loop

Processing tens of thousands of emails in one execution would be fragile: any
restart mid-run loses all progress. Instead, each run handles a bounded
`PER_RUN_LIMIT` (default 2000) and then, if more mail remains, calls its own
**Re-chain webhook** to launch the next run.

This works because the loop is **naturally resumable**: every processed message
leaves the candidate pool (it is trashed, or it gains `LLM Categorized` and,
once old enough, `LLM Reviewed`), so the next run's Gmail search returns a
*different* set with no overlap. If a run dies, the next one simply picks up whatever is still
unprocessed.

**Kill switch:** deactivate the workflow in n8n. The re-chain webhook
unregisters, the next re-trigger call gets a 404, and the chain stops cleanly.

## The control labels

Three bookkeeping labels, which **you create once in Gmail before the first
run** ([step 3](#3-create-the-gmail-labels)); the workflow never creates
labels, and stops before classifying anything if one is missing:

| Label | Meaning | Applied to |
|---|---|---|
| `LLM Categorized` | A category decision was made | every email the workflow touches, at any age |
| `LLM Reviewed` | A trash/keep verdict was **acted on** | only emails at least `TRASH_AGE_DAYS` old |
| `Needs Review` | Kept, but the model gave no usable category | genuine failures only (never deferred trash) |

Keeping *categorised* and *reviewed* separate is what makes the age split safe.
If a 5-day-old email were stamped `LLM Reviewed`, it would never be looked at
again, so it would become permanently trash-exempt. Instead a young email gets
its category plus `LLM Categorized`, then the second query arm picks it up once
it is old enough, and it gets a real verdict.

The two query arms exclude these labels, which makes the system idempotent:
nothing is categorised twice, nothing is judged twice, and the re-chain always
makes forward progress. To exempt a message from the LLM entirely, apply both
`LLM Categorized` and `LLM Reviewed` to it by hand.

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
| `LLM_DISABLE_THINKING` | Default `True`: sends `chat_template_kwargs: {enable_thinking: false}` so reasoning models (e.g. qwen3) return an answer instead of empty content with `finish_reason: length`. Set `False` for endpoints that reject unknown request fields, such as the OpenAI API. |
| `N8N_BASE_URL` | URL n8n can reach itself on (for the re-chain webhook). |
| `NTFY_SERVER`, `NTFY_TOPIC` | Push notifications. Set `NTFY_TOPIC = ""` to disable. |
| `PER_RUN_LIMIT` | Emails per execution before re-chaining. |
| `TRASH_AGE_DAYS` | Default `30`: only mail at least this old is trash-evaluated; younger mail is categorised now and judged later. Also sets the `older_than` arm of the query. |
| `LABEL_ON_DEFERRED` | Default `[]`: categories to apply even on a *deferred* trash verdict (e.g. `["Politics"]` for mail you want labelled now and trashed later). Others are suppressed on deferred trash because the model is usually picking filler. |
| `GMAIL_QUERY` | The Gmail search selecting candidate mail. The default is the two-arm union built from the control-label names and `TRASH_AGE_DAYS`; keep both arms parenthesised if you change it. |
| `SCHEDULE_CRON`, `TIMEZONE` | When the weekly trigger fires. |

Then tune `config/rules.md` and `config/labels.yaml` (see below).

### 3. Create the Gmail labels

The workflow **does not create labels**; it only applies labels that already
exist in your Gmail. Before the first run, create (Gmail → Settings → Labels →
*Create new label*), with names exactly as written:

- the three control labels: **`LLM Reviewed`**, **`LLM Categorized`** and
  **`Needs Review`**;
- **every label in `config/labels.yaml`**, both the `existing` and the
  `auto_create` entries.

If any are missing, the first Code node (*Build label index*) fails the run
with an error such as `Gmail is missing 2 label(s) this workflow applies:
"LLM Reviewed", "Receipts"`, and nothing is classified. This is deliberate.
Gmail silently accepts a label that does not exist, so without the check a
fresh install would mark nothing as reviewed and re-process the same mail on
every run while reporting success.

### 4. Generate and import

```bash
pip install pyyaml
python build_workflow.py        # writes gmail-cleanup.json
```

Import `gmail-cleanup.json` into n8n (Workflows → Import from File). Open any
Gmail HTTP node, select your OAuth2 credential — n8n applies it to all of them.
Save and activate the workflow.

### 5. First run

Trigger the workflow manually once ("Execute Workflow"). It will process the
first `PER_RUN_LIMIT` emails and, if more remain, re-chain automatically until
the backlog is clear. After that the weekly schedule keeps the inbox tidy.

On a fresh install the first pass covers the whole mailbox, since nothing is
categorised yet. That is expected, but it is one LLM call per 20 emails, so a
large mailbox takes a while.

### Upgrading from an earlier version (seed `LLM Categorized`)

Earlier versions used `LLM Reviewed` alone. After upgrading, the new
categorisation arm (`-label:"LLM Categorized"`) matches **every** message,
including all the mail the old version already handled, so the first run
would re-classify your whole history. Seed the new label first. It is a
Gmail bulk action, needs no LLM, and runs in the Gmail web UI with your own
login (no server access):

1. Finish or stop any run of the old version (deactivate it), so nothing is
   stamped `LLM Reviewed` behind your back.
2. Create the new control labels (`LLM Categorized`, `Needs Review`;
   [step 3](#3-create-the-gmail-labels)).
3. Gmail → Settings → General → **Conversation view: off**, then Save. With it
   off, search results and bulk labels apply per **message**; with it on they
   apply to whole threads, which would stamp newer, unprocessed replies too.
4. Search for `label:"LLM Reviewed" -label:"LLM Categorized"`, tick the
   select-all box, click **"Select all conversations that match this
   search"**, then **Label as → LLM Categorized**. Large mailboxes are labelled
   in the background; repeat the search until it returns nothing.
5. Turn conversation view back on if you use it, then import the new
   `gmail-cleanup.json` and activate it.

Already-reviewed mail is now categorised as far as the workflow is concerned,
and only genuinely new or uncategorised mail goes to the LLM. Messages the old
version reviewed but never labelled keep no category. Search
`label:"LLM Reviewed" has:nouserlabels` to find any you want to file by hand.

## Customizing the classification

- **`config/rules.md`** is the heart of the system — it is the instruction set
  the LLM follows for *keep* vs. *trash*. Rewrite it to match your own
  priorities. The shipped version is a general-purpose example.
- **`config/labels.yaml`** lists the category labels:
  - `existing` — labels already in your Gmail that the model may use.
  - `auto_create` — categories with a one-line description; the model may
    apply them. Despite the name (shared with the companion Python agent, which
    does create them), this workflow does not: create them in Gmail yourself
    ([step 3](#3-create-the-gmail-labels)), or the run stops with an error
    listing the missing ones.

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
- **Deferred, not dropped.** A trash verdict on young mail is not acted on
  but is not lost either: the email keeps no `LLM Reviewed` stamp, so the
  `older_than` arm returns it for a fresh verdict once it is old enough.
  Exactly `TRASH_AGE_DAYS` old counts as old enough; unknown age never does.
- **Both query arms are parenthesised.** Gmail accepts `-label:"A" OR (...)`
  without complaint but returns only the first arm, which would silently stop
  all trashing. A structural test pins the exact query.
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
