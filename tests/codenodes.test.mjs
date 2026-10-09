// Logic tests for the workflow's Code nodes. Run with: node --test tests/
//
// These exercise the actual jsCode embedded in gmail-cleanup.json, including
// explicit regressions for the three bugs found while bringing the workflow up:
//   1. id-less emails desyncing Parse decisions
//   2. ntfy notification firing many times per run
//   3. the re-chain stopping early on a partial-but-full batch
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { runNode } from './harness.mjs';

// ─── Build label index ───────────────────────────────────────────────────────

const LI_CONSTANTS = { Constants: [{ json: { validLabels: ['Taxes', 'Receipts'] } }] };

test('Build label index maps names to IDs and finds LLM Reviewed', () => {
  const out = runNode('Build label index', {
    json: { labels: [
      { name: 'LLM Reviewed', id: 'Label_19' },
      { name: 'Receipts', id: 'Label_5' },
      { name: 'Taxes', id: 'Label_7' },
    ] },
    nodes: LI_CONSTANTS,
  });
  assert.equal(out[0].json.reviewedLabelId, 'Label_19');
  assert.equal(out[0].json.labelNameToId.Receipts, 'Label_5');
});

// Regression: with the label absent, reviewedLabelId used to be null, Gmail
// accepted addLabelIds [null] with a 200, nothing was stamped, and the
// self-rechaining loop re-processed the same mail forever while reporting
// success. It must now stop the run before any message is fetched.
test('Build label index fails loudly when LLM Reviewed is missing', () => {
  assert.throws(
    () => runNode('Build label index', {
      json: { labels: [{ name: 'Receipts', id: 'Label_5' }, { name: 'Taxes', id: 'Label_7' }] },
      nodes: LI_CONSTANTS,
    }),
    (err) => /missing 1 label\(s\)/.test(err.message) && /"LLM Reviewed"/.test(err.message)
      && /Nothing was classified/.test(err.message),
  );
});

test('Build label index fails loudly when a category label is missing from Gmail', () => {
  assert.throws(
    () => runNode('Build label index', {
      json: { labels: [{ name: 'LLM Reviewed', id: 'Label_19' }, { name: 'Taxes', id: 'Label_7' }] },
      nodes: LI_CONSTANTS,
    }),
    (err) => /"Receipts"/.test(err.message) && !/"Taxes"/.test(err.message),
  );
});

test('Build label index lists every missing label once', () => {
  assert.throws(
    () => runNode('Build label index', {
      json: { labels: [] },
      nodes: { Constants: [{ json: { validLabels: ['Receipts', 'Receipts', 'Taxes'] } }] },
    }),
    (err) => /missing 3 label\(s\)/.test(err.message),
  );
});

// ─── Parse metadata ──────────────────────────────────────────────────────────

test('Parse metadata extracts headers, age, and the unsubscribe signal', () => {
  const out = runNode('Parse metadata', {
    json: {
      id: 'm1', threadId: 't1', snippet: 'hello there',
      internalDate: String(Date.now() - 5 * 86400000),
      payload: { headers: [
        { name: 'From', value: 'alice@example.com' },
        { name: 'Subject', value: 'Lunch?' },
        { name: 'List-Unsubscribe', value: '<mailto:x>' },
      ] },
      labelIds: ['INBOX'],
    },
  });
  assert.equal(out.json.id, 'm1');
  assert.equal(out.json.sender, 'alice@example.com');
  assert.equal(out.json.subject, 'Lunch?');
  assert.equal(out.json.has_list_unsubscribe, true);
  assert.equal(out.json.age_days, 5);
});

test('Parse metadata tolerates a failed fetch (no payload, no id)', () => {
  // A failed Get metadata call propagates an error-shaped item — must not throw.
  const out = runNode('Parse metadata', { json: { error: 'Not Found' } });
  assert.equal(out.json.id, undefined);
  assert.equal(out.json.sender, '');
  assert.equal(out.json.has_list_unsubscribe, false);
});

// ─── Extract IDs ─────────────────────────────────────────────────────────────

test('Extract IDs flattens message pages and caps at perRunLimit', () => {
  const out = runNode('Extract IDs', {
    input: [
      { json: { messages: [{ id: 'a' }, { id: 'b' }] } },
      { json: { messages: [{ id: 'c' }, { id: 'd' }] } },
    ],
    nodes: { Constants: [{ json: { perRunLimit: 3 } }] },
  });
  assert.equal(out.length, 3);
  assert.deepEqual(out.map((o) => o.json.id), ['a', 'b', 'c']);
});

// ─── Build prompt ────────────────────────────────────────────────────────────

