from copy import deepcopy
from unittest.mock import Mock, patch

import pytest
from aliyun.log import IndexConfig, IndexKeyConfig, IndexLineConfig
from aliyun.log.index_config import IndexJsonKeyConfig

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


@pytest.mark.parametrize(
    ("actor_type", "doc_value"),
    [(None, False), ("text", False), ("json", True), ("json", False), ("long", True), ("double", False)],
)
@pytest.mark.parametrize("with_line_config", [True, False])
@pytest.mark.parametrize("reduce_lists", ["white", "black", "both"])
def test_reconcile_preserves_complete_index_config(
    actor_type: str | None, doc_value: bool, with_line_config: bool, reduce_lists: str
) -> None:
    store = object.__new__(AliyunLogStore)
    store.client = Mock()
    store.project_name = "test-project"
    required_keys = store._get_workflow_execution_index_keys()
    required_before = {name: deepcopy(config.to_json()) for name, config in required_keys.items()}
    keys = dict(required_keys)
    if actor_type is None:
        del keys["from_account_id"]
    else:
        keys["from_account_id"] = IndexKeyConfig(
            index_type=actor_type,
            doc_value=doc_value,
            case_sensitive=True,
            token_list=["|", "~"],
            chinese=False,
            alias="account_actor",
            json_key_config=IndexJsonKeyConfig(index_all=False, max_depth=3) if actor_type == "json" else None,
        )
    custom_json = IndexJsonKeyConfig(index_all=False, max_depth=4)
    custom_json.add_key("nested.count", "long", doc_value=True, alias="count")
    keys["custom_payload"] = IndexKeyConfig(index_type="json", json_key_config=custom_json, token_list=[";"])
    keys["custom_metric"] = IndexKeyConfig(index_type="long", doc_value=False, alias="metric")
    line_config = (
        IndexLineConfig(
            token_list=["|"], case_sensitive=True, chinese=False, auto_key_detect=True, auto_key_count_limit=37
        )
        if with_line_config
        else None
    )
    existing = IndexConfig(
        ttl=37,
        line_config=line_config,
        key_config_list=keys,
        all_keys_config=IndexKeyConfig(token_list=["~"], case_sensitive=True, doc_value=True, chinese=False),
        log_reduce=True,
        scan_index=False,
    )
    existing.modify_time = 1234567890
    existing.set_docvalue_max_text_len(4096)
    if reduce_lists in {"white", "both"}:
        existing.set_log_reduce_white_list(["custom_payload"])
    if reduce_lists in {"black", "both"}:
        existing.set_log_reduce_black_list(["custom_metric"])
    before = deepcopy(existing.to_json())
    expected = deepcopy(before)
    if actor_type is None:
        expected["keys"]["from_account_id"] = deepcopy(required_before["from_account_id"])
    else:
        expected_actor = expected["keys"]["from_account_id"]
        expected_actor.update(type="text", doc_value=True, token=["|", "~"], caseSensitive=True)
        for json_option in ("index_all", "max_depth", "json_keys"):
            expected_actor.pop(json_option, None)

    with (
        patch.object(store, "get_existing_index_config", return_value=existing),
        patch.object(store, "_get_workflow_execution_index_keys", return_value=required_keys),
    ):
        store.ensure_index_config(AliyunLogStore.workflow_execution_logstore)

    store.client.create_index.assert_not_called()
    store.client.update_index.assert_called_once()
    project, logstore, updated = store.client.update_index.call_args.args
    assert (project, logstore) == ("test-project", AliyunLogStore.workflow_execution_logstore)
    assert updated.to_json()["keys"]["from_account_id"] == expected["keys"]["from_account_id"]
    assert updated.to_json() == expected
    # The SDK omits ttl/modify_time and serializes only the white list when both lists exist.
    assert vars(updated) == {**vars(existing), "key_config_list": updated.key_config_list}
    assert updated is not existing
    assert updated.key_config_list is not existing.key_config_list
    assert existing.to_json() == before
    assert {name: config.to_json() for name, config in required_keys.items()} == required_before

    store.client.reset_mock()
    with patch.object(store, "get_existing_index_config", return_value=updated):
        store.ensure_index_config(AliyunLogStore.workflow_execution_logstore)
    store.client.update_index.assert_not_called()
    store.client.create_index.assert_not_called()
    assert updated.to_json() == expected
    assert existing.to_json() == before


@pytest.mark.parametrize(
    ("index_type", "doc_value"),
    [("text", False), ("json", True), ("json", False), ("long", True), ("double", False)],
)
def test_existing_incompatible_actor_index_is_upgraded(index_type: str, doc_value: bool) -> None:
    store = object.__new__(AliyunLogStore)
    store.client = Mock()
    store.project_name = "test-project"
    keys = store._get_workflow_execution_index_keys()
    actor = IndexKeyConfig(index_type=index_type, doc_value=doc_value)
    custom_key = IndexKeyConfig(index_type="long", doc_value=False)
    line_config = IndexLineConfig(token_list=[" "], case_sensitive=True, chinese=False)
    existing = IndexConfig(
        line_config=line_config,
        key_config_list={**keys, "from_account_id": actor, "custom_metric": custom_key},
        scan_index=False,
    )

    with patch.object(store, "get_existing_index_config", return_value=existing):
        store.ensure_index_config(AliyunLogStore.workflow_execution_logstore)

    store.client.create_index.assert_not_called()
    store.client.update_index.assert_called_once()
    project, logstore, updated = store.client.update_index.call_args.args
    assert (project, logstore) == ("test-project", AliyunLogStore.workflow_execution_logstore)
    assert updated.key_config_list["from_account_id"].index_type == "text"
    assert updated.key_config_list["from_account_id"].doc_value is True
    assert updated.key_config_list["custom_metric"] is custom_key
    assert updated.line_config is line_config
    assert updated.scan_index is False
    assert existing.key_config_list["from_account_id"] is actor
    assert actor.index_type == index_type
    assert actor.doc_value is doc_value

    store.client.reset_mock()
    with patch.object(store, "get_existing_index_config", return_value=updated):
        store.ensure_index_config(AliyunLogStore.workflow_execution_logstore)
    store.client.update_index.assert_not_called()


@pytest.mark.parametrize(
    ("logstore_name", "field_name"),
    [
        (AliyunLogStore.workflow_execution_logstore, "inputs"),
        (AliyunLogStore.workflow_node_execution_logstore, "from_account_id"),
    ],
)
def test_other_fields_keep_existing_json_text_compatibility(logstore_name: str, field_name: str) -> None:
    store = object.__new__(AliyunLogStore)
    existing_key = IndexKeyConfig(index_type="json", doc_value=False)
    existing = IndexConfig(key_config_list={field_name: existing_key})
    required_keys = {field_name: IndexKeyConfig(index_type="text", doc_value=True)}

    merged, needs_update = store._merge_index_configs(existing, required_keys, logstore_name)

    assert needs_update is False
    assert merged.key_config_list[field_name] is existing_key
