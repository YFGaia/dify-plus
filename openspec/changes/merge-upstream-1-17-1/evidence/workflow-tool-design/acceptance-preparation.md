# Traditional workflow Tool-node runtime preparation

2026-09-30. Readonly source investigation for tasks 29.10, B09 and O01 while the final complete image builds. This record does not mark those runtime gates passed. No source, DB, account, key or provider configuration changed. Inspected repository hashes are in `inspected-source.sha256`.

## Source contract and focused checks

The upstream tool execution owner moved to installed `graphon` 0.7 (`graphon.nodes.tool.tool_node.ToolNode`); Dify supplies `DifyToolNodeRuntime`, tool-provider/credential resolution and message/file/usage adapters. Looking for the old `api/core/workflow/nodes/tool/tool_node.py` is therefore insufficient.

The frontend creates new Tool nodes with `tool_node_version='2'`. `getConfiguredValue` and the shared form inputs store settings as structured constant/mixed/variable inputs; the runtime combines `tool_configurations` with typed `tool_parameters`, retains `credential_id`, and passes complete tenant/app/user scope to ToolManager. Legacy node version 1 without `tool_node_version` intentionally passes no variable pool and uses legacy scalar settings. The initially suspected scalar-settings loss in the variable-pool converter is therefore not an established regression: legacy and current constructors follow distinct documented paths. No speculative fix was made.

Draft sync row-locks/checks its unique hash and stores the graph; collaborative graph-only sync preserves independent features/variables. Publish copies the actual draft graph into a numbered immutable version and updates the App workflow pointer in the same controller transaction. The draft response only projects Agent-node binding data; ordinary Tool-node configuration remains part of the graph. Publishing validates plugin credential policy when enabled but does not prove that a provider invocation will succeed.

Existing, meaningful unit suites were run once:

```
UV_PROJECT_ENVIRONMENT=/private/tmp/dify-m02-python-complete uv run --project api --no-sync pytest -o addopts='' -p no:benchmark --timeout 60 \
  api/tests/unit_tests/core/workflow/nodes/tool/test_tool_node.py \
  api/tests/unit_tests/core/workflow/nodes/tool/test_tool_node_runtime.py \
  api/tests/unit_tests/core/workflow/nodes/tool/test_tool_node_invocation_behavior.py \
  api/tests/unit_tests/core/tools/test_tool_manager.py \
  api/tests/unit_tests/core/tools/workflow_as_tool/test_tool.py
```

115 passed, two existing warnings, 1.38 seconds (`tool-contract-tests.log`). Boundaries are patched/stubbed by these existing tests; this is not real plugin or HTTP invocation evidence.

```
UV_PROJECT_ENVIRONMENT=/private/tmp/dify-m02-python-complete uv run --project api --no-sync pytest -o addopts='' -p no:benchmark --timeout 60 \
  api/tests/unit_tests/services/test_workflow_service.py \
  -k 'sync_draft or publish_workflow or validate_workflow_credentials_should_check_tool or validate_workflow_credentials_should_skip_tool or get_published_workflow_by_id_rejects_foreign'
```

21 passed, 119 deselected, one existing warning, 1.07 seconds (`save-publish-tests.log`). No high-confidence new source defect was established. The following real runtime transitions remain necessary.

## 29.10 real save/refetch/publish/run

Use only the dedicated stack and legitimately created owner workspace. Prefer an actually installed, supported no-credential stock Tool such as Time/current_time **if** available in the real inventory. Discover the exact provider ID and live parameter schema; the unit-test shorthand `time` is not a substitute for a real installed plugin ID. If no suitable tool exists, install a supported stock package through the existing plugin product flow; do not fabricate a provider or stub its invocation.

Inventory routes are `/console/api/workspaces/current/tool-providers` and `/console/api/workspaces/current/tool-provider/builtin/<provider>/tools`. Summaries should include provider/tool ID, parameter name/form/type and installation status, never credential values.

