#!/usr/bin/env python3
"""Generate the gmail-cleanup n8n workflow JSON.

Run `python build_workflow.py` after editing the configuration below or the
files in config/. It writes gmail-cleanup.json, which you import into n8n.
"""
import json
import math
from pathlib import Path

# ─── Configuration ──────────────────────────────────────────────────────────

# n8n credential ID for your Gmail OAuth2 credential. This is only a placeholder
# baked into the exported JSON; after importing, open any Gmail HTTP node and
# pick your own credential — n8n then rewrites every node to match.
OAUTH_CRED_ID   = "REPLACE_WITH_YOUR_GMAIL_OAUTH2_CREDENTIAL_ID"
OAUTH_CRED_NAME = "Gmail OAuth2"

# Base URL n8n can reach ITSELF on — the workflow POSTs here to re-trigger the
# next batch. The in-process default is fine for most single-node installs.
N8N_BASE_URL = "http://localhost:5678"

# n8n "Header Auth" credential guarding that re-chain webhook. Without it anyone
# who can reach n8n could start runs. Create it after import with any header
# name (e.g. X-Rechain-Secret) and a long random value; the webhook checks it
# and Re-trigger next batch sends it. Placeholder only, like OAUTH_CRED_ID.
RECHAIN_CRED_ID   = "REPLACE_WITH_YOUR_RECHAIN_HEADER_AUTH_CREDENTIAL_ID"
RECHAIN_CRED_NAME = "Gmail cleanup re-chain secret"

# Any OpenAI-compatible chat-completions endpoint + model name. Tested with
# Ollama; also works with llama.cpp, vLLM, LM Studio, or the OpenAI API.
LLM_API_URL = "http://localhost:11434/v1/chat/completions"
LLM_MODEL   = "qwen3"

# Set True for an endpoint that needs an API key (the OpenAI API, most hosted
# providers). Ask LLM then authenticates with an n8n "Header Auth" credential,
# which you create after import with Name `Authorization` and Value
# `Bearer <your key>`, so the key never appears in this file or the JSON.
# Local endpoints (Ollama, llama.cpp) need no key: leave False.
LLM_USE_API_KEY = False
LLM_CRED_ID     = "REPLACE_WITH_YOUR_LLM_API_KEY_CREDENTIAL_ID"
LLM_CRED_NAME   = "LLM API key"

# Send chat_template_kwargs {enable_thinking: false} with every request. Reasoning
# models (qwen3 and similar) otherwise spend the whole token budget "thinking" and
# return EMPTY content with finish_reason "length". Ollama, llama.cpp and vLLM
# accept the field; set False for endpoints that reject unknown request fields,
# such as the OpenAI API.
LLM_DISABLE_THINKING = True

# ntfy push notification for the per-run summary. ntfy.sh is the free public
# server — pick a long, hard-to-guess topic. Set NTFY_TOPIC = "" to disable: the
# ntfy nodes are then left out of the workflow entirely.
NTFY_SERVER = "https://ntfy.sh"
NTFY_TOPIC  = "change-me-to-a-private-topic"

# Optional n8n workflow ID to invoke on failure (n8n "Error Workflow"). "" = none.
ERROR_WORKFLOW_ID = ""

# Emails processed per execution before the workflow re-chains itself. Any value
# works: List messages fetches as many 500-ID pages as this needs.
PER_RUN_LIMIT = 2000

# Age gate. Every email is CATEGORISED at any age, but only an email at least
# TRASH_AGE_DAYS old is TRASH-EVALUATED. A younger email gets its category plus
# `LLM Categorized` only (a trash verdict is deferred, not acted on), and comes
# back through the second query arm once it is old enough for a real verdict.
# rules.md uses age as a signal ("verification codes: KEEP only if recent"), so a
# verdict computed at day 5 would be wrong at day 30 anyway.
TRASH_AGE_DAYS = 30

# Categories the model picks DELIBERATELY on a trash verdict, so the label is
# still applied while the email waits out the age gate (e.g. a "Politics" label
# you want on mail that will be trashed later). Empty by default: deferred trash
# otherwise gets NO category, because on a trash verdict the model is usually
# picking least-bad filler and catch-all labels fill up with noise.
LABEL_ON_DEFERRED = []

# Control labels (create all three in Gmail before the first run; see README).
REVIEWED_LABEL      = "LLM Reviewed"      # a trash/keep verdict was acted on
CATEGORIZED_LABEL   = "LLM Categorized"   # a category decision was made, any age
NEEDS_REVIEW_LABEL  = "Needs Review"      # kept, but the model gave no usable label

# Gmail search selecting candidate emails: the union of two passes. BOTH arms must
# be parenthesised: the unparenthesised form `-label:"A" OR (...)` parses but
# quietly returns the first arm alone, which would disable trashing entirely.
#   arm 1  uncategorised mail, ANY age            -> categorise
#   arm 2  old mail without a verdict             -> trash-evaluate
GMAIL_QUERY = (f'(-label:"{CATEGORIZED_LABEL}") OR '
               f'(older_than:{TRASH_AGE_DAYS}d -label:"{REVIEWED_LABEL}")')

# Weekly trigger: cron expression + timezone.
SCHEDULE_CRON = "0 3 * * 6"   # Saturdays at 03:00
TIMEZONE      = "America/New_York"

RECHAIN_WEBHOOK_PATH = "gmail-cleanup-rechain"
# Fixed so the generated JSON is deterministic (CI checks it for drift).
# n8n assigns its own ID on import; this value is just a stable placeholder.
WORKFLOW_ID = "gmail-cleanup-n8n0"

# ─────────────────────────────────────────────────────────────────────────────

