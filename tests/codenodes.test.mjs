// Logic tests for the workflow's Code nodes. Run with: node --test tests/
//
// These exercise the actual jsCode embedded in gmail-cleanup.json, including
// explicit regressions for the three bugs found while bringing the workflow up:
//   1. id-less emails desyncing Parse decisions
//   2. ntfy notification firing many times per run
//   3. the re-chain stopping early on a partial-but-full batch
// and for the ones found since (lone surrogates, missing labels, the age gate,
// the 2,000-per-run cap, overlapping drains).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { runNode } from './harness.mjs';

// ─── Build label index ───────────────────────────────────────────────────────

const CONTROL = { reviewedLabelName: 'LLM Reviewed', categorizedLabelName: 'LLM Categorized', needsReviewLabelName: 'Needs Review' };
const LI_CONSTANTS = { Constants: [{ json: { ...CONTROL, validLabels: ['Taxes', 'Receipts'] } }] };
const CONTROL_LABELS = [
  { name: 'LLM Reviewed', id: 'Label_19' }, { name: 'LLM Categorized', id: 'Label_20' },
  { name: 'Needs Review', id: 'Label_21' },
];

test('Build label index maps names to IDs and finds LLM Reviewed', () => {
  const out = runNode('Build label index', {
    json: { labels: [
      ...CONTROL_LABELS,
      { name: 'Receipts', id: 'Label_5' },
      { name: 'Taxes', id: 'Label_7' },
    ] },
    nodes: LI_CONSTANTS,
  });
  assert.equal(out[0].json.reviewedLabelId, 'Label_19');
  assert.equal(out[0].json.categorizedLabelId, 'Label_20');
  assert.equal(out[0].json.needsReviewLabelId, 'Label_21');
  assert.equal(out[0].json.labelNameToId.Receipts, 'Label_5');
});

// Regression: with the label absent, reviewedLabelId used to be null, Gmail
// accepted addLabelIds [null] with a 200, nothing was stamped, and the
// self-rechaining loop re-processed the same mail forever while reporting
// success. It must now stop the run before any message is fetched.
test('Build label index fails loudly when LLM Reviewed is missing', () => {
  assert.throws(
    () => runNode('Build label index', {
      json: { labels: [...CONTROL_LABELS.filter((l) => l.name !== 'LLM Reviewed'),
        { name: 'Receipts', id: 'Label_5' }, { name: 'Taxes', id: 'Label_7' }] },
      nodes: LI_CONSTANTS,
    }),
    (err) => /missing 1 label\(s\)/.test(err.message) && /"LLM Reviewed"/.test(err.message)
      && /Nothing was classified/.test(err.message),
  );
});

test('Build label index fails loudly when a category label is missing from Gmail', () => {
  assert.throws(
    () => runNode('Build label index', {
      json: { labels: [...CONTROL_LABELS, { name: 'Taxes', id: 'Label_7' }] },
      nodes: LI_CONSTANTS,
    }),
    (err) => /"Receipts"/.test(err.message) && !/"Taxes"/.test(err.message),
  );
});

test('Build label index lists every missing label once', () => {
  assert.throws(
    () => runNode('Build label index', {
      json: { labels: [] },
      nodes: { Constants: [{ json: { ...CONTROL, validLabels: ['Receipts', 'Receipts', 'Taxes'] } }] },
    }),
    (err) => /missing 5 label\(s\)/.test(err.message),   // 3 control + 2 distinct categories
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
        categorizedLabelId: 'Label_20', needsReviewLabelId: 'Label_21',
      } }],
      Constants: [{ json: { validLabels: ['Receipts'], trashAgeDays: 30, labelOnDeferred: [] } }],
    },
  };
}

