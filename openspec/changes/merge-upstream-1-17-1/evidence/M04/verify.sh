#!/bin/sh
# Run from repository root. No dependency synchronization or real services.
set -eu
export UV_PROJECT_ENVIRONMENT=/private/tmp/dify-m02-python-complete
export UV_CACHE_DIR=/private/tmp/dify-m02-uv-cache
uv run --project api --no-sync pytest -q --no-cov \
  api/tests/unit_tests/controllers/service_api/test_billing_extend.py \
  api/tests/unit_tests/controllers/service_api/test_wraps.py \
  api/tests/unit_tests/controllers/console/test_apikey.py \
  api/tests/unit_tests/core/app/test_billing_hooks_extend.py \
  api/tests/unit_tests/core/app/apps/advanced_chat/test_generate_task_pipeline_core.py \
  api/tests/unit_tests/core/app/apps/workflow/test_generate_task_pipeline_core.py \
  api/tests/unit_tests/core/app/apps/chat/test_app_generator_and_runner.py \
  api/tests/unit_tests/core/app/workflow/test_persistence_layer.py \
  api/tests/unit_tests/core/app/task_pipeline/test_easy_ui_based_generate_task_pipeline_core.py \
  api/tests/unit_tests/core/memory/test_token_buffer_memory.py \
  api/tests/unit_tests/tasks/extend/test_update_account_money_task_extend.py \
  api/tests/unit_tests/services/test_dataset_api_key_service.py \
  api/tests/unit_tests/controllers/test_swagger.py
