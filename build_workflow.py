#!/usr/bin/env python3
"""Generate the gmail-cleanup n8n workflow JSON.

Run `python build_workflow.py` after editing the configuration below or the
files in config/. It writes gmail-cleanup.json, which you import into n8n.
"""
import json
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

# Any OpenAI-compatible chat-completions endpoint + model name. Tested with
# Ollama; also works with llama.cpp, vLLM, LM Studio, or the OpenAI API.
LLM_API_URL = "http://localhost:11434/v1/chat/completions"
LLM_MODEL   = "qwen3"

# ntfy push notification for the per-run summary. ntfy.sh is the free public
# server — pick a long, hard-to-guess topic. Set NTFY_TOPIC = "" to disable.
NTFY_SERVER = "https://ntfy.sh"
NTFY_TOPIC  = "change-me-to-a-private-topic"

# Optional n8n workflow ID to invoke on failure (n8n "Error Workflow"). "" = none.
ERROR_WORKFLOW_ID = ""

# Emails processed per execution before the workflow re-chains itself.
PER_RUN_LIMIT = 2000

# Gmail search selecting candidate emails. Anything matching this that is not
# yet stamped with the `LLM Reviewed` label gets classified.
GMAIL_QUERY = "older_than:30d -label:llm-reviewed"

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

When \`action\` is \`keep\`, choose the single best-matching label from this list. If \`action\` is \`trash\`, set \`label\` to \`null\`. Pick exactly one label per kept email — no nesting, no comma-separated values.

${labelLines.join('\n')}

# Output format

Return ONLY a JSON object with this exact structure (no prose, no markdown):

\`\`\`json
{"decisions": [
  {"id": "...", "action": "keep", "label": "Receipts"},
  {"id": "...", "action": "trash", "label": null}
]}
\`\`\`

The \`decisions\` array must have exactly the same number of entries as input emails, in the same order. Each \`id\` must match an input id. Each \`action\` is either \`"keep"\` or \`"trash"\`. Each \`label\` is either one of the labels above (when keeping) or \`null\` (when trashing).

# Emails to classify

${emailsBlock.join('\n')}`;

