from unittest.mock import Mock, patch

from aliyun.log import IndexConfig, IndexKeyConfig, IndexLineConfig

from extensions.logstore.aliyun_logstore import AliyunLogStore


def test_new_workflow_index_supports_account_analytics() -> None:
    store = object.__new__(AliyunLogStore)
    store.client = Mock()
    store.project_name = "test-project"

    with patch.object(store, "get_existing_index_config", return_value=None):
        store.ensure_index_config(AliyunLogStore.workflow_execution_logstore)

    store.client.create_index.assert_called_once()
    project, logstore, config = store.client.create_index.call_args.args
    assert (project, logstore) == ("test-project", AliyunLogStore.workflow_execution_logstore)
    actor = config.key_config_list["from_account_id"]
    assert actor.index_type == "text"
    assert actor.doc_value is True
    assert actor.token_list == AliyunLogStore.DEFAULT_TOKEN_LIST


def test_existing_workflow_index_adds_actor_without_losing_custom_settings() -> None:
    store = object.__new__(AliyunLogStore)
    store.client = Mock()
    store.project_name = "test-project"
    keys = store._get_workflow_execution_index_keys()
    keys.pop("from_account_id", None)
    custom_key = IndexKeyConfig(index_type="long", doc_value=False)
    keys["custom_metric"] = custom_key
    line_config = IndexLineConfig(token_list=[" "], case_sensitive=True, chinese=False)
    existing = IndexConfig(line_config=line_config, key_config_list=keys, scan_index=False)

    with patch.object(store, "get_existing_index_config", return_value=existing):
        store.ensure_index_config(AliyunLogStore.workflow_execution_logstore)

    store.client.update_index.assert_called_once()
    project, logstore, updated = store.client.update_index.call_args.args
    assert (project, logstore) == ("test-project", AliyunLogStore.workflow_execution_logstore)
    assert updated.key_config_list["from_account_id"].index_type == "text"
    assert updated.key_config_list["from_account_id"].doc_value is True
    assert updated.key_config_list["custom_metric"] is custom_key
    assert updated.line_config is line_config
    assert updated.scan_index is False
    assert "from_account_id" not in existing.key_config_list

    store.client.reset_mock()
    with patch.object(store, "get_existing_index_config", return_value=updated):
        store.ensure_index_config(AliyunLogStore.workflow_execution_logstore)
    store.client.update_index.assert_not_called()


def test_existing_compatible_actor_index_keeps_user_configuration() -> None:
    store = object.__new__(AliyunLogStore)
    keys = store._get_workflow_execution_index_keys()
    actor = IndexKeyConfig(index_type="text", case_sensitive=True, doc_value=True, token_list=[])
    existing = IndexConfig(key_config_list={**keys, "from_account_id": actor}, scan_index=True)

    merged, needs_update = store._merge_index_configs(existing, keys, AliyunLogStore.workflow_execution_logstore)

    assert needs_update is False
    assert merged.key_config_list["from_account_id"] is actor
