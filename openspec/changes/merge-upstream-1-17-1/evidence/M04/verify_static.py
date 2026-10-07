"""Run from repo root via uv run --project api --no-sync python <this file>."""
import ast
import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path.cwd()
OUT = Path(__file__).resolve().parent
INPUT = "e83bdfe536eb114aa5dee17ddec539d9a71ab99e"
UPSTREAM = "8387590ace4a094de812b7847fc6a4c3a27cd52b"


def git_file(revision, path):
    return subprocess.check_output(["git", "show", f"{revision}:{path}"], text=True)


def definition(text, name):
    return ast.dump(next(n for n in ast.walk(ast.parse(text)) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name), include_attributes=False)


checks = {}
for name in ["cloud_edition_billing_resource_check", "cloud_edition_billing_knowledge_limit_check", "cloud_edition_billing_rate_limit_check", "validate_dataset_token", "validate_and_get_api_token"]:
    path = "api/controllers/service_api/wraps.py"
    checks[name + "_matches_upstream"] = definition(Path(path).read_text(), name) == definition(git_file(UPSTREAM, path), name)
for path in ["api/controllers/console/datasets/datasets.py", "api/controllers/web/completion.py", "api/events/event_handlers/update_account_money_when_messaeg_created_extend.py", "api/core/app/workflow/layers/persistence.py", "api/core/memory/token_buffer_memory.py", "api/core/app/apps/base_app_runner.py", "api/extensions/ext_celery.py"]:
    checks[path + "_unchanged"] = Path(path).read_text() == git_file(INPUT, path)
for path in ["api/uv.lock", "pnpm-lock.yaml", "api/controllers/console/auth/oauth.py", "api/services/account_oauth_gateway_extend.py"]:
    checks[path + "_unchanged"] = Path(path).read_text() == git_file(INPUT, path)
assert all(checks.values()), checks
callers = []
for path in sorted(Path("api/controllers/service_api").rglob("*.py")):
    for cls in (n for n in ast.parse(path.read_text()).body if isinstance(n, ast.ClassDef)):
        for method in (n for n in cls.body if isinstance(n, ast.FunctionDef)):
            for deco in method.decorator_list:
                fn = deco.func if isinstance(deco, ast.Call) else deco
                if isinstance(fn, ast.Name) and fn.id == "validate_app_token":
                    params = [p.arg for p in method.args.args + method.args.kwonlyargs]
                    callers.append({"path": str(path), "callable": f"{cls.name}.{method.name}", "line": method.lineno, "parameters": params, "api_token_injected": "api_token" in params or method.args.kwarg is not None, "fetch_user": ast.unparse(deco), "owner": "M04"})
assert len(callers) >= 30
(OUT / "callers.json").write_text(json.dumps(callers, ensure_ascii=False, indent=2) + "\n")
changed = subprocess.check_output(["git", "diff", INPUT, "--name-only"], text=True).splitlines()
owned = [str(p) for p in Path("api/tests/unit_tests").rglob("*billing*extend.py")]
source_paths = sorted(set([p for p in changed + owned if p.endswith(".py")] + [c["path"] for c in callers] + [key.removesuffix("_unchanged") for key in checks if key.endswith("_unchanged")]))
# Include every test named in the exact verification command, plus preserved hook sources.
source_paths += [word.rstrip("\\") for word in (OUT / "verify.sh").read_text().split() if word.startswith("api/") and word.endswith(".py")]
source_paths += [f"api/core/app/apps/{mode}/app_generator.py" for mode in ["chat", "agent_chat", "completion", "advanced_chat", "workflow"]]
source_paths += ["api/core/app/apps/workflow/generate_task_pipeline.py", "api/core/app/task_pipeline/easy_ui_based_generate_task_pipeline.py", "api/tasks/extend/update_account_money_when_workflow_node_execution_created_extend.py"]
source_paths += [str(p) for p in Path("api/schedule").glob("*quota*extend.py")]
manifest = {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in sorted(set(source_paths))}
(OUT / "verified-inputs.json").write_text(json.dumps({"input_head": INPUT, "upstream": UPSTREAM, "head_at_verification": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(), "sha256": manifest}, indent=2) + "\n")
(OUT / "static-scope.json").write_text(json.dumps({"checks": checks, "callers": len(callers), "all_passed": True, "production_changed_paths": [p for p in changed if p.startswith("api/") and not p.startswith("api/tests/")], "limits": ["Signature probe executes actual decorator with real SQLite quota and attribution, endpoint business bodies are covered by their existing tests.", "No dataset quota or total token limit invented; accumulated_quota is usage."]}, ensure_ascii=False, indent=2) + "\n")
print(f"{len(checks)} immutable-boundary checks passed; {len(callers)} callers; {len(manifest)} source/test/lock hashes")
