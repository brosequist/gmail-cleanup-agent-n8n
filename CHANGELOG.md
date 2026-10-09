# Changelog

All notable changes to this workflow. Versions follow
[semantic versioning](https://semver.org): a major version means you must do
something when you upgrade (create labels, add a credential, re-import).

## 2.0.0 — 2026-10-09

**Upgrading from 1.x needs three steps before you activate the new
workflow:** create the `LLM Categorized` and `Needs Review` labels and seed
`LLM Categorized` ([Upgrading](README.md#upgrading-from-an-earlier-version-seed-llm-categorized)),
create every catalog label in Gmail ([step 3](README.md#3-create-the-gmail-labels)),
and create the re-chain secret credential and select it on both re-chain
nodes ([step 1](README.md#1-create-the-credentials-in-n8n)).

### Added

- **Age gate.** Every email is categorised at any age, but only mail at least
  `TRASH_AGE_DAYS` (default 30) old is trash-evaluated. Younger trash verdicts
  are deferred and judged again later. New control labels `LLM Categorized`
  and `Needs Review` alongside `LLM Reviewed`; the Gmail query is now the
  two-arm union of uncategorised mail and old mail without a verdict.
  `LABEL_ON_DEFERRED` keeps chosen categories on deferred trash.
- **One or two labels per kept email**, never a third, and never a second to
  avoid choosing. The same contract as the companion Python agent from v1.4.
- **`LLM_DISABLE_THINKING`** (default on) sends
  `chat_template_kwargs: {enable_thinking: false}`, so reasoning models such as
  qwen3 return an answer instead of empty content.
- **`LLM_USE_API_KEY`** authenticates Ask LLM with an n8n Header Auth
  credential, for the OpenAI API and other keyed endpoints.
- **Skip if draining**: the weekly schedule no longer starts a second chain
  while a drain is still running. `DRAIN_STALE_HOURS` (default 24) bounds how
  long a marker from a dead drain can block it.

### Changed

- **The re-chain webhook requires a shared secret** (n8n header auth), and
  `Re-trigger next batch` sends it. Previously anyone who could reach n8n could
  start a run.
- **The run stops before fetching mail if Gmail lacks any label the workflow
  applies**, naming every missing one. Previously a missing `LLM Reviewed` made
  Gmail silently apply nothing, and the chain re-processed the same mail
  forever while reporting success.
- Run results accumulate per execution (keyed by execution ID) instead of in
  one shared list that every run cleared.

### Fixed

- Prompts are made well-formed (`toWellFormed()`): a lone UTF-16 surrogate, such
  as an emoji cut in half by the snippet limit, made llama-server reject the
  whole request on every run.
- `PER_RUN_LIMIT` above 2,000 stopped the chain after one run: List messages
  could fetch only four pages. It now fetches as many as the limit needs.
- List labels and List messages now retry like every other Gmail call; one
  transient error used to fail the run.
- `NTFY_TOPIC = ""` now disables notifications. It used to POST every summary to
  the bare server URL.

## 1.0.1 — 2026-08-26

### Fixed

- Pagination never terminated once the whole backlog fit on one page: n8n
  ignores `completeExpression` unless `paginationCompleteWhen` is `other`.

## 1.0.0 — 2026-05-21

First release: the self-rechaining n8n workflow with a single `LLM Reviewed`
label, the embedded rules and label catalog, ntfy summaries, and the pytest
and `node:test` suites run by CI.