# Embed rules.md + labels.yaml so the imported workflow is fully self-contained.
_CONFIG = Path(__file__).resolve().parent / "config"
rules_md = (_CONFIG / "rules.md").read_text()
labels_yaml_raw = (_CONFIG / "labels.yaml").read_text()

# Parse labels.yaml minimally for prompt-builder use
import yaml
labels_data = yaml.safe_load(labels_yaml_raw)
existing = list(labels_data.get("existing") or [])
auto_create = dict(labels_data.get("auto_create") or {})

VALID_LABELS = existing + list(auto_create.keys())

# --- Prompt builder: assembles the classification prompt for one batch ---
PROMPT_BUILDER_JS = r"""
// Constants from upstream
const c = $('Constants').first().json;
const rules = c.rulesMd;
const existing = c.existingLabels;
const autoCreate = c.autoCreateLabels;

// Batch items (one per email). Drop any without an id — a failed Get metadata
// fetch (Gmail 404/500) propagates an item with no id; it can't be classified
// or labeled, and keeping it desyncs ids vs batchEmails in Parse decisions.
const batch = $input.all().map(i => i.json).filter(e => e && e.id);

const labelLines = [];
for (const n of existing) labelLines.push(`- \`${n}\` (existing)`);
for (const [n, d] of Object.entries(autoCreate)) labelLines.push(`- \`${n}\` — ${d}`);

const emailsBlock = [];
batch.forEach((e, idx) => {
  const meta = [];
  if (e.age_days != null) meta.push(`Age: ${e.age_days} days`);
  if (e.has_list_unsubscribe) meta.push('List-Unsubscribe: yes');
  const metaStr = meta.length ? meta.join('\n') + '\n' : '';
  emailsBlock.push(
    `## ${idx + 1}. id: ${e.id}\n` +
    `From: ${e.sender}\n` +
    `Subject: ${e.subject}\n` +
    metaStr +
    `Snippet: ${(e.snippet || '').slice(0, 300)}\n`
  );
});

const prompt = `${rules.trim()}

# Available labels

Label EVERY email, including emails you mark \`trash\`, in a \`labels\` array. Use label names from this list exactly as written: no nesting, no comma-separated values, no invented names.

**One label is the default.** Add a SECOND label only when two categories are independently true of the same email: a hotel booking receipt really is both a travel record and a receipt; a vet bill really is both a pet matter and a statement. Never a third.

**Do not use a second label to avoid choosing.** Where the rules already settle a pairing (a bill goes in one category, a one-off purchase in another), that decision is made: apply the one the rules name. A second label is for genuine overlap, not for hedging.

${labelLines.join('\n')}

# Output format

Return ONLY a JSON object with this exact structure (no prose, no markdown):

\`\`\`json
{"decisions": [
  {"id": "...", "action": "keep", "labels": ["Receipts"]},
  {"id": "...", "action": "keep", "labels": ["Travel", "Receipts"]},
  {"id": "...", "action": "trash", "labels": ["Receipts"]}
]}
\`\`\`

The \`decisions\` array must have exactly the same number of entries as input emails, in the same order. Each \`id\` must match an input id. Each \`action\` is either \`"keep"\` or \`"trash"\`. \`labels\` is ALWAYS required and always an array of one or two names: pick the best-fitting category from the list above even when \`action\` is \`"trash"\`. Never return \`null\` and never an empty array. Emails newer than ${c.trashAgeDays} days are categorised now but only trash-evaluated later, once they age past the threshold, so every email needs a category regardless of its verdict. Judge \`action\` on the email's own merits; do not soften a \`trash\` verdict just because a label is also required.

# Emails to classify

${emailsBlock.join('\n')}`;

// Lone UTF-16 surrogates (an emoji cut in half by the .slice() above, or a
// malformed one straight from Gmail) make llama-server reject the whole
// request: "invalid string: surrogate U+D800..U+DBFF". The email stays in the
// batch, so every run would fail at the same place. toWellFormed() swaps any
// lone half for U+FFFD and leaves intact emoji alone.
return [{
  json: {
    prompt: prompt.toWellFormed(),
    ids: batch.map(e => e.id),
    batchEmails: batch,
  }
}];
"""

# --- Metadata extractor: turns Gmail messages.get response into {sender, subject, ...} ---
METADATA_EXTRACT_JS = r"""
// runOnceForEachItem mode: return ONE item, not an array
const m = $json;
const headers = (m.payload && m.payload.headers) || [];
const get = (n) => {
  const h = headers.find(x => x.name.toLowerCase() === n.toLowerCase());
  return h ? h.value : '';
};
const internalDate = parseInt(m.internalDate || '0', 10);
const ageDays = internalDate ? Math.floor((Date.now() - internalDate) / 86400000) : null;
return {
  json: {
    id: m.id,
    threadId: m.threadId,
    sender: get('From'),
    subject: get('Subject'),
    snippet: m.snippet || '',
    age_days: ageDays,
    has_list_unsubscribe: !!get('List-Unsubscribe'),
    labelIds: m.labelIds || [],
  }
};
"""

