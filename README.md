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
  (Ollama, llama.cpp, vLLM, LM Studio), including keyed ones such as the
  OpenAI API via `LLM_USE_API_KEY`.
- **Self-contained export** — the classification rules and label catalog are
  embedded into the workflow JSON at build time; importing the file is enough.
- **Resilient** — every Gmail call retries, a failed write never aborts a run,
  a weekly run never starts on top of a drain still in progress, and a per-run
  push notification reports what happened. An LLM that stays down through its
  retries does fail the run (see [Failure behaviour](#failure-behaviour)).

## How it works

A *run* is a single n8n execution that processes up to `PER_RUN_LIMIT` emails.

### 1. Triggers

Two entry points feed the same graph:

- **Weekly schedule** — a cron trigger (default: Saturdays 03:00). It passes
  through **Skip if draining**, which ends the execution quietly while a
  re-chain drain is still running (see
  [Overlapping runs](#overlapping-runs)).
- **Re-chain webhook** — an HTTP trigger the workflow calls on *itself* to
  start the next batch (see [The self-rechaining loop](#the-self-rechaining-loop)).
  It requires a shared secret header, so nobody else can start a run.

### 2. Setup stage

- **Constants** — a Code node holding all run configuration: the Gmail search,
  the per-run limit, the LLM endpoint and model, plus the embedded
  classification rules and label catalog. It also starts this execution's
  accumulator in workflow static data, keyed by execution ID so overlapping
  executions cannot touch each other's tally.
- **List labels → Build label index** — fetches your Gmail labels and builds a
  name→ID map, including the three [control labels](#the-control-labels). If a
  control label or any label in your catalog is missing from Gmail, it stops
  the run here with an error naming them, before any mail is fetched (see
  [step 3](#3-create-the-gmail-labels)).
- **List messages** — a paginated Gmail search returning up to
  `PER_RUN_LIMIT` message IDs, in pages of 500 (as many pages as the limit
  needs). The default is the union of two passes, each arm parenthesised:
  `(-label:"LLM Categorized") OR (older_than:30d -label:"LLM Reviewed")`,
  meaning "anything not yet categorised, at any age" plus "anything old enough
  that has no verdict yet".
- **Extract IDs → Get metadata → Parse metadata** — for each message, fetches
  the `From`, `Subject`, `Date` and `List-Unsubscribe` headers, the snippet and
  its current labels. The model sees sender, subject, the first 300 characters
  of the snippet, the age in days (computed from Gmail's `internalDate`, not
  the `Date` header) and whether a `List-Unsubscribe` header is present (a
  strong "bulk mail" signal). Work is per **message**, not per thread: each
  reply in a conversation is judged on its own.

### 3. Classify and apply (the batch loop)

Messages are split into batches of 20 and looped:

- **Build prompt** — assembles one prompt containing the classification rules,
  the available label catalog, and the 20 emails' metadata. Emails whose
  metadata fetch failed (no ID) are dropped here so they cannot corrupt a batch.
- **Ask LLM** — POSTs the prompt to an OpenAI-compatible chat-completions
  endpoint with `response_format: {type: "json_object"}` and `temperature: 0.2`
  (both fixed in the generator). By default it also sends
  `chat_template_kwargs: {enable_thinking: false}`, so reasoning models answer
  instead of spending the token budget thinking (see `LLM_DISABLE_THINKING`).
  With `LLM_USE_API_KEY` it authenticates with an n8n Header Auth credential.
- **Parse decisions** — parses the response (with a regex fallback if the JSON
  is malformed) and *validates* every decision: unknown IDs, duplicates, bad
  actions, and invalid labels are rejected; any message the model omitted is
  defaulted to a safe *keep* with no label. If nothing in the response parses,
  that is every message in the batch: all 20 go to `Needs Review`. The
  validation errors are attached to each item as `parseErrors`, visible in
  the execution data in n8n but not reported anywhere else. Each decision carries a `labels` array (a legacy
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
  both). The counts are the run's **decisions**, not confirmed Gmail results:
  a trash or label call that failed after its retries is still counted. It
  also sets or clears the drain marker that **Skip if draining** reads.
- **ntfy: summary** — sends one push notification per run (see
  [Notifications](#notifications)).
- **Re-chain** — if the Gmail search still returned a *full* batch, the
  workflow POSTs its own webhook, with the shared secret, to start a fresh
  execution.

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

**The re-chain secret.** The webhook uses n8n header auth: a call without the
right header gets a 403 before any node runs. `Re-trigger next batch` sends the
header from the same Header Auth credential ([step 1](#1-create-the-credentials-in-n8n)).
Both nodes must use the **same** credential. `Re-trigger next batch` continues
on error so that the kill switch's 404 ends the chain quietly, which means a
403 from a mismatched secret ends it just as quietly, with the backlog
unfinished.

### Overlapping runs

A large backlog drains over many re-chained runs and can outlast a week. When
the weekly schedule fires during a drain, **Skip if draining** ends that
execution before it fetches any mail, so two chains never judge the same mail
at once. Each run that re-chains records `drainActiveAt` in workflow static
data; the final run clears it. A marker older than `DRAIN_STALE_HOURS`
(default 24) is treated as a dead drain (n8n restarted mid-chain, say) and no
longer blocks the schedule. Re-chained runs never pass through the guard, so a
drain cannot block itself.

One limitation: n8n saves static data at the **end** of an execution, from a
copy loaded at its **start**, and a run re-chains a minute or two before it
finishes. If a drain's last run is shorter than that, its predecessor's save
can land afterwards and restore the marker. The next scheduled run is then
skipped, until the marker goes stale. That costs at most one scheduled run and
never any mail.

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
CHANGELOG.md             Changes by version
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

- An [n8n](https://n8n.io) instance (2.x). The workflow sets execution order
  `v1` in its settings, and its safety check depends on it (see
  [Operational notes](#operational-notes)); leave it as is.
- An OpenAI-compatible LLM endpoint. A local [Ollama](https://ollama.com) server
  is a good free option; any instruction-following model that can return JSON
  works.
- A Google account, with a Google Cloud project that has the Gmail API enabled
  and an OAuth 2.0 client.
- Python 3 with `pyyaml` (`pip install pyyaml`) to run the generator.

### 1. Create the credentials in n8n

**Gmail.** The workflow authenticates with a **generic OAuth2 API** credential.
In n8n, create one with Google's endpoints:

- Authorization URL: `https://accounts.google.com/o/oauth2/v2/auth`
- Access Token URL: `https://oauth2.googleapis.com/token`
- Scope: `https://www.googleapis.com/auth/gmail.modify`
- Auth URI query parameters: `access_type=offline&prompt=consent`

(The `gmail.modify` scope allows labeling and trashing, but **not** permanent
deletion.)

**Re-chain secret (required).** Create a **Header Auth** credential named
`Gmail cleanup re-chain secret`, with any header name (for example
`X-Rechain-Secret`) and a long random value (`openssl rand -hex 32`). The
re-chain webhook rejects calls without it, and `Re-trigger next batch` sends
it. The value lives only in n8n's credential store.

**LLM API key (only for keyed endpoints).** For the OpenAI API or another
hosted provider, create a **Header Auth** credential named `LLM API key` with
Name `Authorization` and Value `Bearer <your key>`, and set
`LLM_USE_API_KEY = True` in step 2. Local endpoints (Ollama, llama.cpp) need
neither.

### 2. Configure

Edit the configuration block at the top of `build_workflow.py`, then
regenerate (step 4). Every setting is baked into the JSON at build time; the
only things you pick in the n8n UI after import are the credentials.

| Setting | Default | Purpose |
|---|---|---|
| `OAUTH_CRED_ID`, `OAUTH_CRED_NAME` | placeholder, `Gmail OAuth2` | Gmail credential reference. Leave the placeholder and pick your credential after import. |
| `N8N_BASE_URL` | `http://localhost:5678` | URL n8n can reach itself on, for the re-chain call. |
| `RECHAIN_CRED_ID`, `RECHAIN_CRED_NAME` | placeholder, `Gmail cleanup re-chain secret` | Header Auth credential for the re-chain webhook (step 1). |
| `LLM_API_URL` | `http://localhost:11434/v1/chat/completions` | Your chat-completions endpoint (the default is a local Ollama). |
| `LLM_MODEL` | `qwen3` | Model name sent with each request. |
| `LLM_USE_API_KEY` | `False` | `True` makes Ask LLM authenticate with a Header Auth credential, for keyed endpoints. |
| `LLM_CRED_ID`, `LLM_CRED_NAME` | placeholder, `LLM API key` | That credential's reference, used only when `LLM_USE_API_KEY` is on. |
| `LLM_DISABLE_THINKING` | `True` | Sends `chat_template_kwargs: {enable_thinking: false}` so reasoning models (e.g. qwen3) return an answer instead of empty content with `finish_reason: length`. Set `False` for endpoints that reject unknown request fields, such as the OpenAI API. |
| `NTFY_SERVER`, `NTFY_TOPIC` | `https://ntfy.sh`, a placeholder topic | Push notifications. `NTFY_TOPIC = ""` leaves the ntfy nodes out entirely. |
| `ERROR_WORKFLOW_ID` | `""` | An n8n Error Workflow to run when this one fails (`settings.errorWorkflow`). Empty for none. |
| `PER_RUN_LIMIT` | `2000` | Emails per execution before re-chaining. Any value works. |
| `TRASH_AGE_DAYS` | `30` | Only mail at least this old is trash-evaluated; younger mail is categorised now and judged later. Also sets the `older_than` arm of the query. |
| `LABEL_ON_DEFERRED` | `[]` | Categories to apply even on a *deferred* trash verdict (e.g. `["Politics"]` for mail you want labelled now and trashed later). Others are suppressed on deferred trash because the model is usually picking filler. |
| `REVIEWED_LABEL`, `CATEGORIZED_LABEL`, `NEEDS_REVIEW_LABEL` | `LLM Reviewed`, `LLM Categorized`, `Needs Review` | Names of the [control labels](#the-control-labels). Rename them here if you want different names; create them in Gmail under the new names. |
| `GMAIL_QUERY` | two-arm union | The Gmail search selecting candidate mail, built from the control-label names and `TRASH_AGE_DAYS`. Keep both arms parenthesised if you change it. |
| `SCHEDULE_CRON`, `TIMEZONE` | `0 3 * * 6`, `America/New_York` | When the weekly trigger fires (Saturdays 03:00). |
| `DRAIN_STALE_HOURS` | `24` | How old a drain marker may be before a scheduled run ignores it. Keep it well above your longest single run. |
| `RECHAIN_WEBHOOK_PATH` | `gmail-cleanup-rechain` | The webhook path. Paths are global in n8n, so a second copy of this workflow needs its own. |
| `WORKFLOW_ID` | `gmail-cleanup-n8n0` | A fixed placeholder that keeps the generated JSON deterministic; n8n assigns its own ID on import. |

Fixed in the generator rather than configurable: 20 emails per LLM call,
`temperature: 0.2`, `response_format: json_object`, 300 snippet characters per
email, and the timeouts and retries under
[Failure behaviour](#failure-behaviour).

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
Then select the re-chain secret credential on **Re-chain webhook** and on
**Re-trigger next batch** (the same one on both), and, if you turned on
`LLM_USE_API_KEY`, the API key credential on **Ask LLM**. Save and activate the
workflow.

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

The rules and catalog are copied into the **Constants** node when you run the
generator; the imported workflow does not read `config/` at run time.

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

## Notifications

With a topic set, each run sends one ntfy message:

- **Title:** `Gmail cleanup: N kept, N deferred, N trashed of N`.
- **Body:** the same counts, two-label keeps, needs-review failures, a
  per-label breakdown, and whether the backlog is cleared or the workflow is
  re-triggering.
- **Priority:** `2` (silent) while a drain is mid-way, `4` on the run that
  clears the backlog. Tags: `envelope,broom`.

The request sends no `Authorization` header, so the topic must accept
unauthenticated publishing: a public server such as ntfy.sh (use a long,
hard-to-guess topic) or a self-hosted topic open for writes. A server that
requires a token answers 403, and that failure is ignored like any other
notification failure.

## Failure behaviour

| Step | Timeout | Retries | If it still fails |
|---|---|---|---|
| List labels, List messages | 30 s | 4 tries, 3 s apart | The run stops before anything is classified. |
| Get metadata | 30 s | 4 tries, 3 s apart | The email is dropped from this run and returns on the next. |
| Ask LLM | 180 s | 3 tries, 5 s apart | **The run fails**, and the chain stops until the next schedule. |
| Trash message, Add label, Mark reviewed, Mark needs-review | 30 s | 4 tries, 3 s apart | The run continues; the email stays in the query and is retried next run. The summary still counts it. |
| ntfy: summary | 10 s | 3 tries, 5 s apart | Ignored. |
| Re-trigger next batch | 15 s | 3 tries, 5 s apart | Ignored: the chain ends (see [the re-chain secret](#the-self-rechaining-loop)). |

A missing Gmail label also fails the run on purpose, before any mail is
fetched (see [step 3](#3-create-the-gmail-labels)). Set `ERROR_WORKFLOW_ID` to
hear about failed runs.

## Operational notes

- **Execution order and canvas position.** *Build label index* must fail the
  run before *List messages* fetches mail. Both hang off *Constants* in
  parallel, and n8n's `v1` execution order runs parallel branches top to
  bottom by canvas position. So keep the workflow on execution order `v1`, and
  keep *List labels* above *List messages* if you rearrange the canvas.
  `tests/test_structure.py` checks both in the generated JSON.
- **One webhook path per copy.** n8n webhook paths are global. A second copy of
  the workflow (for another mailbox) needs its own `RECHAIN_WEBHOOK_PATH` and
  its own re-chain secret, or both copies' re-chains drive one mailbox.
- **ntfy topics must be open for writes.** See [Notifications](#notifications).

## Tests

Two suites guard core functionality so a future change cannot quietly break it:

- **`tests/test_structure.py`** (pytest) — verifies the generator runs, the
  committed `gmail-cleanup.json` is up to date, every connection resolves to a
  real node, the batch loop and re-chain wiring are intact, the config is
  embedded correctly, and no environment-specific values leak in.
- **`tests/codenodes.test.mjs`** (`node:test`) — runs the *actual* JavaScript
  from each Code node in `gmail-cleanup.json` against a mock n8n runtime. It
  covers the label check, prompt building, decision parsing and validation,
  the age gate, the tally, the drain guard and the re-chain/notification
  gates, with a regression test for each bug fixed so far.

Run them locally (Node.js 20 or later: the prompt code uses
`String.prototype.toWellFormed`):

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
