"""Structural tests for the generated n8n workflow.

These guard the workflow *graph* and the generator: a future change that drops
a connection, renames a node without updating references, leaks an
environment-specific value, or forgets to regenerate gmail-cleanup.json will
fail CI here. Logic inside the Code nodes is covered by codenodes.test.mjs.
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOW_JSON = REPO / "gmail-cleanup.json"

WORKFLOW = json.loads(WORKFLOW_JSON.read_text())
NODES = {n["name"]: n for n in WORKFLOW["nodes"]}
CONNECTIONS = WORKFLOW["connections"]

EXPECTED_NODES = {
    "Weekly schedule", "Re-chain webhook", "Constants", "List labels",
    "Build label index", "List messages", "Extract IDs", "Get metadata",
    "Parse metadata", "Batch (20)", "Build prompt", "Ask LLM", "Parse decisions",
    "Route action", "Trash message", "Mark reviewed (post-trash)", "Add label",
    "Mark needs-review", "Merge actions", "Tally", "ntfy gate",
    "ntfy: summary", "Re-chain gate", "Re-trigger next batch",
}


def _targets(node_name, output_index=None):
    """Names of nodes that `node_name` connects to (optionally one output)."""
    outputs = CONNECTIONS.get(node_name, {}).get("main", [])
    if output_index is not None:
        outputs = [outputs[output_index]] if output_index < len(outputs) else []
    return [link["node"] for output in outputs for link in output]


def _build_variant(tmp_path, **settings):
    """Generate the workflow with some configuration constants overridden.

    Runs a copy of build_workflow.py (plus config/) in tmp_path with each
    `NAME = value` assignment rewritten, so build-time switches can be tested
    without touching the committed gmail-cleanup.json."""
    shutil.copytree(REPO / "config", tmp_path / "config")
    script = (REPO / "build_workflow.py").read_text()
    for name, value in settings.items():
        script, n = re.subn(rf"^{name}\s*=.*$", f"{name} = {value!r}", script,
                            count=1, flags=re.M)
        assert n == 1, f"no top-level {name} = ... in build_workflow.py"
    (tmp_path / "build_workflow.py").write_text(script)
    subprocess.run([sys.executable, "build_workflow.py"], cwd=tmp_path,
                   check=True, capture_output=True)
    return json.loads((tmp_path / "gmail-cleanup.json").read_text())


# ─── Generator + drift ───────────────────────────────────────────────────────

def test_generator_runs_and_produces_valid_json():
    result = subprocess.run(
        [sys.executable, "build_workflow.py"], cwd=REPO,
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    wf = json.loads(WORKFLOW_JSON.read_text())
    assert wf["nodes"] and wf["connections"]


def test_committed_json_is_up_to_date():
    """gmail-cleanup.json must match a fresh regeneration (it is deterministic)."""
    committed = WORKFLOW_JSON.read_bytes()
    subprocess.run([sys.executable, "build_workflow.py"], cwd=REPO, check=True)
    regenerated = WORKFLOW_JSON.read_bytes()
    WORKFLOW_JSON.write_bytes(committed)  # leave the tree clean
    assert regenerated == committed, (
        "gmail-cleanup.json is stale — run `python build_workflow.py` and commit."
    )


# ─── Node set ────────────────────────────────────────────────────────────────

def test_exact_expected_node_set():
    assert set(NODES) == EXPECTED_NODES


def test_every_code_node_declares_an_explicit_run_mode():
    # n8n 2.x's default Code-node mode is not stable across versions.
    for node in WORKFLOW["nodes"]:
        if node["type"] == "n8n-nodes-base.code":
            assert node["parameters"].get("mode") in (
                "runOnceForAllItems", "runOnceForEachItem",
            ), f"{node['name']} has no explicit Code-node mode"


# ─── Connection integrity ────────────────────────────────────────────────────

def test_all_connections_reference_existing_nodes():
    for source, conn in CONNECTIONS.items():
        assert source in NODES, f"connection from unknown node {source!r}"
        for output in conn.get("main", []):
            for link in output:
                assert link["node"] in NODES, (
                    f"{source!r} connects to unknown node {link['node']!r}"
                )


def test_both_triggers_enter_the_pipeline_at_constants():
    for trigger in ("Weekly schedule", "Re-chain webhook"):
        assert "Constants" in _targets(trigger), (
            f"{trigger!r} must feed Constants"
        )


def test_missing_label_check_runs_before_any_mail_is_fetched():
    """Build label index throws when Gmail lacks a label the workflow applies.
    That only protects the mailbox if it runs BEFORE List messages. Constants
    fans out to both branches in parallel, so the guarantee rests on n8n's v1
    execution order (parallel branches run depth-first, top to bottom by
    canvas position) and on the throw not being swallowed. Moving a node on
    the canvas, or adding continueOnFail/onError, would silently undo it."""
    assert WORKFLOW["settings"].get("executionOrder") == "v1"
    assert set(_targets("Constants")) >= {"List labels", "List messages"}
    assert _targets("List labels") == ["Build label index"]
    labels_y = NODES["List labels"]["position"][1]
    messages_y = NODES["List messages"]["position"][1]
    assert labels_y < messages_y, "label branch must sit above the message branch"
    for name in ("List labels", "Build label index"):
        assert not NODES[name].get("continueOnFail"), name
        assert NODES[name].get("onError") in (None, "stopWorkflow"), name
    assert "throw new Error" in NODES["Build label index"]["parameters"]["jsCode"]


def test_batch_loop_is_intact():
    # SplitInBatches output 0 = done -> Tally; output 1 = loop -> Build prompt.
    assert _targets("Batch (20)", 0) == ["Tally"]
    assert _targets("Batch (20)", 1) == ["Build prompt"]
    # The loop body must return to Batch (20).
    assert "Batch (20)" in _targets("Merge actions")


def test_classify_chain_is_in_order():
    assert _targets("Build prompt") == ["Ask LLM"]
    assert _targets("Ask LLM") == ["Parse decisions"]
    assert _targets("Parse decisions") == ["Route action"]


def test_route_action_has_three_branches_in_order():
    # Switch outputs: 0 = trash, 1 = keep+label, 2 = no category (deferred / needs review).
    outputs = CONNECTIONS["Route action"]["main"]
    assert [o[0]["node"] for o in outputs] == [
        "Trash message", "Add label", "Mark needs-review",
    ]


def test_every_apply_branch_returns_to_the_merge():
    for branch_end in ("Mark reviewed (post-trash)", "Add label", "Mark needs-review"):
        assert "Merge actions" in _targets(branch_end)
    # Trash is always followed by its reviewed-stamp before merging.
    assert _targets("Trash message") == ["Mark reviewed (post-trash)"]


def test_tally_fans_out_to_both_gates_and_gates_lead_to_their_actions():
    assert set(_targets("Tally")) == {"ntfy gate", "Re-chain gate"}
    assert _targets("ntfy gate") == ["ntfy: summary"]
    assert _targets("Re-chain gate") == ["Re-trigger next batch"]


def test_empty_ntfy_topic_leaves_the_ntfy_nodes_out(tmp_path):
    """NTFY_TOPIC = "" disables notifications. It used to leave the node in
    place, POSTing every summary to the bare server URL (https://ntfy.sh/)."""
    wf = _build_variant(tmp_path, NTFY_TOPIC="")
    names = {n["name"] for n in wf["nodes"]}
    assert names == EXPECTED_NODES - {"ntfy gate", "ntfy: summary"}
    assert "ntfy" not in json.dumps(wf["connections"])
    tally = [link["node"] for out in wf["connections"]["Tally"]["main"] for link in out]
    assert tally == ["Re-chain gate"]


def test_a_set_ntfy_topic_posts_to_that_topic():
    payload_url = NODES["ntfy: summary"]["parameters"]["url"]
    assert payload_url == "https://ntfy.sh/change-me-to-a-private-topic"
    assert _targets("Tally") == ["ntfy gate", "Re-chain gate"]


def test_list_messages_fetches_enough_pages_for_any_per_run_limit(tmp_path):
    """maxRequests was fixed at 4 (2,000 IDs), so a PER_RUN_LIMIT above 2,000
    could never see a full batch and the re-chain stopped after one run."""
    pages = NODES["List messages"]["parameters"]["options"]["pagination"]["pagination"]
    assert pages["maxRequests"] * 500 >= _constants_payload()["perRunLimit"]
    for limit, expected in ((5000, 10), (1200, 3)):
        sub = tmp_path / str(limit)
        sub.mkdir()
        wf = _build_variant(sub, PER_RUN_LIMIT=limit)
        node = next(n for n in wf["nodes"] if n["name"] == "List messages")
        assert node["parameters"]["options"]["pagination"]["pagination"]["maxRequests"] == expected


def test_every_gmail_call_retries():
    """Including the two list calls: one transient 5xx/429 there used to fail
    the whole run."""
    for node in WORKFLOW["nodes"]:
        if "oAuth2Api" in node.get("credentials", {}):
            assert node.get("retryOnFail") is True, node["name"]
            assert node.get("maxTries", 0) >= 3, node["name"]


# ─── Config embedding ────────────────────────────────────────────────────────

def _constants_payload():
    """The JSON object the Constants Code node returns."""
    code = NODES["Constants"]["parameters"]["jsCode"]
    start = code.index("return [{ json: ") + len("return [{ json: ")
    end = code.rindex(" }];")
    return json.loads(code[start:end])


def test_constants_embeds_rules_and_label_catalog():
    payload = _constants_payload()
    rules = (REPO / "config" / "rules.md").read_text()
    labels = yaml.safe_load((REPO / "config" / "labels.yaml").read_text())
    existing = list(labels.get("existing") or [])
    auto_create = dict(labels.get("auto_create") or {})

    assert payload["rulesMd"].strip() == rules.strip()
    assert payload["existingLabels"] == existing
    assert payload["autoCreateLabels"] == auto_create
    # validLabels must be exactly the union the prompt offers the model.
    assert payload["validLabels"] == existing + list(auto_create)


def test_gmail_query_has_both_parenthesised_arms():
    """The idempotency contract, for the two-pass design: arm 1 drops categorised
    mail, arm 2 drops mail with a verdict. Both arms MUST be parenthesised: the
    unparenthesised `-label:"A" OR (...)` parses but returns the first arm alone,
    which would silently disable trashing."""
    p = _constants_payload()
    q = p["gmailQuery"]
    assert q == (f'(-label:"{p["categorizedLabelName"]}") OR '
                 f'(older_than:{p["trashAgeDays"]}d -label:"{p["reviewedLabelName"]}")')
    assert p["trashAgeDays"] > 0 and isinstance(p["labelOnDeferred"], list)


# ─── Sanitization (open-source repo must stay clean) ─────────────────────────

def test_no_internal_infrastructure_values_leak():
    """The exported workflow must not carry host- or environment-specific data."""
    blob = json.dumps(WORKFLOW)
    assert "svc.cluster.local" not in blob, "internal Kubernetes hostname leaked"
    assert "/home/" not in blob and "/Users/" not in blob, "local filesystem path leaked"


def test_credentials_are_placeholders_not_real_ids():
    """Every Gmail node must reference the placeholder credential, never a real one."""
    cred_ids = {
        node["credentials"]["oAuth2Api"]["id"]
        for node in WORKFLOW["nodes"]
        if "oAuth2Api" in node.get("credentials", {})
    }
    assert cred_ids == {"REPLACE_WITH_YOUR_GMAIL_OAUTH2_CREDENTIAL_ID"}, (
        f"a real n8n credential ID is baked into the workflow: {cred_ids}"
    )


def test_ask_llm_sends_no_credential_by_default():
    node = NODES["Ask LLM"]
    assert "credentials" not in node
    assert "authentication" not in node["parameters"]


def test_llm_api_key_uses_its_own_header_auth_credential(tmp_path):
    """Keyed endpoints (the OpenAI API) 401'd: Ask LLM sent no Authorization
    header. LLM_USE_API_KEY switches it to a Header Auth credential, a separate
    placeholder from the Gmail one so picking either never rewires the other."""
    wf = _build_variant(tmp_path, LLM_USE_API_KEY=True)
    node = next(n for n in wf["nodes"] if n["name"] == "Ask LLM")
    assert node["parameters"]["authentication"] == "genericCredentialType"
    assert node["parameters"]["genericAuthType"] == "httpHeaderAuth"
    assert node["credentials"] == {"httpHeaderAuth": {
        "id": "REPLACE_WITH_YOUR_LLM_API_KEY_CREDENTIAL_ID", "name": "LLM API key"}}
    # Nothing else changes.
    others = [n for n in wf["nodes"] if n["name"] != "Ask LLM"]
    assert others == [n for n in WORKFLOW["nodes"] if n["name"] != "Ask LLM"]


# ─── Workflow settings ───────────────────────────────────────────────────────

def test_settings_are_sane():
    settings = WORKFLOW["settings"]
    assert settings["executionOrder"] == "v1"
    assert settings.get("timezone")


def test_llm_request_disables_thinking_by_default():
    """Reasoning models return EMPTY content (finish_reason length) unless
    thinking is disabled; the flag rides in Constants so endpoints that reject
    unknown fields (the OpenAI API) can turn it off in one place."""
    body = NODES["Ask LLM"]["parameters"]["jsonBody"]
    assert "chat_template_kwargs: { enable_thinking: false }" in body
    assert "json.disableThinking" in body
    assert '"disableThinking": true' in NODES["Constants"]["parameters"]["jsCode"]


def test_add_label_applies_every_category_label_plus_reviewed():
    body = NODES["Add label"]["parameters"]["jsonBody"]
    assert "...($json.labelIds" in body and "$json.reviewedLabelId" in body


def test_apply_branches_stamp_the_age_gate_labels():
    """Reviewed only when a verdict was acted on; Categorized always; Needs Review
    only for genuine failures, never for deferred trash."""
    add = NODES["Add label"]["parameters"]["jsonBody"]
    assert "$json.categorizedLabelId" in add and "$json.stampReviewed ? [$json.reviewedLabelId]" in add
    nr = NODES["Mark needs-review"]["parameters"]["jsonBody"]
    assert "[$json.categorizedLabelId]" in nr
    assert "$json.deferred ? [] : [$json.needsReviewLabelId]" in nr
    assert "$json.stampReviewed ? [$json.reviewedLabelId]" in nr
    post = NODES["Mark reviewed (post-trash)"]["parameters"]["jsonBody"]
    assert "reviewedLabelId" in post and "categorizedLabelId" in post


def test_label_index_requires_all_three_control_labels():
    p = _constants_payload()
    assert (p["reviewedLabelName"], p["categorizedLabelName"], p["needsReviewLabelName"]) == (
        "LLM Reviewed", "LLM Categorized", "Needs Review")
    js = NODES["Build label index"]["parameters"]["jsCode"]
    assert "c.reviewedLabelName, c.categorizedLabelName, c.needsReviewLabelName" in js