# --- Parse LLM response + plan apply actions ---
DECISION_PARSER_JS = r"""
const resp = $('Ask LLM').first().json;
const batchInfo = $('Build prompt').first().json;
const li = $('Build label index').first().json;
const labelIndex = li.labelNameToId;
const { reviewedLabelId, categorizedLabelId, needsReviewLabelId } = li;
const c = $('Constants').first().json;
const validLabels = c.validLabels;
const trashAgeDays = c.trashAgeDays;
const labelOnDeferred = c.labelOnDeferred || [];

// Age gate: an email may be trash-evaluated only if it is at least trashAgeDays
// old (>=, so exactly N days is eligible) AND has no verdict yet. Unknown age is
// never eligible. Younger emails are categorised only; they come back through the
// older_than arm of gmailQuery once they are old enough.
const trashEligible = (em) => {
  const age = em && em.age_days;
  if (age == null || age < trashAgeDays) return false;
  return !((em.labelIds || []).includes(reviewedLabelId));
};

let raw = resp.choices?.[0]?.message?.content || '';
raw = raw.trim();
// Strip code fences
if (raw.startsWith('```json')) raw = raw.slice(7);
if (raw.startsWith('```')) raw = raw.slice(3);
if (raw.endsWith('```')) raw = raw.slice(0, -3);
raw = raw.trim();

let decisions = [];
try {
  const parsed = JSON.parse(raw);
  if (Array.isArray(parsed.decisions)) decisions = parsed.decisions;
} catch (e) {
  // Regex fallback: the `labels` array, or a legacy scalar `label`.
  const re = /\{\s*"id"\s*:\s*"([^"]+)"\s*,\s*"action"\s*:\s*"(keep|trash)"\s*,\s*(?:"labels"\s*:\s*\[([^\]]*)\]|"label"\s*:\s*(?:"([^"]*)"|null))\s*\}/g;
  let m;
  while ((m = re.exec(raw)) !== null) {
    const labels = m[3] != null ? [...m[3].matchAll(/"([^"]*)"/g)].map(x => x[1])
                                : (m[4] ? [m[4]] : []);
    decisions.push({ id: m[1], action: m[2], labels });
  }
}

// One category label by default, a second only for genuine overlap, never a
// third (same contract as the private engine and the Python CLI v1.4).
const MAX_LABELS = 2;
// Labels on a decision: the `labels` array, or the scalar `label` an older
// prompt or model may still return. Blank and duplicate names dropped.
function decisionLabels(d) {
  const raw = Array.isArray(d.labels) ? d.labels : (d.label ? [d.label] : []);
  const out = [];
  for (const n of raw) if (typeof n === 'string' && n && !out.includes(n)) out.push(n);
  return out;
}

// Validate + plan
const inputIds = new Set(batchInfo.ids);
const inputById = new Map(batchInfo.batchEmails.map(e => [e.id, e]));
const seen = new Set();
const plan = [];
const errors = [];

for (const d of decisions) {
  if (!inputIds.has(d.id)) { errors.push(`unknown id ${d.id}`); continue; }
  if (seen.has(d.id)) { errors.push(`dup id ${d.id}`); continue; }
  const action = d.action;
  if (action !== 'keep' && action !== 'trash') {
    errors.push(`bad action for ${d.id}: ${action}`);
    continue;
  }
  // A label is required for BOTH actions, so that a trash verdict on an email too
  // young to act on still categorises it. A trash verdict with no usable label is
  // still honoured when eligible: dropping it would turn every trash verdict into
  // a skip if a model regressed on the label contract.
  let labels = decisionLabels(d);
  if (labels.length > MAX_LABELS) {
    errors.push(`${labels.length} labels for ${d.id}, kept first ${MAX_LABELS}: ${labels.join('/')}`);
    labels = labels.slice(0, MAX_LABELS);
  }
  const unknown = labels.filter(l => !validLabels.includes(l));
  labels = labels.filter(l => validLabels.includes(l));
  if (unknown.length) errors.push(`${labels.length ? 'dropped unknown label(s)' : 'unknown label'} for ${d.id}: ${unknown.join('/')}`);
  if (!labels.length && action === 'keep') {
    if (!unknown.length) errors.push(`unknown label for ${d.id}: null`);
    continue;                     // keep with no usable label -> needs review
  }
  seen.add(d.id);
  const em = inputById.get(d.id) || {};

  // Defer, never act on, a trash verdict for an email that is not yet eligible.
  const eligible = trashEligible(em);
  const deferred = action === 'trash' && !eligible;
  const finalAction = deferred ? 'keep' : action;
  // Deferred trash keeps only labels listed in labelOnDeferred (per label, not per
  // email); otherwise it gets no category at all, just LLM Categorized.
  const finalLabels = deferred ? labels.filter(l => labelOnDeferred.includes(l)) : labels;
  const labelIds = finalLabels.map(l => labelIndex[l]).filter(Boolean);
  plan.push({
    id: d.id,
    action: finalAction,
    verdict: action,                 // what the model said, for the tally
    deferred,
    labels: finalLabels,
    labelIds,
    label: finalLabels[0] ?? null,   // first label; Route action keys on labelId
    labelId: labelIds[0] ?? null,
    // What a deferred verdict would have labelled, for the tally.
    suppressedLabel: deferred && !finalLabels.length ? (labels[0] ?? null) : null,
    age_days: em.age_days ?? null,
    // LLM Reviewed ONLY when a verdict was acted on (the email was old enough):
    // stamping a young email would make it permanently trash-exempt.
    stampReviewed: eligible,
    reviewedLabelId, categorizedLabelId, needsReviewLabelId,
    sender: em.sender || '',
    subject: em.subject || '',
  });
}

// Missing IDs default to keep-no-label (safe failure) -> the needs-review branch.
for (const id of inputIds) {
  if (!seen.has(id)) {
    const em = inputById.get(id) || {};
    plan.push({
      id, action: 'keep', verdict: null, deferred: false,
      labels: [], labelIds: [], label: null, labelId: null,
      age_days: em.age_days ?? null,
      // Old mail the model keeps failing on is still marked reviewed, so it does
      // not loop forever; young mail stays open for a later pass.
      stampReviewed: trashEligible(em),
      reviewedLabelId, categorizedLabelId, needsReviewLabelId,
      sender: em.sender || '',
      subject: em.subject || '',
    });
    errors.push(`missing decision for ${id}, defaulted to keep`);
  }
}

// Accumulate decisions across batches for Tally
const sd = $getWorkflowStaticData('global');
if (!sd.runResults) sd.runResults = [];
for (const p of plan) sd.runResults.push(p);

return plan.map(p => ({ json: { ...p, parseErrors: errors } }));
"""

