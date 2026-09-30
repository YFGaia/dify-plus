# Standard worker queue topology: source audit

2026-09-30, source-only inspection at starting HEAD `953cf1c1ef88258197c72b98e89fcd1dab85ac39`. No queue defaults or service definitions changed.

- `api/docker/entrypoint.sh` default queues include `workflow_based_app_execution` for both `DEPLOYMENT_EDITION=CLOUD` and self-hosted branches.
- `docker/docker-compose.dify-plus.yaml` has distinct `worker` and `worker-gaia` services. The former uses the entrypoint's main defaults; the latter explicitly selects `extend_high,extend_low` unless the operator overrides `CELERY_QUEUES`. Workflow-node billing tasks declare `extend_high`.
- The pre-merge fork commit `1c3368ed1584c4e9b6387a28334552d10c946ab4` already had the same split: main defaults included `workflow_based_app_execution`, and worker-gaia defaulted to `extend_high,extend_low`.
- Actual read-only container inventory of the loopback quota self-test project contained `worker-1`, but no `worker-gaia` service container. It was a selected-service rehearsal, not the full standard topology.

Conclusion: there is no demonstrated source queue-default merge regression. Independent runtime acceptance must start the standard `worker` plus `worker-gaia` from the newly built image, then verify active queues, registered billing tasks, actual task delivery/completion and positive usage/account reconciliation. Restoring an omitted standard service is distinct from a source queue fix; a custom temporary consumer is not evidence that the default deployment topology is complete.