const CONSTANTS = [{ json: {
  rulesMd: 'CLASSIFICATION RULES GO HERE',
  existingLabels: ['Taxes'],
  autoCreateLabels: { Receipts: 'order confirmations' },
  validLabels: ['Taxes', 'Receipts'],
} }];

test('Build prompt embeds rules + labels and lists the emails', () => {
  const out = runNode('Build prompt', {
    input: [
      { json: { id: 'a', sender: 's', subject: 'Hello', snippet: 'hi', age_days: 3 } },
    ],
    nodes: { Constants: CONSTANTS },
  });
  const j = out[0].json;
  assert.ok(j.prompt.includes('CLASSIFICATION RULES GO HERE'));
  assert.ok(j.prompt.includes('Receipts'));
  assert.ok(j.prompt.includes('id: a'));
  assert.deepEqual(j.ids, ['a']);
});

test('Build prompt drops id-less emails (regression: failed-metadata items)', () => {
  const out = runNode('Build prompt', {
    input: [
      { json: { id: 'a', sender: 's', subject: 't', snippet: '' } },
      { json: { sender: 'no-id', subject: 't', snippet: '' } }, // failed fetch
      { json: { id: 'b', sender: 's', subject: 't', snippet: '' } },
    ],
    nodes: { Constants: CONSTANTS },
  });
  assert.deepEqual(out[0].json.ids, ['a', 'b']);
  assert.equal(out[0].json.batchEmails.length, 2);
});

test('Build prompt never emits a lone surrogate (emoji cut mid-pair by the 300-char slice)', () => {
  // 150 two-code-unit emoji = 300 code units; one leading char pushes the
  // last emoji across the cut, leaving its high surrogate alone at index 299.
  const snippet = 'x' + '🐆'.repeat(150);
  const lone = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/;
  assert.ok(lone.test(snippet.slice(0, 300)), 'fixture must reproduce the cut');

  const out = runNode('Build prompt', {
    input: [
      { json: { id: 'cut', sender: 's', subject: 'zoo 🐅', snippet } },
      { json: { id: 'ok', sender: 's', subject: 'intact', snippet: 'party 🎉 tonight' } },
    ],
    nodes: { Constants: CONSTANTS },
  });
  const p = out[0].json.prompt;
  assert.ok(!lone.test(p), 'prompt still contains a lone surrogate');
  assert.ok(p.includes('�'), 'the broken half becomes U+FFFD');
  assert.ok(p.includes('party 🎉 tonight') && p.includes('zoo 🐅'), 'intact emoji survive');
  // What llama-server actually receives: the JSON body must round-trip.
  assert.doesNotThrow(() => JSON.parse(JSON.stringify({ content: p })));
  assert.equal(JSON.stringify(p).includes('\\ud83d"'), false);
});

// ─── Parse decisions ─────────────────────────────────────────────────────────

function pdContext({ content, ids, batchEmails, staticData = {} }) {
  return {
    staticData,
    nodes: {
      'Ask LLM': [{ json: { choices: [{ message: { content } }] } }],
      'Build prompt': [{ json: { ids, batchEmails } }],
      'Build label index': [{ json: {
        labelNameToId: { Receipts: 'Label_5' }, reviewedLabelId: 'Label_19',
      } }],
      Constants: [{ json: { validLabels: ['Receipts'] } }],
    },
  };
}

test('Parse decisions: valid keep + trash decisions resolve correctly', () => {
  const emails = [
    { id: 'a', sender: 'sa', subject: 'ta' },
    { id: 'b', sender: 'sb', subject: 'tb' },
  ];
  const out = runNode('Parse decisions', pdContext({
    content: JSON.stringify({ decisions: [
      { id: 'a', action: 'keep', label: 'Receipts' },
      { id: 'b', action: 'trash', label: null },
    ] }),
    ids: ['a', 'b'], batchEmails: emails,
  }));
  const byId = Object.fromEntries(out.map((o) => [o.json.id, o.json]));
  assert.equal(byId.a.action, 'keep');
  assert.equal(byId.a.labelId, 'Label_5');
  assert.equal(byId.b.action, 'trash');
  assert.equal(byId.b.labelId, null);
});

test('Parse decisions: a desynced batch does not throw (regression: undefined id)', () => {
  // batchEmails has an entry whose id property is missing; ids has the matching
  // array slot as null (how JSON serializes an undefined array element).
  const emails = [{ id: 'a', sender: 'sa', subject: 'ta' }, { sender: 'sb', subject: 'tb' }];
  const out = runNode('Parse decisions', pdContext({
    content: JSON.stringify({ decisions: [{ id: 'a', action: 'trash', label: null }] }),
    ids: ['a', null], batchEmails: emails,
  }));
  assert.ok(Array.isArray(out)); // the point: no crash
});