LABEL_INDEX_JS = r"""
const labels = $json.labels || [];
const labelNameToId = {};
for (const l of labels) labelNameToId[l.name] = l.id;
const c = $('Constants').first().json;
const control = [c.reviewedLabelName, c.categorizedLabelName, c.needsReviewLabelName];

// Fail loudly, before any message is fetched, if Gmail lacks a label this
// workflow applies. The workflow never creates labels. Gmail silently accepts
// addLabelIds:[null] with a 200, so a missing control label would no-op every
// stamp: nothing gets marked, the query never shrinks, and the self-rechaining
// loop re-processes the same mail forever while every run reports success. A
// missing category label would likewise leave that category unlabelled.
const needed = [...control, ...(c.validLabels || [])];
const missing = needed.filter((n, i) => needed.indexOf(n) === i && !labelNameToId[n]);
if (missing.length) {
  throw new Error(
    `Gmail is missing ${missing.length} label(s) this workflow applies: ` +
    missing.map(n => `"${n}"`).join(', ') + '. ' +
    'Create them in Gmail (Settings > Labels > Create new label, names exactly as ' +
    'written; see the README) and run again. Nothing was classified.');
}

return [{ json: {
  labelNameToId,
  reviewedLabelId: labelNameToId[c.reviewedLabelName],
  categorizedLabelId: labelNameToId[c.categorizedLabelName],
  needsReviewLabelId: labelNameToId[c.needsReviewLabelName],
  total: labels.length,
} }];
"""

TALLY_JS = r"""
const sd = $getWorkflowStaticData('global');
const items = sd.runResults || [];
const perRunLimit = $('Constants').first().json.perRunLimit;
const allKeeps = items.filter(x => x.action === 'keep');
const kept = allKeeps.filter(x => !x.deferred);          // real keeps only
const deferred = items.filter(x => x.deferred);         // trash verdicts waiting on age
const trashed = items.filter(x => x.action === 'trash');
// Genuine categorisation failures only (deferred trash has no label by design).
const uncategorized = kept.filter(x => !x.labelId);
// An email with two labels counts under both.
const byLabel = {};
for (const k of allKeeps) {
  // A deferred item counts only if it kept a labelOnDeferred label.
  const ls = (k.labels && k.labels.length) ? k.labels : (k.deferred ? [] : [k.label || '(no label)']);
  for (const l of ls) byLabel[l] = (byLabel[l] || 0) + 1;
}
const multiLabelled = kept.filter(k => k.labels && k.labels.length > 1).length;
const labelLines = Object.entries(byLabel)
  .sort((a, b) => b[1] - a[1])
  .map(([l, n]) => `  ${l}: ${n}`)
  .join('\n');
const total = items.length;
// "More remain?" must be judged on how many ids the query returned (pre-filter),
// NOT on `total`: Build prompt drops id-less emails (failed metadata fetches),
// so a genuine full batch often yields total slightly under perRunLimit — using
// total here wrongly ends the chain with tens of thousands still unprocessed.
const idCount = $('Extract IDs').all().length;
const moreRemain = idCount >= perRunLimit;
const statusLine = moreRemain
  ? `Backlog not cleared — re-triggering for the next ${perRunLimit}.`
  : `Backlog cleared — nothing left matching the filter.`;
const title = `Gmail cleanup: ${kept.length} kept, ${deferred.length} deferred, ${trashed.length} trashed of ${total}`;
const body = `Run finished.\nProcessed: ${total}\nKept (labeled): ${kept.length}` +
  (multiLabelled ? ` (${multiLabelled} with 2 labels)` : '') +
  `\nDeferred trash (under ${$('Constants').first().json.trashAgeDays} days, re-judged later): ${deferred.length}` +
  `\nTrashed: ${trashed.length}` +
  (uncategorized.length ? `\nNeeds Review (no usable label): ${uncategorized.length}` : '') +
  `\n\nKept by label:\n${labelLines || '  (none)'}\n\n${statusLine}`;
// Clear for next run
sd.runResults = [];
return [{ json: { title, body, total, kept: kept.length, deferred: deferred.length, trashed: trashed.length,
                  uncategorized: uncategorized.length, multiLabelled, moreRemain } }];
"""

# --- Re-chain gate: emit one item to fire the re-trigger, or nothing to stop ---
RECHAIN_GATE_JS = r"""
// n8n skips a node that receives zero input items, so returning [] ends the chain.
// total>0 is true only on the genuine Tally run (spurious SplitInBatches "done"
// re-fires see cleared runResults => total 0), so this fires Re-trigger exactly
// once — and only when the query still has a full batch's worth queued.
const t = $input.first().json;
return (t && t.total > 0 && t.moreRemain) ? [{ json: { source: 'rechain' } }] : [];
"""

# --- ntfy gate: suppress the spurious zero-total Tally re-fires ---
NTFY_GATE_JS = r"""
// SplitInBatches' "done" output re-fires many times at end-of-run (the NoOp
// loop-merge delivers once per branch). Tally runs each time but only the
// genuine completion has total>0 — gate so ntfy sends exactly one notification.
const t = $input.first().json;
return (t && t.total > 0) ? [{ json: t }] : [];
"""

