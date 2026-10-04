"""Static deployment defaults only; no dotenv, Compose CLI or running services."""

from pathlib import Path

import yaml
from yaml.nodes import MappingNode, ScalarNode


def test_optional_avatar_recovery_defaults_share_api_beat_and_extend_worker_mapping():
    root = Path(__file__).resolve().parents[4]
    source = (root / "docker/docker-compose.dify-plus.yaml").read_text()
    document = yaml.safe_load(source)
    syntax = yaml.compose(source, Loader=yaml.SafeLoader)
    defaults = {
        "ENABLE_CASDOOR_AVATAR_INITIAL_RECOVERY_TASK": "false",
        "CASDOOR_AVATAR_INITIAL_RECOVERY_INTERVAL_SECONDS": "30",
    }

    def field(node, name):
        assert isinstance(node, MappingNode)
        matches = [value for key, value in node.value if key.value == name]
        assert len(matches) == 1, name
        return matches[0]

    shared_node = field(syntax, "x-shared-env")
    services_node = field(syntax, "services")
    for key, default in defaults.items():
        expected = "${" + key + ":-" + default + "}"
        value = field(shared_node, key)
        assert isinstance(value, ScalarNode) and value.value == expected
        assert document["x-shared-env"][key] == expected
        for service, mode in (("api", "api"), ("worker_beat", "beat"), ("worker-gaia", "worker")):
            environment_node = field(field(services_node, service), "environment")
            assert field(environment_node, "<<") is shared_node
            assert key not in {name.value for name, _value in environment_node.value}
            environment = document["services"][service]["environment"]
            assert environment[key] == expected
            assert environment["MODE"] == mode

    queue_expression = document["services"]["worker-gaia"]["environment"]["CELERY_QUEUES"]
    assert queue_expression == "${CELERY_QUEUES:-extend_high,extend_low}"
    assert "extend_low" in queue_expression.removeprefix("${CELERY_QUEUES:-").removesuffix("}").split(",")

    example = (root / "docker/envs/core-services/api.env.example").read_text()
    assignments = [line.split("=", 1) for line in example.splitlines() if line and not line.startswith("#")]
    required_example = (root / "docker/.env.example").read_text()
    for key, default in defaults.items():
        assert [value for name, value in assignments if name == key] == [default]
        assert key not in required_example