test('Parse decisions: an invalid label is rejected, message defaults to keep', () => {
  const emails = [{ id: 'a', sender: 'sa', subject: 'ta' }];
  const out = runNode('Parse decisions', pdContext({
    content: JSON.stringify({ decisions: [{ id: 'a', action: 'keep', label: 'Nonsense' }] }),
    ids: ['a'], batchEmails: emails,
  }));
  assert.equal(out[0].json.action, 'keep');
  assert.equal(out[0].json.labelId, null); // no valid label resolved
});

test('Parse decisions: a message the model omitted defaults to a safe keep', () => {
  const emails = [
    { id: 'a', sender: 'sa', subject: 'ta' },
    { id: 'b', sender: 'sb', subject: 'tb' },
  ];
  const out = runNode('Parse decisions', pdContext({
    content: JSON.stringify({ decisions: [{ id: 'a', action: 'trash', label: null }] }),
    ids: ['a', 'b'], batchEmails: emails,
  }));
  const b = out.find((o) => o.json.id === 'b');
  assert.equal(b.json.action, 'keep');
});

test('Parse decisions: malformed JSON falls back to regex extraction', () => {
  const emails = [{ id: 'a', sender: 'sa', subject: 'ta' }];
  const out = runNode('Parse decisions', pdContext({
    content: 'noise {"id": "a", "action": "trash", "label": null} more noise',
    ids: ['a'], batchEmails: emails,
  }));
  assert.equal(out[0].json.action, 'trash');
});

test('Parse decisions: results accumulate into workflow static data', () => {
  const emails = [{ id: 'a', sender: 'sa', subject: 'ta' }];
  const staticData = {};
  runNode('Parse decisions', pdContext({
    content: JSON.stringify({ decisions: [{ id: 'a', action: 'trash', label: null }] }),
    ids: ['a'], batchEmails: emails, staticData,
  }));
  assert.equal(staticData.runResults.length, 1);
});

// ─── Tally ───────────────────────────────────────────────────────────────────

function tallyCtx(runResults, idCount, perRunLimit = 2000) {
  return {
    staticData: { runResults },
    nodes: {
      Constants: [{ json: { perRunLimit } }],
      'Extract IDs': Array.from({ length: idCount }, () => ({ json: { id: 'x' } })),
    },
  };
}

test('Tally counts kept vs trashed', () => {
  const out = runNode('Tally', tallyCtx([
    { action: 'keep', label: 'Receipts' },
    { action: 'keep', label: 'Receipts' },
    { action: 'trash' },
  ], 3));
  assert.equal(out[0].json.total, 3);
  assert.equal(out[0].json.kept, 2);
  assert.equal(out[0].json.trashed, 1);
});

test('Tally: moreRemain true on a full query batch even if total < perRunLimit (regression)', () => {
  // 1983 processed (id-less emails were filtered out) but the query returned a
  // full 2000 IDs — the chain MUST continue.
  const out = runNode('Tally', tallyCtx(
    Array.from({ length: 1983 }, () => ({ action: 'trash' })),
    2000,
  ));
  assert.equal(out[0].json.total, 1983);
  assert.equal(out[0].json.moreRemain, true);
});

test('Tally: moreRemain false when the query returned a partial batch', () => {
  const out = runNode('Tally', tallyCtx(
    Array.from({ length: 500 }, () => ({ action: 'trash' })),
    500,
  ));
  assert.equal(out[0].json.moreRemain, false);
});

test('Tally clears the accumulator for the next run', () => {
  const ctx = tallyCtx([{ action: 'trash' }], 1);
  runNode('Tally', ctx);
  assert.deepEqual(ctx.staticData.runResults, []);
});

// ─── Gates (the spurious-refire guards) ──────────────────────────────────────

test('ntfy gate: passes the genuine run (total > 0)', () => {
  const out = runNode('ntfy gate', { input: [{ json: { total: 7, title: 'x', body: 'y' } }] });
  assert.equal(out.length, 1);
});

test('ntfy gate: drops a spurious zero-total re-fire (regression: ntfy fired 86x)', () => {
  const out = runNode('ntfy gate', { input: [{ json: { total: 0 } }] });
  assert.equal(out.length, 0);
});

test('Re-chain gate: fires once — only when total > 0 AND more remain', () => {
  assert.equal(runNode('Re-chain gate', { input: [{ json: { total: 2000, moreRemain: true } }] }).length, 1);
  assert.equal(runNode('Re-chain gate', { input: [{ json: { total: 0, moreRemain: true } }] }).length, 0);
  assert.equal(runNode('Re-chain gate', { input: [{ json: { total: 2000, moreRemain: false } }] }).length, 0);
});