OAUTH_CRED_REF = {"oAuth2Api": {"id": OAUTH_CRED_ID, "name": OAUTH_CRED_NAME}}
RECHAIN_CRED_REF = {"httpHeaderAuth": {"id": RECHAIN_CRED_ID, "name": RECHAIN_CRED_NAME}}

def http_node(name, id_, method, url, position, body=None, body_type=None, query_params=None,
              continue_on_fail=False, pagination=None, response_format=None, retry=False):
    params = {
        "method": method,
        "url": url,
        "authentication": "genericCredentialType",
        "genericAuthType": "oAuth2Api",
        "options": {"timeout": 30000},
    }
    if response_format == "json":
        params["options"]["response"] = {"response": {"responseFormat": "json"}}
    if query_params:
        params["sendQuery"] = True
        params["queryParameters"] = {"parameters": query_params}
    if body is not None:
        params["sendBody"] = True
        if body_type == "json":
            params["contentType"] = "json"
            params["specifyBody"] = "json"
            params["jsonBody"] = body
        elif body_type == "raw":
            params["contentType"] = "raw"
            params["rawContentType"] = "application/json"
            params["body"] = body
    if pagination:
        params["options"]["pagination"] = pagination
    node = {
        "parameters": params,
        "id": id_,
        "name": name,
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": position,
        "credentials": OAUTH_CRED_REF,
    }
    if continue_on_fail:
        node["onError"] = "continueRegularOutput"
    if retry:
        node["retryOnFail"] = True
        node["maxTries"] = 4
        node["waitBetweenTries"] = 3000
    return node

def code_node(name, id_, code, position, runOnce=True):
    # runOnce=True (default) -> runOnceForAllItems; False -> runOnceForEachItem.
    # ALWAYS set mode explicitly; the bare default in n8n 2.x can change.
    return {
        "parameters": {
            "jsCode": code,
            "mode": "runOnceForAllItems" if runOnce else "runOnceForEachItem",
        },
        "id": id_,
        "name": name,
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": position,
    }

# ===== Build nodes =====
nodes = []

# 1. Schedule
nodes.append({
    "parameters": {"rule": {"interval": [{"field": "cronExpression", "expression": SCHEDULE_CRON}]}},
    "id": "n-schedule",
    "name": "Weekly schedule",
    "type": "n8n-nodes-base.scheduleTrigger",
    "typeVersion": 1.1,
    "position": [240, 300],
})

# 1b. Re-chain webhook — second trigger. The workflow POSTs this URL at the end
# of a full batch to start a fresh execution, draining the backlog over many
# short, crash-safe runs. Deactivating the workflow unregisters this URL, which
# cleanly stops an in-progress drain. Header auth: n8n answers 403 to a caller
# without the secret, before any node runs.
nodes.append({
    "parameters": {
        "httpMethod": "POST",
        "path": RECHAIN_WEBHOOK_PATH,
        "authentication": "headerAuth",
        "responseMode": "onReceived",
        "options": {},
    },
    "id": "n-webhook",
    "name": "Re-chain webhook",
    "type": "n8n-nodes-base.webhook",
    "typeVersion": 2,
    "position": [240, 480],
    "webhookId": "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d",
    "credentials": RECHAIN_CRED_REF,
})

# 2. Constants (as Code node — Set doesn't handle nested objects cleanly)
constants_js = (
    "// Clear cross-batch accumulator at start of run\n"
    "const sd = $getWorkflowStaticData('global');\n"
    "sd.runResults = [];\n"
    "return [{ json: " + json.dumps({
        "perRunLimit": PER_RUN_LIMIT,
        "gmailQuery": GMAIL_QUERY,
        "trashAgeDays": TRASH_AGE_DAYS,
        "labelOnDeferred": LABEL_ON_DEFERRED,
        "reviewedLabelName": REVIEWED_LABEL,
        "categorizedLabelName": CATEGORIZED_LABEL,
        "needsReviewLabelName": NEEDS_REVIEW_LABEL,
        "model": LLM_MODEL,
        "llmApiUrl": LLM_API_URL,
        "disableThinking": LLM_DISABLE_THINKING,
        "rulesMd": rules_md,
        "existingLabels": existing,
        "autoCreateLabels": auto_create,
        "validLabels": VALID_LABELS,
    }) + " }];"
)
nodes.append(code_node("Constants", "n-constants", constants_js, [460, 300], runOnce=True))

# 3. List labels
nodes.append(http_node("List labels", "n-list-labels", "GET",
    "https://gmail.googleapis.com/gmail/v1/users/me/labels",
    [680, 200], response_format="json", retry=True))

# 4. Build label index
nodes.append(code_node("Build label index", "n-label-idx", LABEL_INDEX_JS, [900, 200], runOnce=True))

