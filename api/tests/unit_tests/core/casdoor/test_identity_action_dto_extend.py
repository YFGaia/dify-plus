"""The DTO module resolves its late-defined models without transport registration."""

import inspect

import controllers.console.casdoor_schemas_extend as schemas


def test_all_response_schemas_and_empty_configuration_resolve_from_dto_module_alone():
    response_types = [
        model
        for _, model in inspect.getmembers(schemas, inspect.isclass)
        if model.__module__ == schemas.__name__ and issubclass(model, schemas.CasdoorResponse)
    ]
    assert response_types
    for model in response_types:
        assert model.model_json_schema()["type"] == "object"
    assert schemas.CasdoorConfigurationResponse().model_dump(mode="json") == {
        "enabled": False,
        "etag": 0,
        "active_revision_id": None,
        "draft_revision_id": None,
        "active": None,
        "draft": None,
    }