1. Create a disposable traditional Workflow in Studio. In the real editor add Start → Tool → End. Choose the actual installed tool and configure one visible setting with an unmistakable literal acceptance marker; for a live time tool whose schema exposes a format setting, use a marker appended to its supported date format. Set End output to the Tool's actual text output through the variable picker.
2. Save through the normal editor/collaborative path. Capture successful save or YJS confirmation without secrets. GET `/apps/<app_id>/workflows/draft`; verify provider/tool IDs, setting value, `tool_node_version`, parameter binding, End output selector and edges. Reload the editor, reopen Tool and verify the configured setting and output mapping visibly return.
3. Run the draft through UI. Collect the actual workflow/node final statuses and output. Start, Tool and End must succeed; output must contain the configured marker and live tool result. An initial HTTP 200 or event stream alone is insufficient.
4. Publish through UI. GET `/apps/<app_id>/workflows/publish`; verify the published graph contains the same configuration and that the version/pointer is the one released. Execute the published WebApp/API through its legitimate access path and confirm matching marker/tool output with succeeded nodes.
5. Change the draft setting marker to a different value, save/refetch/reload and rerun. Before publishing again, the published graph/output must still use the first marker. Publish the changed draft and verify the published graph/output now uses the second. This establishes save versus publication behavior without editing database rows.
6. If the live schema includes optional numeric/boolean/static settings, choose valid 0/false values and repeat refetch/run to catch truthiness loss. Test a variable or mixed setting only when that actual parameter supports it, using a Start input and the editor's variable binding controls. Do not inject impossible schemas solely to make a test pass.

For a first failure record the earliest of UI setting → saved graph → refetched graph → published graph → selected provider/credential → plugin request → final node/output. Keep the first error before retries; source-only unit evidence cannot replace it.

## B09 billing / stop / restore / retry

A no-cost tool workflow proves tool configuration and execution, but cannot prove nonzero LLM billing. Use the separate legitimate nonzero-priced supported model batch and the existing N05 design for billing reconciliation.

- For a workflow exposed as a tool, run a simple supported child workflow first, then the parent Tool workflow. Confirm the child published version and parent/child trace/run linkage, user/tenant attribution and final outputs. `WorkflowTool.latest_usage` propagates child usage into Tool metadata, while fork billing tasks explicitly act on LLM node executions, not Tool nodes; reconcile actual LLM-node charges rather than assuming parent aggregated usage incurs a second charge.
- For stop: use the supported UI stop operation during an actual in-progress run; capture the task/run ID and final stopped status, persist completed-node usage, await ordinary billing delivery and reconcile it. The endpoint is `/apps/<app_id>/workflow-runs/tasks/<task_id>/stop`; stopping a no-op or zero-price workflow does not prove billing preservation.
- For restore: use the product published-version restore action and verify the resulting draft graph/config/variables by refetch; rerun and publish to establish that the restored Tool config is runnable, not merely returned by HTTP. The scoped existing tests also cover foreign workflow rejection; role/runtime ownership remains independently required.
- For retry: use an actually supported failure/retry behavior of the chosen node/provider, preserve the original failed execution and every retry attempt, and verify final status and charged completed LLM nodes. Do not simulate delivery by manually dispatching a billing task or modifying a DB row. Existing non-idempotent Celery redelivery and concurrency debt remains P4 and is not claimed fixed by these merge repairs.

## O01 standard runtime topology

Before tool/billing acceptance confirm all enabled candidate services use intended complete images/digests and no source overlays; record API/Web/worker/plugin/sandbox versions. Source default `worker` queues include `workflow_based_app_execution`; Compose additionally defines `worker-gaia` for `extend_high,extend_low`. Both standard consumers are needed. The old reduced 23000 stack omitted worker-gaia; no source default omission was established.

Read worker active queues and registered tasks without printing broker credentials; verify one real workflow request is consumed by the ordinary workflow worker and its actual completed LLM billing task by worker-gaia. Capture task delivery/result and absence of stuck acceptance tasks, without consuming or modifying held user tasks. Plugin success and Tool node outputs must come from the real plugin daemon; a tool list or daemon health check alone does not establish invocation compatibility. Standard sandbox/code-node execution is a distinct actual invocation check. Verify WebSocket 101/interactive editor sync and any enabled Ingress path without new 502. API/worker/beat automatic migrations must remain disabled; migrations run only through the explicit single-executor service.