# 5. List messages (paginated)
nodes.append(http_node("List messages", "n-list-msgs", "GET",
    "https://gmail.googleapis.com/gmail/v1/users/me/messages",
    [680, 400],
    query_params=[
        {"name": "q", "value": "={{ $('Constants').first().json.gmailQuery }}"},
        {"name": "maxResults", "value": "={{ Math.min(500, $('Constants').first().json.perRunLimit) }}"},
    ],
    pagination={
        "pagination": {
            "paginationMode": "updateAParameterInEachRequest",
            "parameters": {
                "parameters": [
                    {"name": "pageToken", "type": "qs", "value": "={{ $response.body.nextPageToken }}"}
                ]
            },
            # n8n only evaluates completeExpression when paginationCompleteWhen
            # is "other". Left at its default ("responseIsEmpty") the expression
            # is ignored, and since Gmail's response is never empty the node just
            # re-requests page 1 forever. Harmless while the backlog exceeded 500
            # messages (every page carried a real nextPageToken, so maxRequests
            # capped the run), fatal once it fit in a single page: every response
            # became identical and n8n's identical-response guard aborted the
            # node. Broke the daily run 2026-08-25/26.
            "paginationCompleteWhen": "other",
            "completeExpression": "={{ !$response.body.nextPageToken || ($pageCount * 500) >= $('Constants').first().json.perRunLimit }}",
            "limitPagesFetched": True,
            # Enough pages for PER_RUN_LIMIT. A fixed 4 capped every run at
            # 2,000 IDs, so with a higher limit Tally's moreRemain
            # (ids >= limit) could never be true and the chain stopped after
            # one run.
            "maxRequests": math.ceil(PER_RUN_LIMIT / 500),
        }
    },
    response_format="json", retry=True))

# 6. Extract message IDs
nodes.append(code_node("Extract IDs", "n-extract-ids",
    """
const max = $('Constants').first().json.perRunLimit;
const ids = [];
for (const item of $input.all()) {
  const msgs = item.json.messages || [];
  for (const m of msgs) ids.push(m.id);
  if (ids.length >= max) break;
}
return ids.slice(0, max).map(id => ({ json: { id } }));
""",
    [900, 400], runOnce=True))

# 7. Get metadata per message — bake repeated metadataHeaders into URL
# (n8n's HTTP node collapses repeated queryParameter names; URL bypass works)
nodes.append(http_node("Get metadata", "n-get-meta", "GET",
    "=https://gmail.googleapis.com/gmail/v1/users/me/messages/{{ $json.id }}"
    "?format=metadata&metadataHeaders=From&metadataHeaders=Subject"
    "&metadataHeaders=List-Unsubscribe&metadataHeaders=Date",
    [1120, 400], response_format="json",
    retry=True, continue_on_fail=True))

# 8. Extract metadata fields (per-item)
nodes.append(code_node("Parse metadata", "n-parse-meta", METADATA_EXTRACT_JS, [1340, 400], runOnce=False))
# (runOnce=False -> runOnceForEachItem, code uses $json)

# 9. SplitInBatches
nodes.append({
    "parameters": {"batchSize": 20, "options": {}},
    "id": "n-split",
    "name": "Batch (20)",
    "type": "n8n-nodes-base.splitInBatches",
    "typeVersion": 3,
    "position": [1560, 400],
})

# 10. Build prompt
nodes.append(code_node("Build prompt", "n-build-prompt", PROMPT_BUILDER_JS, [1780, 400], runOnce=True))

# 11. Ask LLM
nodes.append({
    "parameters": {
        "method": "POST",
        "url": "={{ $('Constants').first().json.llmApiUrl }}",
        "sendBody": True,
        "contentType": "json",
        "specifyBody": "json",
        "jsonBody": "={{ JSON.stringify({\n  model: $('Constants').first().json.model,\n  messages: [{ role: 'user', content: $json.prompt }],\n  response_format: { type: 'json_object' },\n  temperature: 0.2,\n  ...($('Constants').first().json.disableThinking ? { chat_template_kwargs: { enable_thinking: false } } : {}),\n}) }}",
        "options": {"timeout": 180000, "response": {"response": {"responseFormat": "json"}}},
    },
    "id": "n-ask-llm",
    "name": "Ask LLM",
    "type": "n8n-nodes-base.httpRequest",
    "typeVersion": 4.2,
    "position": [2000, 400],
    "retryOnFail": True,
    "maxTries": 3,
    "waitBetweenTries": 5000,
})
if LLM_USE_API_KEY:
    nodes[-1]["parameters"].update(
        {"authentication": "genericCredentialType", "genericAuthType": "httpHeaderAuth"})
    nodes[-1]["credentials"] = {"httpHeaderAuth": {"id": LLM_CRED_ID, "name": LLM_CRED_NAME}}

# 12. Parse decisions
nodes.append(code_node("Parse decisions", "n-parse-dec", DECISION_PARSER_JS, [2220, 400], runOnce=True))

# 13. Switch: trash vs keep-with-label vs keep-no-label
nodes.append({
    "parameters": {
        "rules": {
            "values": [
                {
                    "conditions": {
                        "options": {"caseSensitive": True, "typeValidation": "strict"},
                        "conditions": [
                            {"id": "trash", "leftValue": "={{ $json.action }}", "rightValue": "trash",
                             "operator": {"type": "string", "operation": "equals"}}
                        ],
                        "combinator": "and",
                    },
                    "renameOutput": True,
                    "outputKey": "trash",
                },
                {
                    "conditions": {
                        "options": {"caseSensitive": True, "typeValidation": "strict"},
                        "conditions": [
                            {"id": "keep-with-label", "leftValue": "={{ $json.labelId }}", "rightValue": "",
                             "operator": {"type": "string", "operation": "notEmpty", "singleValue": True}}
                        ],
                        "combinator": "and",
                    },
                    "renameOutput": True,
                    "outputKey": "keep",
                },
            ]
        },
        "options": {"fallbackOutput": "extra", "renameFallbackOutput": "skip"},
    },
    "id": "n-switch",
    "name": "Route action",
    "type": "n8n-nodes-base.switch",
    "typeVersion": 3.2,
    "position": [2440, 400],
})

# 14a. Trash branch — POST /messages/{id}/trash
nodes.append(http_node("Trash message", "n-trash", "POST",
    "=https://gmail.googleapis.com/gmail/v1/users/me/messages/{{ $json.id }}/trash",
    [2660, 240], response_format="json",
    retry=True, continue_on_fail=True))

