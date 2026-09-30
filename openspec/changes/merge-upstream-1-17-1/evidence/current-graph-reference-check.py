"""Static planning/evidence checks; does not run product or business tests."""
from pathlib import Path
import hashlib
import json
import re

change = Path(__file__).resolve().parents[1]
repo = change.parents[2]
graph = json.loads((change / "execution-graph.json").read_text())
nodes = {node["id"]: node for node in graph["nodes"]}
assert len(nodes) == len(graph["nodes"])
visiting, visited = set(), set()
def walk(node_id):
    assert node_id not in visiting, node_id
    if node_id in visited:
        return
    visiting.add(node_id)
    for dependency in nodes[node_id]["dependencies"]:
        assert dependency in nodes, (node_id, dependency)
        walk(dependency)
    visiting.remove(node_id)
    visited.add(node_id)
for node_id in nodes:
    walk(node_id)
for node in nodes.values():
    if node["status"] == "passed":
        for reference in node.get("evidence", []):
            assert (change / reference).exists() or (repo / reference).exists(), reference
source = nodes["N01"]["source_checkpoint"]
assert source["status"].startswith("development_and_independent_source_review_passed")
assert source["targeted_tests"] == 52
review = json.loads((change / "evidence/N01/independent-source-review.json").read_text())
assert review["verdict"] == "No issues found." and review["source_sha256"] == source["owned_files"]
advanced = {relative for node in nodes.values() if node["id"] in ["N07", "N08", "N09", "N10"] for relative in node.get("owned_conflict_paths", [])}
for relative, expected_hash in source["owned_files"].items():
    if relative in advanced:
        assert any(row["path"] == relative and row["sha256"] == expected_hash for row in json.loads((change / "evidence/N02/context-file-manifest.json").read_text())), relative
        continue
    assert hashlib.sha256((repo / relative).read_bytes()).hexdigest() == expected_hash, relative
if nodes["N02"]["status"] == "passed":
    frozen = json.loads((change / "evidence/N02/result.json").read_text())
    inventory = json.loads((change / "evidence/N02/context-file-manifest.json").read_text())
    digest = hashlib.sha256()
    for row in inventory:
        digest.update((row["path"] + "\0" + row["sha256"] + "\n").encode())
    assert digest.hexdigest() == frozen["candidate"]["context_manifest_sha256"]
    assert len(inventory) == frozen["candidate"]["file_count"] == 4314
    immutable_inventory = {row["path"]: row["sha256"] for row in inventory}
    for entry in frozen["candidate"]["overlays"]:
        assert immutable_inventory[entry["path"]] == entry["sha256"]
        if entry["path"] not in advanced:
            assert hashlib.sha256((repo / entry["path"]).read_bytes()).hexdigest() == entry["sha256"]
    for reference in frozen["independent_reviews"]:
        assert json.loads((change / reference).read_text())["verdict"] == "No issues found."
if "N10" in nodes and nodes["N10"].get("source_checkpoint"):
    frozen10 = json.loads((change / "evidence/provider-packaging-regression/latest-source-manifest.json").read_text())
    headlock_proof = json.loads((change / "evidence/N10/head-lock-full-image-runtime-2026-09-30.json").read_text())
    for entry in frozen10["overlays"]:
        actual_hash = hashlib.sha256((repo / entry["path"]).read_bytes()).hexdigest()
        if entry["path"] == "api/uv.lock":
            # The retained dirty candidate and the committed release lock are distinct,
            # independently built inputs; clean code-ready checkouts must accept the
            # actual verified HEAD-lock artifact rather than require user's unstaged lock.
            assert actual_hash in {entry["sha256"], headlock_proof["runtime_lock_sha256"]}, entry["path"]
        else:
            assert actual_hash == entry["sha256"], entry["path"]
assert nodes["N02"]["dependencies"] == ["N00"]
assert nodes["N02"]["source_input_requirements"]["source_evidence"] == "evidence/N01/independent-source-review.json"
assert {"N03", "N04", "N01"}.issubset(nodes["N05"]["dependencies"])
assert {"N05", "N07"}.issubset(nodes["N06"]["dependencies"])
assert "N07" in nodes["N05"]["dependencies"]
assert nodes["N07"]["dependencies"] == ["N03"]
def scene_ids(filename):
    return re.findall(r"^\| ([GBKFDO]\d\d)\s", (change / filename).read_text(), re.M)
spec_ids = scene_ids("verification-matrix.md")
assert len(spec_ids) == 29 and spec_ids == scene_ids("acceptance-ledger-2026-09-30.md")
assert len(re.findall(r"^- \[[ x]\] 30\.\d+ ", (change / "tasks.md").read_text(), re.M)) == 29
summary = json.loads((change / "evidence/merge-completion-audit-summary-2026-09-30.json").read_text())
assert summary["scene_count"] == 29
assert len(summary["locally_progressable_ids"]) == summary["locally_progressable_count"] == 24
assert len(summary["historical_deferred_ids"]) == summary["historical_deferred_count"] == 4
assert len(summary["production_ids"]) == summary["production_count"] == 1
assert set(spec_ids) == set(summary["locally_progressable_ids"] + summary["historical_deferred_ids"] + summary["production_ids"])
state = json.loads((change / "acceptance-ledger-state-2026-09-30.json").read_text())
assert [row["id"] for row in state["scenes"]] == spec_ids
assert sum(state["counts"].values()) == 29
assert sum(row["scope"] == "local-current" for row in state["scenes"]) == 24
assert sum(row["scope"] == "historical-deferred" for row in state["scenes"]) == 4
assert sum(row["scope"] == "production-gated" for row in state["scenes"]) == 1
for row in state["scenes"]:
    assert all((change / ref).exists() for q in row.get("required_subchecks", []) for ref in q.get("evidence", [])), row["id"]
assert sum(len(r.get("remaining_local", [])) for r in state["scenes"]) == state["completion_summary"]["local_missing_subcheck_count"]
print("PASS: DAG, passed references, N01 exact reviewed source milestone, N02 complete durable frozen source inventory, N02/N05/N06 dependencies and29 scene IDs /24 local /4 historical /1 production")