return [{
  json: {
    prompt,
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
const labelIndex = $('Build label index').first().json.labelNameToId;
const reviewedLabelId = $('Build label index').first().json.reviewedLabelId;
const validLabels = $('Constants').first().json.validLabels;

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
  // Regex fallback
  const re = /\{\s*"id"\s*:\s*"([^"]+)"\s*,\s*"action"\s*:\s*"(keep|trash)"\s*,\s*"label"\s*:\s*(?:"([^"]*)"|null)\s*\}/g;
  let m;
  while ((m = re.exec(raw)) !== null) {
    decisions.push({ id: m[1], action: m[2], label: m[3] ?? null });
  }
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
  let label = d.label;
  if (action === 'keep' && !validLabels.includes(label)) {
    errors.push(`unknown label for ${d.id}: ${label}`);
    continue;
  }
  if (action === 'trash') label = null;
  seen.add(d.id);
  const labelId = label ? labelIndex[label] : null;
  const em = inputById.get(d.id) || {};
  plan.push({
    id: d.id,
    action,
    label,
    labelId,
    reviewedLabelId,
    sender: em.sender || '',
    subject: em.subject || '',
  });
}

// Missing IDs default to keep-no-label (safe failure)
for (const id of inputIds) {
  if (!seen.has(id)) {
    const em = inputById.get(id) || {};
    plan.push({
      id, action: 'keep', label: null, labelId: null, reviewedLabelId,
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
const reviewedLabelId = labelNameToId['LLM Reviewed'] || null;
return [{ json: { labelNameToId, reviewedLabelId, total: labels.length } }];
"""

TALLY_JS = r"""
const sd = $getWorkflowStaticData('global');
const items = sd.runResults || [];
const perRunLimit = $('Constants').first().json.perRunLimit;
const kept = items.filter(x => x.action === 'keep');
const trashed = items.filter(x => x.action === 'trash');
const byLabel = {};
for (const k of kept) {
  const l = k.label || '(no label)';
  byLabel[l] = (byLabel[l] || 0) + 1;
}
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
const title = `Gmail cleanup: ${kept.length} kept, ${trashed.length} trashed of ${total}`;
const body = `Run finished.\nProcessed: ${total}\nKept (labeled): ${kept.length}\nTrashed: ${trashed.length}\n\nKept by label:\n${labelLines || '  (none)'}\n\n${statusLine}`;
// Clear for next run
sd.runResults = [];
return [{ json: { title, body, total, kept: kept.length, trashed: trashed.length, moreRemain } }];
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
# cleanly stops an in-progress drain.
nodes.append({
    "parameters": {
        "httpMethod": "POST",
        "path": RECHAIN_WEBHOOK_PATH,
        "responseMode": "onReceived",
        "options": {},
    },
    "id": "n-webhook",
    "name": "Re-chain webhook",
    "type": "n8n-nodes-base.webhook",
    "typeVersion": 2,
    "position": [240, 480],
    "webhookId": "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d",
})

# 2. Constants (as Code node — Set doesn't handle nested objects cleanly)
constants_js = (
    "// Clear cross-batch accumulator at start of run\n"
    "const sd = $getWorkflowStaticData('global');\n"
    "sd.runResults = [];\n"
    "return [{ json: " + json.dumps({
        "perRunLimit": PER_RUN_LIMIT,
        "gmailQuery": GMAIL_QUERY,
        "model": LLM_MODEL,
        "llmApiUrl": LLM_API_URL,
        "ntfyTopic": NTFY_TOPIC,
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
    [680, 200], response_format="json"))

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
            "completeExpression": "={{ !$response.body.nextPageToken || ($pageCount * 500) >= $('Constants').first().json.perRunLimit }}",
            "limitPagesFetched": True,
            "maxRequests": 4,
        }
    },
    response_format="json"))

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
        "jsonBody": "={{ JSON.stringify({\n  model: $('Constants').first().json.model,\n  messages: [{ role: 'user', content: $json.prompt }],\n  response_format: { type: 'json_object' },\n  temperature: 0.2,\n}) }}",
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
        "jsonBody": "={{ JSON.stringify({ addLabelIds: [$('Build label index').first().json.reviewedLabelId] }) }}",
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

# 14b. Label branch — POST /messages/{id}/modify with addLabelIds (category + Reviewed)
nodes.append({
    "parameters": {
        "method": "POST",
        "url": "=https://gmail.googleapis.com/gmail/v1/users/me/messages/{{ $json.id }}/modify",
        "authentication": "genericCredentialType",
        "genericAuthType": "oAuth2Api",
        "sendBody": True,
        "contentType": "json",
        "specifyBody": "json",
        "jsonBody": "={{ JSON.stringify({ addLabelIds: [$json.labelId, $json.reviewedLabelId] }) }}",
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

# 14c. Skip branch — kept by LLM but no valid label resolved. Apply Reviewed
# so future runs don't re-evaluate the same email indefinitely.
nodes.append({
    "parameters": {
        "method": "POST",
        "url": "=https://gmail.googleapis.com/gmail/v1/users/me/messages/{{ $json.id }}/modify",
        "authentication": "genericCredentialType",
        "genericAuthType": "oAuth2Api",
        "sendBody": True,
        "contentType": "json",
        "specifyBody": "json",
        "jsonBody": "={{ JSON.stringify({ addLabelIds: [$json.reviewedLabelId] }) }}",
        "options": {"timeout": 30000, "response": {"response": {"responseFormat": "json"}}},
    },
    "id": "n-reviewed-skip",
    "name": "Mark reviewed (skip)",
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

# 20. Re-trigger — POST the workflow's own webhook to start the next batch.
# Internal cluster URL: no Authentik, no auth. onError=continue so a 404
# (workflow deactivated as a kill switch) ends the chain quietly.
nodes.append({
    "parameters": {
        "method": "POST",
        "url": f"{N8N_BASE_URL}/webhook/{RECHAIN_WEBHOOK_PATH}",
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
        [{"node": "Mark reviewed (skip)", "type": "main", "index": 0}],
    ]},
    "Trash message": {"main": [[{"node": "Mark reviewed (post-trash)", "type": "main", "index": 0}]]},
    "Mark reviewed (post-trash)": {"main": [[{"node": "Merge actions", "type": "main", "index": 0}]]},
    "Add label": {"main": [[{"node": "Merge actions", "type": "main", "index": 0}]]},
    "Mark reviewed (skip)": {"main": [[{"node": "Merge actions", "type": "main", "index": 0}]]},
    "Merge actions": {"main": [[{"node": "Batch (20)", "type": "main", "index": 0}]]},
    "Tally": {"main": [[
        {"node": "ntfy gate", "type": "main", "index": 0},
        {"node": "Re-chain gate", "type": "main", "index": 0},
    ]]},
    "ntfy gate": {"main": [[{"node": "ntfy: summary", "type": "main", "index": 0}]]},
    "Re-chain gate": {"main": [[{"node": "Re-trigger next batch", "type": "main", "index": 0}]]},
    "Re-chain webhook": {"main": [[{"node": "Constants", "type": "main", "index": 0}]]},
}

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