# 14a-2. After trash, mark email as LLM Reviewed (so audit search returns it).
# $json after Trash message is the Gmail API response (id/threadId/labelIds only),
# so we cannot reference $json.reviewedLabelId here — reach back to Build label index.
nodes.append({
    "parameters": {
        "method": "POST",
        "url": "=https://gmail.googleapis.com/gmail/v1/users/me/messages/{{ $json.id }}/modify",
        "authentication": "genericCredentialType",
        "genericAuthType": "oAuth2Api",
        "sendBody": True,
        "contentType": "json",
        "specifyBody": "json",
        # A trashed email was by definition eligible, so it gets both stamps.
        "jsonBody": "={{ JSON.stringify({ addLabelIds: ["
                    "$('Build label index').first().json.reviewedLabelId, "
                    "$('Build label index').first().json.categorizedLabelId"
                    "].filter(Boolean) }) }}",
        "options": {"timeout": 30000, "response": {"response": {"responseFormat": "json"}}},
    },
    "id": "n-reviewed-trash",
    "name": "Mark reviewed (post-trash)",
    "type": "n8n-nodes-base.httpRequest",
    "typeVersion": 4.2,
    "position": [2880, 240],
    "credentials": OAUTH_CRED_REF,
    "retryOnFail": True,
    "maxTries": 4,
    "waitBetweenTries": 3000,
    "onError": "continueRegularOutput",
})

# 14b. Label branch — POST /messages/{id}/modify with addLabelIds (the one or two
# category labels + Reviewed). `labelId` (the first) only drives Route action.
nodes.append({
    "parameters": {
        "method": "POST",
        "url": "=https://gmail.googleapis.com/gmail/v1/users/me/messages/{{ $json.id }}/modify",
        "authentication": "genericCredentialType",
        "genericAuthType": "oAuth2Api",
        "sendBody": True,
        "contentType": "json",
        "specifyBody": "json",
        # Category label(s) + LLM Categorized always; LLM Reviewed ONLY when the
        # email was old enough for its verdict to be acted on.
        "jsonBody": "={{ JSON.stringify({ addLabelIds: "
                    "[...($json.labelIds || [$json.labelId]), $json.categorizedLabelId]"
                    ".concat($json.stampReviewed ? [$json.reviewedLabelId] : [])"
                    ".filter(Boolean) }) }}",
        "options": {"timeout": 30000, "response": {"response": {"responseFormat": "json"}}},
    },
    "id": "n-modify",
    "name": "Add label",
    "type": "n8n-nodes-base.httpRequest",
    "typeVersion": 4.2,
    "position": [2660, 440],
    "credentials": OAUTH_CRED_REF,
    "retryOnFail": True,
    "maxTries": 4,
    "waitBetweenTries": 3000,
    "onError": "continueRegularOutput",
})

# 14c. No-category branch. Two populations land here:
#   (a) deferred trash: young, verdict withheld, filler label suppressed
#   (b) genuine failures: kept, but the model gave no usable label
# Both get LLM Categorized so the categorisation arm stops returning them; only
# (b) gets Needs Review (a deferred email is working as designed, and flagging it
# would bury the real failures). LLM Reviewed only when old enough, as elsewhere.
nodes.append({
    "parameters": {
        "method": "POST",
        "url": "=https://gmail.googleapis.com/gmail/v1/users/me/messages/{{ $json.id }}/modify",
        "authentication": "genericCredentialType",
        "genericAuthType": "oAuth2Api",
        "sendBody": True,
        "contentType": "json",
        "specifyBody": "json",
        "jsonBody": "={{ JSON.stringify({ addLabelIds: [$json.categorizedLabelId]"
                    ".concat($json.deferred ? [] : [$json.needsReviewLabelId])"
                    ".concat($json.stampReviewed ? [$json.reviewedLabelId] : [])"
                    ".filter(Boolean) }) }}",
        "options": {"timeout": 30000, "response": {"response": {"responseFormat": "json"}}},
    },
    "id": "n-reviewed-skip",
    "name": "Mark needs-review",
    "type": "n8n-nodes-base.httpRequest",
    "typeVersion": 4.2,
    "position": [2660, 600],
    "credentials": OAUTH_CRED_REF,
    "retryOnFail": True,
    "maxTries": 4,
    "waitBetweenTries": 3000,
    "onError": "continueRegularOutput",
})

# 15. NoOp to merge branches back (skip branch + trash + modify)
nodes.append({
    "parameters": {},
    "id": "n-merge",
    "name": "Merge actions",
    "type": "n8n-nodes-base.noOp",
    "typeVersion": 1,
    "position": [2880, 400],
})

# 16. Loop back to split via second SplitInBatches output convention
# (handled in connections)

# 17. Tally
nodes.append(code_node("Tally", "n-tally", TALLY_JS, [3100, 600], runOnce=True))

if NTFY_TOPIC:
    # 17b. ntfy gate — drops the spurious zero-total Tally re-fires so ntfy sends once.
    nodes.append(code_node("ntfy gate", "n-ntfy-gate", NTFY_GATE_JS, [3320, 600], runOnce=True))

    # 18. ntfy
    nodes.append({
        "parameters": {
            "method": "POST",
            "url": f"{NTFY_SERVER}/{NTFY_TOPIC}",
            "sendHeaders": True,
            "headerParameters": {
                "parameters": [
                    {"name": "X-Title", "value": "={{ $json.title }}"},
                    # Low priority (silent) for mid-drain runs; high for the final one.
                    {"name": "X-Priority", "value": "={{ $json.moreRemain ? '2' : '4' }}"},
                    {"name": "X-Tags", "value": "envelope,broom"},
                ]
            },
            "sendBody": True,
            "contentType": "raw",
            "rawContentType": "text/plain",
            "body": "={{ $json.body }}",
            "options": {"timeout": 10000},
        },
        "id": "n-ntfy",
        "name": "ntfy: summary",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [3540, 600],
        "retryOnFail": True,
        "maxTries": 3,
        "waitBetweenTries": 5000,
        # A notification failure must never error the workflow or break the chain.
        "onError": "continueRegularOutput",
    })

