"""Validate with non-runtime placeholders; never print resolved config or real env."""
from pathlib import Path
import json
import os
import subprocess

here = Path(__file__).resolve().parent
plan = json.loads((here / "runtime-plan.json").read_text())
env = {k: v for k, v in os.environ.items() if k in ["PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT"]}
for key in ["SECRET_KEY", "DB_PASSWORD", "REDIS_PASSWORD", "QDRANT_API_KEY", "PLUGIN_DAEMON_KEY", "PLUGIN_INNER_KEY", "SANDBOX_API_KEY"]:
    env["LOCAL_ACCEPTANCE_" + key] = "config-validation-only-placeholder"
for key in ["API_IMAGE", "WEB_IMAGE", "PLUGIN_IMAGE", "SSRF_IMAGE"]:
    env["LOCAL_ACCEPTANCE_" + key] = "validation-only/" + key.lower() + ":not-a-runtime-image"
env["LOCAL_ACCEPTANCE_BASE_COMPOSE"] = plan["compose_base"]
command = ["docker", "compose", "--project-directory", plan["compose_project_directory"], "--env-file", "/dev/null", "--project-name", plan["project"], "-f", plan["compose_base"], "-f", plan["compose_overlay"]]
for profile in plan["active_profiles"] + ["migration", "acceptance-mysql-migration", "acceptance-mysql-probe", "acceptance-beat"]:
    command += ["--profile", profile]
quiet = subprocess.run(command + ["config", "--quiet"], env=env, text=True, capture_output=True)
assert quiet.returncode == 0, quiet.stderr
resolved = subprocess.run(command + ["config", "--format", "json"], env=env, text=True, capture_output=True)
assert resolved.returncode == 0, resolved.stderr
config = json.loads(resolved.stdout)
services = config["services"]
checks = []
expected_ports = {"nginx":23010, "api":25442, "api_websocket":25443}
for name in plan["business_services"] + plan["middleware_services"] + plan["init_services"] + plan["migration_services"] + ["mysql_probe", "worker_beat"]:
    service = services[name]
    ports = service.get("ports", [])
    if name in expected_ports:
        assert len(ports) == 1 and ports[0]["host_ip"] == plan["bind_host"] and int(ports[0]["published"]) == expected_ports[name], name
    else:
        assert not ports, name
    for mount in service.get("volumes", []):
        if mount["type"] == "volume":
            assert mount["source"].startswith("acceptance_"), (name, mount["source"])
        elif mount["type"] == "bind":
            assert mount.get("read_only") and "/docker/" in mount["source"] and name in {"nginx", "ssrf_proxy"}, (name, mount["source"])
    checks.append("isolated volumes/loopback ports: " + name)
for name in ["api", "api_websocket", "worker", "worker-gaia", "worker-dataset", "init_secret_key", "migration"]:
    settings = services[name]["environment"]
    assert settings["MIGRATION_ENABLED"] == "false"
    assert settings["DB_HOST"] == "db_postgres" and settings["DB_DATABASE"] == "dify_acceptance"
    assert settings["REDIS_HOST"] == "redis" and settings["VECTOR_STORE"] == "qdrant"
    assert settings["STORAGE_TYPE"] == "opendal" and settings["OPENDAL_SCHEME"] == "fs"
    assert "agent_backend" not in services[name].get("depends_on", {})
assert "workflow_based_app_execution" in services["worker"]["environment"]["CELERY_WORKER_QUEUES"].split(",")
assert services["worker-gaia"]["environment"]["CELERY_WORKER_QUEUES"] == "extend_high,extend_low"
assert services["nginx"]["environment"]["NGINX_SOCKET_IO_UPSTREAM"] == "api_websocket:5001"
assert "sh /docker-entrypoint.sh" in services["nginx"]["entrypoint"][-1]
assert services["web"]["environment"]["NEXT_PUBLIC_SOCKET_URL"] == plan["origins"]["socket_browser"]
assert services["qdrant"]["image"].endswith(":v1.8.3")
# The probe/migration service configurations must keep MySQL isolated even while PG serves UI.
for name in ["migration_mysql", "mysql_probe"]:
    settings = services[name]["environment"]
    assert settings["DB_HOST"] == "db_mysql" and settings["DB_TYPE"] == "mysql"
    assert settings["DB_DATABASE"] == "dify_acceptance_mysql"
summary = {"config_quiet_exit":0, "static_assertions":"passed", "validator_started_runtime":False, "secrets":"only validation placeholders; resolved JSON not stored or printed", "host":plan["host"], "bind_host":plan["bind_host"], "project":plan["project"], "checks":checks, "image_readiness_verified_by_validator":False}
(here / "config-validation-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
print("PASS: config quiet, selected services, independent volumes,::1 binds/localhost URLs, WS, workflow+extend queues; no runtime startup or real secrets")