test('Parse decisions: valid keep + trash decisions resolve correctly', () => {
  const emails = [
    { id: 'a', sender: 'sa', subject: 'ta', age_days: 40 },
    { id: 'b', sender: 'sb', subject: 'tb', age_days: 40 },   // old enough to trash
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
  const emails = [{ id: 'a', sender: 'sa', subject: 'ta', age_days: 40 }];
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
  assert.equal(staticData.runs['exec-1'].items.length, 1);
});

// ─── Tally ───────────────────────────────────────────────────────────────────

function tallyCtx(runResults, idCount, perRunLimit = 2000) {
  return {
    staticData: { runs: { 'exec-1': { startedAt: Date.now(), items: runResults } } },
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

test('Tally: moreRemain true on a full batch above 2,000 per run (regression: chain stopped after one run)', () => {
  const out = runNode('Tally', tallyCtx(
    Array.from({ length: 5000 }, () => ({ action: 'keep', label: 'Receipts' })),
    5000, 5000,
  ));
  assert.equal(out[0].json.moreRemain, true);
});

test('Tally: moreRemain false when the query returned a partial batch', () => {
  const out = runNode('Tally', tallyCtx(
    Array.from({ length: 500 }, () => ({ action: 'trash' })),
    500,
  ));
  assert.equal(out[0].json.moreRemain, false);
});

test('Tally drops its own accumulator for the next run', () => {
  const ctx = tallyCtx([{ action: 'trash' }], 1);
  runNode('Tally', ctx);
  assert.deepEqual(ctx.staticData.runs, {});
});

// ─── Overlapping runs (GCA-12) ───────────────────────────────────────────────

const HOUR = 3600000;
const guard = (staticData) => runNode('Skip if draining', { input: [{ json: {} }], staticData });

test('Skip if draining: a scheduled run is held back while a drain is active', () => {
  assert.equal(guard({ drainActiveAt: Date.now() - 2 * HOUR }).length, 0);
});

test('Skip if draining: proceeds with no drain, a finished drain, or a stale marker', () => {
  assert.equal(guard({}).length, 1);
  assert.equal(guard({ drainActiveAt: null }).length, 1);
  assert.equal(guard({ drainActiveAt: Date.now() - 25 * HOUR }).length, 1);   // DRAIN_STALE_HOURS = 24
});

test('Tally sets the drain marker when it re-chains and clears it on the final run', () => {
  const mid = tallyCtx([{ action: 'trash' }], 2000);
  runNode('Tally', mid);
  assert.ok(Date.now() - mid.staticData.drainActiveAt < 5000);
  const last = tallyCtx([{ action: 'trash' }], 10);
  last.staticData.drainActiveAt = Date.now() - HOUR;
  runNode('Tally', last);
  assert.equal(last.staticData.drainActiveAt, null);
});

test('Overlapping executions keep separate tallies (regression: shared runResults)', () => {
  const staticData = {};
  const constants = (executionId) => runNode('Constants', { staticData, executionId });
  const decide = (executionId, id, action) => runNode('Parse decisions', {
    ...pdContext({
      content: JSON.stringify({ decisions: [{ id, action, labels: ['Receipts'] }] }),
      ids: [id], batchEmails: [{ id, sender: 's', subject: 't', age_days: 40 }], staticData,
    }),
    executionId,
  });
  constants('A');
  decide('A', 'a1', 'keep');
  constants('B');                 // B starts mid-way through A: must not clear A
  decide('B', 'b1', 'trash');
  decide('A', 'a2', 'keep');
  const tally = (executionId) => runNode('Tally', {
    staticData, executionId,
    nodes: { Constants: [{ json: { perRunLimit: 2000, trashAgeDays: 30 } }], 'Extract IDs': [{ json: { id: 'x' } }] },
  })[0].json;
  const a = tally('A');
  assert.deepEqual([a.total, a.kept, a.trashed], [2, 2, 0]);
  const b = tally('B');
  assert.deepEqual([b.total, b.kept, b.trashed], [1, 0, 1]);
  assert.deepEqual(staticData.runs, {});
});

test('Constants prunes accumulators a crashed run left behind, but not a live one', () => {
  const staticData = { runResults: [{ action: 'trash' }], runs: {
    old: { startedAt: Date.now() - 3 * 86400000, items: [] },
    live: { startedAt: Date.now() - HOUR, items: [{ action: 'keep' }] },
  } };
  runNode('Constants', { staticData, executionId: 'new' });
  assert.deepEqual(Object.keys(staticData.runs).sort(), ['live', 'new']);
  assert.equal(staticData.runResults, undefined);
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

// ─── One-or-two-label contract (GCA-6) ──────────────────────────────────────

function pd2({ content, ids, age = 40 }) {
  const ctx = pdContext({ content, ids, batchEmails: ids.map((id) => ({ id, sender: 's', subject: 't', age_days: age })) });
  ctx.nodes['Build label index'] = [{ json: {
    labelNameToId: { Receipts: 'Label_5', Travel: 'Label_6', Taxes: 'Label_7' }, reviewedLabelId: 'Label_19',
  } }];
  ctx.nodes.Constants = [{ json: { validLabels: ['Receipts', 'Travel', 'Taxes'], trashAgeDays: 30, labelOnDeferred: [] } }];
  return ctx;
}
const byId = (out) => Object.fromEntries(out.map((o) => [o.json.id, o.json]));

test('Build prompt states the one-or-two-label contract with the do-not-hedge rule', () => {
  const p = runNode('Build prompt', {
    input: [{ json: { id: 'a', sender: 's', subject: 't', snippet: 'x' } }],
    nodes: { Constants: CONSTANTS },
  })[0].json.prompt;
  assert.ok(p.includes('One label is the default.'));
  assert.ok(p.includes('Never a third.'));
  assert.ok(p.includes('Do not use a second label to avoid choosing.'));
  assert.ok(p.includes('"labels": ["Travel", "Receipts"]'));
  assert.ok(!/single best-matching label/.test(p), 'old one-label wording is gone');
});

test('Parse decisions: a two-label keep resolves both label ids (first one drives routing)', () => {
  const out = byId(runNode('Parse decisions', pd2({
    ids: ['a'], content: JSON.stringify({ decisions: [
      { id: 'a', action: 'keep', labels: ['Travel', 'Receipts'] }] }),
  })));
  assert.deepEqual(out.a.labels, ['Travel', 'Receipts']);
  assert.deepEqual(out.a.labelIds, ['Label_6', 'Label_5']);
  assert.equal(out.a.label, 'Travel');
  assert.equal(out.a.labelId, 'Label_6');
});

test('Parse decisions: a legacy scalar label is still accepted', () => {
  const out = byId(runNode('Parse decisions', pd2({
    ids: ['a'], content: JSON.stringify({ decisions: [{ id: 'a', action: 'keep', label: 'Taxes' }] }),
  })));
  assert.deepEqual(out.a.labels, ['Taxes']);
  assert.deepEqual(out.a.labelIds, ['Label_7']);
});

test('Parse decisions: a third label is trimmed and an unknown one dropped, with errors', () => {
  const out = byId(runNode('Parse decisions', pd2({
    ids: ['a', 'b'], content: JSON.stringify({ decisions: [
      { id: 'a', action: 'keep', labels: ['Travel', 'Receipts', 'Taxes'] },
      { id: 'b', action: 'keep', labels: ['Receipts', 'Nope'] }] }),
  })));
  assert.deepEqual(out.a.labels, ['Travel', 'Receipts']);
  assert.deepEqual(out.b.labels, ['Receipts']);
  const errs = out.a.parseErrors.join('\n');
  assert.match(errs, /3 labels for a, kept first 2/);
  assert.match(errs, /dropped unknown label\(s\) for b: Nope/);
});

test('Parse decisions: eligible trash keeps its labels in the plan; regex fallback reads a labels array', () => {
  const content = 'oops {"id": "a", "action": "keep", "labels": ["Travel", "Receipts"]} '
    + '{"id": "b", "action": "trash", "labels": ["Taxes"]} not json';
  const out = byId(runNode('Parse decisions', pd2({ ids: ['a', 'b'], content })));
  assert.deepEqual(out.a.labels, ['Travel', 'Receipts']);
  assert.equal(out.b.action, 'trash');
  assert.deepEqual(out.b.labels, ['Taxes']);   // not applied: the trash branch stamps control labels only
});

test('Tally counts each label of a two-label keep and reports multi-labelled keeps', () => {
  const out = runNode('Tally', tallyCtx([
    { action: 'keep', labels: ['Travel', 'Receipts'], label: 'Travel' },
    { action: 'keep', labels: ['Receipts'], label: 'Receipts' },
    { action: 'keep', label: 'Receipts' },              // pre-GCA-6 shape
    { action: 'trash' },
  ], 4));
  const j = out[0].json;
  assert.equal(j.kept, 3);
  assert.equal(j.multiLabelled, 1);
  assert.match(j.body, /Kept \(labeled\): 3 \(1 with 2 labels\)/);
  assert.match(j.body, /Receipts: 3/);
  assert.match(j.body, /Travel: 1/);
});

// ─── Age gate (GCA-2) ────────────────────────────────────────────────────────
// trashAgeDays = 30: an email exactly 30 days old IS trash-evaluated (>=); one day
// younger is not. A deferred trash verdict is kept, gets LLM Categorized, and is
// NOT stamped LLM Reviewed, so the older_than arm brings it back when it ages.

function gate(emails, decisions, { labelOnDeferred = [] } = {}) {
  const ctx = pdContext({
    content: JSON.stringify({ decisions }),
    ids: emails.map((e) => e.id),
    batchEmails: emails.map((e) => ({ sender: 's', subject: 't', ...e })),
  });
  ctx.nodes['Build label index'][0].json.labelNameToId = {
    Receipts: 'Label_5', Politics: 'Label_8', Travel: 'Label_6' };
  ctx.nodes.Constants = [{ json: { validLabels: ['Receipts', 'Politics', 'Travel'], trashAgeDays: 30, labelOnDeferred } }];
  return Object.fromEntries(runNode('Parse decisions', ctx).map((o) => [o.json.id, o.json]));
}

test('Age gate boundary: 29 days defers, exactly 30 is trashed, 31 is trashed, unknown age defers', () => {
  const out = gate(
    [{ id: 'd29', age_days: 29 }, { id: 'd30', age_days: 30 }, { id: 'd31', age_days: 31 }, { id: 'dnull', age_days: null }],
    ['d29', 'd30', 'd31', 'dnull'].map((id) => ({ id, action: 'trash', labels: ['Receipts'] })),
  );
  assert.deepEqual([out.d29.action, out.d29.deferred, out.d29.stampReviewed], ['keep', true, false]);
  assert.deepEqual([out.d30.action, out.d30.deferred, out.d30.stampReviewed], ['trash', false, true]);
  assert.deepEqual([out.d31.action, out.d31.deferred, out.d31.stampReviewed], ['trash', false, true]);
  assert.deepEqual([out.dnull.action, out.dnull.deferred], ['keep', true]);
  for (const id of ['d29', 'd30', 'd31', 'dnull']) assert.equal(out[id].verdict, 'trash');
});

test('Age gate: a young keep is categorised but not stamped reviewed', () => {
  const out = gate([{ id: 'y', age_days: 5 }], [{ id: 'y', action: 'keep', labels: ['Receipts'] }]);
  assert.deepEqual([out.y.action, out.y.labelId, out.y.stampReviewed], ['keep', 'Label_5', false]);
});

test('Age gate: an old email that already has a verdict is not re-judged', () => {
  // Arrives via the categorisation arm (e.g. an upgrade before seeding): it is
  // categorised, but its trash verdict is not acted on a second time.
  const out = gate([{ id: 'o', age_days: 400, labelIds: ['Label_19'] }],
                   [{ id: 'o', action: 'trash', labels: ['Receipts'] }]);
  assert.deepEqual([out.o.action, out.o.deferred, out.o.stampReviewed], ['keep', true, false]);
});

test('Deferred trash gets no category label (filler suppressed) unless exempted per label', () => {
  const plain = gate([{ id: 'x', age_days: 3 }], [{ id: 'x', action: 'trash', labels: ['Receipts'] }]);
  assert.deepEqual(plain.x.labels, []);
  assert.equal(plain.x.labelId, null);                 // -> no-category branch, no Needs Review
  assert.equal(plain.x.suppressedLabel, 'Receipts');

  const exempt = gate([{ id: 'x', age_days: 3 }],
    [{ id: 'x', action: 'trash', labels: ['Politics', 'Receipts'] }], { labelOnDeferred: ['Politics'] });
  assert.deepEqual(exempt.x.labels, ['Politics']);     // per label: only the exempt one survives
  assert.deepEqual(exempt.x.labelIds, ['Label_8']);
  assert.equal(exempt.x.deferred, true);
  assert.equal(exempt.x.suppressedLabel, null);
});

test('A missing decision on old mail is stamped reviewed (no endless loop); on young mail it is not', () => {
  const out = gate([{ id: 'old', age_days: 90 }, { id: 'new', age_days: 2 }], []);
  assert.deepEqual([out.old.action, out.old.labelId, out.old.stampReviewed], ['keep', null, true]);
  assert.deepEqual([out.new.action, out.new.stampReviewed], ['keep', false]);
  assert.equal(out.old.deferred, false);               // -> gets Needs Review
});

test('Tally separates deferred trash and genuine failures from real keeps', () => {
  const ctx = tallyCtx([
    { action: 'keep', labels: ['Receipts'], label: 'Receipts', labelId: 'Label_5' },
    { action: 'keep', deferred: true, labels: [], label: null, labelId: null },
    { action: 'keep', deferred: true, labels: ['Politics'], label: 'Politics', labelId: 'Label_8' },
    { action: 'keep', labels: [], label: null, labelId: null },      // model failure
    { action: 'trash' },
  ], 5);
  ctx.nodes.Constants[0].json.trashAgeDays = 30;
  const j = runNode('Tally', ctx)[0].json;
  assert.deepEqual([j.kept, j.deferred, j.trashed, j.uncategorized], [2, 2, 1, 1]);
  assert.match(j.title, /2 kept, 2 deferred, 1 trashed of 5/);
  assert.match(j.body, /Deferred trash \(under 30 days, re-judged later\): 2/);
  assert.match(j.body, /Needs Review \(no usable label\): 1/);
  assert.match(j.body, /Politics: 1/);                 // exempt deferred label still counted
});