# 19. Re-chain gate — emits an item only when more emails remain.
nodes.append(code_node("Re-chain gate", "n-rechain-gate", RECHAIN_GATE_JS, [3320, 780], runOnce=True))

# 20. Re-trigger — POST the workflow's own webhook to start the next batch,
# sending the re-chain secret. onError=continue so a 404 (workflow deactivated
# as a kill switch) ends the chain quietly.
nodes.append({
    "parameters": {
        "method": "POST",
        "url": f"{N8N_BASE_URL}/webhook/{RECHAIN_WEBHOOK_PATH}",
        "authentication": "genericCredentialType",
        "genericAuthType": "httpHeaderAuth",
        "sendBody": True,
        "contentType": "json",
        "specifyBody": "json",
        "jsonBody": "={{ JSON.stringify({ source: 'rechain' }) }}",
        "options": {"timeout": 15000},
    },
    "id": "n-retrigger",
    "name": "Re-trigger next batch",
    "type": "n8n-nodes-base.httpRequest",
    "typeVersion": 4.2,
    "position": [3540, 780],
    "credentials": RECHAIN_CRED_REF,
    "retryOnFail": True,
    "maxTries": 3,
    "waitBetweenTries": 5000,
    "onError": "continueRegularOutput",
})

# ===== Connections =====
connections = {
    "Weekly schedule": {"main": [[{"node": "Constants", "type": "main", "index": 0}]]},
    "Constants": {"main": [
        [{"node": "List labels", "type": "main", "index": 0},
         {"node": "List messages", "type": "main", "index": 0}]
    ]},
    "List labels": {"main": [[{"node": "Build label index", "type": "main", "index": 0}]]},
    "List messages": {"main": [[{"node": "Extract IDs", "type": "main", "index": 0}]]},
    "Extract IDs": {"main": [[{"node": "Get metadata", "type": "main", "index": 0}]]},
    "Get metadata": {"main": [[{"node": "Parse metadata", "type": "main", "index": 0}]]},
    "Parse metadata": {"main": [[{"node": "Batch (20)", "type": "main", "index": 0}]]},
    # SplitInBatches output 0 = "done" (after last batch), output 1 = "loop" (current batch)
    "Batch (20)": {"main": [
        [{"node": "Tally", "type": "main", "index": 0}],   # done
        [{"node": "Build prompt", "type": "main", "index": 0}],  # loop
    ]},
    "Build prompt": {"main": [[{"node": "Ask LLM", "type": "main", "index": 0}]]},
    "Ask LLM": {"main": [[{"node": "Parse decisions", "type": "main", "index": 0}]]},
    "Parse decisions": {"main": [[{"node": "Route action", "type": "main", "index": 0}]]},
    # Switch outputs: 0=trash, 1=keep, 2=skip
    "Route action": {"main": [
        [{"node": "Trash message", "type": "main", "index": 0}],
        [{"node": "Add label", "type": "main", "index": 0}],
        [{"node": "Mark needs-review", "type": "main", "index": 0}],
    ]},
    "Trash message": {"main": [[{"node": "Mark reviewed (post-trash)", "type": "main", "index": 0}]]},
    "Mark reviewed (post-trash)": {"main": [[{"node": "Merge actions", "type": "main", "index": 0}]]},
    "Add label": {"main": [[{"node": "Merge actions", "type": "main", "index": 0}]]},
    "Mark needs-review": {"main": [[{"node": "Merge actions", "type": "main", "index": 0}]]},
    "Merge actions": {"main": [[{"node": "Batch (20)", "type": "main", "index": 0}]]},
    "Tally": {"main": [[
        {"node": "Re-chain gate", "type": "main", "index": 0},
    ]]},
    "Re-chain gate": {"main": [[{"node": "Re-trigger next batch", "type": "main", "index": 0}]]},
    "Re-chain webhook": {"main": [[{"node": "Constants", "type": "main", "index": 0}]]},
}
if NTFY_TOPIC:
    # ntfy gate first, matching the canvas (it sits above Re-chain gate).
    connections["Tally"]["main"][0].insert(0, {"node": "ntfy gate", "type": "main", "index": 0})
    connections["ntfy gate"] = {"main": [[{"node": "ntfy: summary", "type": "main", "index": 0}]]}

# Need to also include Build label index in the flow — it's downstream of List labels
# But Constants only has 1 main output, so let me route Build label index inline
connections["Build label index"] = {"main": [[]]}  # terminal, downstream nodes ref via $('Build label index')

_settings = {"executionOrder": "v1", "timezone": TIMEZONE}
if ERROR_WORKFLOW_ID:
    _settings["errorWorkflow"] = ERROR_WORKFLOW_ID

workflow = {
    "id": WORKFLOW_ID,
    "name": "Gmail cleanup",
    "nodes": nodes,
    "connections": connections,
    "settings": _settings,
}

out = Path(__file__).resolve().parent / "gmail-cleanup.json"
out.write_text(json.dumps(workflow, indent=2))
print(f"wrote {out} ({len(out.read_text())} bytes, {len(nodes)} nodes)")
print(f"workflow id: {WORKFLOW_ID}")
