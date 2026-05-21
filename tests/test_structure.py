"""Structural tests for the generated n8n workflow.

These guard the workflow *graph* and the generator: a future change that drops
a connection, renames a node without updating references, leaks an
environment-specific value, or forgets to regenerate gmail-cleanup.json will
fail CI here. Logic inside the Code nodes is covered by codenodes.test.mjs.
"""
import json
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
    "Mark reviewed (skip)", "Merge actions", "Tally", "ntfy gate",
    "ntfy: summary", "Re-chain gate", "Re-trigger next batch",
}


def _targets(node_name, output_index=None):
    """Names of nodes that `node_name` connects to (optionally one output)."""
    outputs = CONNECTIONS.get(node_name, {}).get("main", [])
    if output_index is not None:
        outputs = [outputs[output_index]] if output_index < len(outputs) else []
    return [link["node"] for output in outputs for link in output]


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
    # Switch outputs: 0 = trash, 1 = keep+label, 2 = skip.
    outputs = CONNECTIONS["Route action"]["main"]
    assert [o[0]["node"] for o in outputs] == [
        "Trash message", "Add label", "Mark reviewed (skip)",
    ]


def test_every_apply_branch_returns_to_the_merge():
    for branch_end in ("Mark reviewed (post-trash)", "Add label", "Mark reviewed (skip)"):
        assert "Merge actions" in _targets(branch_end)
    # Trash is always followed by its reviewed-stamp before merging.
    assert _targets("Trash message") == ["Mark reviewed (post-trash)"]


def test_tally_fans_out_to_both_gates_and_gates_lead_to_their_actions():
    assert set(_targets("Tally")) == {"ntfy gate", "Re-chain gate"}
    assert _targets("ntfy gate") == ["ntfy: summary"]
    assert _targets("Re-chain gate") == ["Re-trigger next batch"]


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


def test_gmail_query_excludes_the_reviewed_label():
    # The idempotency contract: processed mail must drop out of the candidate set.
    assert "-label:llm-reviewed" in _constants_payload()["gmailQuery"]


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


# ─── Workflow settings ───────────────────────────────────────────────────────

def test_settings_are_sane():
    settings = WORKFLOW["settings"]
    assert settings["executionOrder"] == "v1"
    assert settings.get("timezone")
