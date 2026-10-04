"""Check optional recovery defaults in committed deployment examples only."""

from pathlib import Path

import yaml


def test_avatar_recovery_defaults_reach_api_beat_and_extend_worker_examples():
    root = Path(__file__).resolve().parents[4]
    compose = yaml.safe_load((root / "docker/docker-compose.dify-plus.yaml").read_text())
    variables = {
        "ENABLE_CASDOOR_AVATAR_INITIAL_RECOVERY_TASK": "false",
        "CASDOOR_AVATAR_INITIAL_RECOVERY_INTERVAL_SECONDS": "30",
    }

    shared = compose["x-shared-env"]
    services = compose["services"]
    for name, default in variables.items():
        expression = f"${{{name}:-{default}}}"
        assert shared[name] == expression
        for service_name, mode in (("api", "api"), ("worker_beat", "beat"), ("worker-gaia", "worker")):
            environment = services[service_name]["environment"]
            assert environment[name] == expression
            assert environment["MODE"] == mode

    gaia_queues = services["worker-gaia"]["environment"]["CELERY_QUEUES"]
    assert gaia_queues == "${CELERY_QUEUES:-extend_high,extend_low}"
    assert "extend_low" in gaia_queues.removeprefix("${CELERY_QUEUES:-").removesuffix("}").split(",")

    api_example = (root / "docker/envs/core-services/api.env.example").read_text().splitlines()
    root_example = (root / "docker/.env.example").read_text().splitlines()
    for name, default in variables.items():
        assert [line for line in api_example if line == f"{name}={default}"] == [f"{name}={default}"]
        assert not any(line.partition("=")[0] == name for line in root_example)
