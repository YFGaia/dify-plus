# M08 read-only integration preflight

Independent Sol sub-agent `/root/integration_preflight_analysis` inspected current Git and committed evidence. This is preparation; M08 remains blocked by M06 and has not passed.

- M00 merge `948fefb69ae87abefa7e91a13968213696b3e310` is an ancestor of the current branch with the intended two parents. The four fork follow-up commits remain ancestors.
- M00 records exactly the predicted 91 conflict paths. Their original rows in `research/conflict-ownership.tsv` still say pending. The current 199-row owner table, 51-row host table and 155-row overlap table need final disposition/host/evidence mapping before M08 acceptance.
- `evidence/M04/handoff.md` records nine M04 hooks plus the M03 OAuth reference; integration still needs to verify actual reachability and evidence for all ten.
- M08's old blocked reason included already-passed M04/M05/M07; corrected to M06 only. M07 result top-level state was stale despite all six child results passing, including the independently reviewed manifest fix. Root reconciled it to source static acceptance only; deployable remains false.
- The implemented extension chain ends at `020_workflow_run_account`, after `019_webapp_auth_switch`. Design, runbook, target spec and V03 acceptance now use 020. Historical evidence and down_revision remain unchanged. Live migration validation remains outstanding.

Next M08 work: check final M01–M07 commits, reconcile all original 91 conflicts and auto-merge/host review records, verify four fork fixes and ten hooks, refresh generated/lock artifacts only under explicit ownership, scan unresolved index/conflict markers/orphans, then freeze a candidate with exact-path staging. V01/V02 must verify that candidate; this report does not substitute for those checks.
