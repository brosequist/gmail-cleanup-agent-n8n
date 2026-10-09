// Minimal n8n Code-node test harness.
//
// It loads gmail-cleanup.json, pulls the `jsCode` of a node by name, and runs
// it with mocked n8n globals ($input, $json, $(), $getWorkflowStaticData,
// $execution).
// This tests the EXACT code that gets imported into n8n, not a copy.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const REPO = join(dirname(fileURLToPath(import.meta.url)), '..');
const WORKFLOW = JSON.parse(readFileSync(join(REPO, 'gmail-cleanup.json'), 'utf8'));

/** Raw jsCode string for a Code node, by node name. */
export function jsCodeOf(nodeName) {
  const node = WORKFLOW.nodes.find((n) => n.name === nodeName);
  if (!node) throw new Error(`no node named ${JSON.stringify(nodeName)}`);
  if (node.type !== 'n8n-nodes-base.code') {
    throw new Error(`node ${JSON.stringify(nodeName)} is not a Code node`);
  }
  return node.parameters.jsCode;
}

function wrap(items) {
  return { all: () => items, first: () => items[0] };
}

/**
 * Execute a Code node's JS against a mocked n8n runtime.
 *
 * @param {string} nodeName               node whose jsCode to run
 * @param {object} ctx
 * @param {Array}  ctx.input              items for $input.all()/.first()
 * @param {object} ctx.json               value for $json (runOnceForEachItem)
 * @param {object} ctx.nodes              map of nodeName -> items array, for $('...')
 * @param {object} ctx.staticData         object returned by $getWorkflowStaticData
 * @param {string} ctx.executionId        value of $execution.id
 * @returns whatever the node code returns (array or single item)
 */
export function runNode(nodeName, { input = [], json, nodes = {}, staticData = {}, executionId = 'exec-1' } = {}) {
  const $input = wrap(input);
  const $ = (name) => {
    if (!(name in nodes)) {
      throw new Error(`test did not mock $(${JSON.stringify(name)})`);
    }
    return wrap(nodes[name]);
  };
  const $getWorkflowStaticData = () => staticData;
  // n8n Code nodes use a top-level `return`, which is legal in a Function body.
  const $execution = { id: executionId };
  const fn = new Function('$input', '$json', '$', '$getWorkflowStaticData', '$execution', jsCodeOf(nodeName));
  return fn($input, json, $, $getWorkflowStaticData, $execution);
}

export { WORKFLOW };
