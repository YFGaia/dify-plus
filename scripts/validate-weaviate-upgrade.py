#!/usr/bin/env python3
"""CI-only, single-node filesystem snapshot upgrade validation, not logical backup testing."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

OLD_IMAGE = "cr.weaviate.io/semitechnologies/weaviate:1.39.2"
TARGET_IMAGE = "cr.weaviate.io/semitechnologies/weaviate:1.39.13"
IMAGE_DIGESTS = {
    OLD_IMAGE: "sha256:5c62e5cbce4c48fc770abcee71099d9de32aad3b44d12299611f8135ee412362",
    TARGET_IMAGE: "sha256:ad2b986bbe4666fbd5302a5ee0cc6c74a048c4c769198d46147ea232d46a2dd8",
}
# Public synthetic values used by the existing integration fixture / Compose examples.
API_KEY = "WVF5YThaHlkYwhGUSmCRgsX3tD5ngdN8pkih"
API_USER = "hello@dify.ai"
COLLECTION = "UpgradeProbe"
OBJECTS = [
    {
        "id": "00000000-0000-0000-0000-000000000001",
        "body": "upgradebaseline synthetic alpha",
        "vector": [1.0, 0.0, 0.0],
    },
    {
        "id": "00000000-0000-0000-0000-000000000002",
        "body": "synthetic beta",
        "vector": [0.8, 0.2, 0.0],
    },
    {
        "id": "00000000-0000-0000-0000-000000000003",
        "body": "postupgrade synthetic gamma",
        "vector": [0.0, 1.0, 0.0],
    },
]


class Validation:
    def __init__(
        self, source: Path, evidence: Path, architecture: str, temporary: Path
    ):
        self.source = source
        self.evidence = evidence
        self.architecture = architecture
        self.temporary = temporary
        self.name = f"dify-upgrade-{uuid.uuid4().hex}"
        self.created = False
        self.running = False
        self.stage = "initialize"
        self.report: dict[str, Any] = {
            "scope": "single-node graceful-stop filesystem snapshots; not Weaviate logical backup module",
            "architecture": architecture,
            "images": {},
            "stages": [],
            "snapshots": [],
            "shutdowns": [],
            "status": "running",
        }
        self.baseline_schema: dict[str, Any] | None = None
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def command(self, *args: str, timeout: int = 180) -> str:
        result = subprocess.run(
            args,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(
                f"{args[0:2]} failed ({result.returncode}): {result.stdout[-6000:]}"
            )
        return result.stdout

    def save(self) -> None:
        (self.evidence / "upgrade.json").write_text(
            json.dumps(self.report, indent=2) + "\n"
        )

    def request(
        self, method: str, path: str, payload: Any = None, *, authenticated: bool = True
    ) -> Any:
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = f"Bearer {API_KEY}"
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:8080{path}", data=data, method=method, headers=headers
        )
        with self.http.open(request, timeout=10) as response:
            body = response.read()
            return json.loads(body) if body else None

    def eventually(self, operation: Any, *, seconds: int = 120) -> Any:
        deadline = time.monotonic() + seconds
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                return operation()
            except (
                AssertionError,
                urllib.error.URLError,
                TimeoutError,
                ConnectionError,
            ) as error:
                last_error = error
                time.sleep(2)
        raise RuntimeError(f"{self.stage}: read verification timed out: {last_error}")

    def start(self, image: str, data: Path, stage: str) -> None:
        if self.created or self.running:
            raise RuntimeError("Refusing to start a second Weaviate container")
        self.stage = stage
        data.mkdir(exist_ok=True)
        arguments = [
            "docker",
            "run",
            "--detach",
            "--name",
            self.name,
            "--label",
            f"dify-upgrade-owner={self.name}",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--publish",
            "127.0.0.1:8080:8080",
            "--publish",
            "127.0.0.1:50051:50051",
            "--mount",
            f"type=bind,src={data},dst=/var/lib/weaviate",
        ]
        environment = {
            "PERSISTENCE_DATA_PATH": "/var/lib/weaviate",
            "DEFAULT_VECTORIZER_MODULE": "none",
            "ENABLE_API_BASED_MODULES": "false",
            "CLUSTER_HOSTNAME": "upgrade-node",
            "DISABLE_TELEMETRY": "true",
            "AUTHENTICATION_ANONYMOUS_ACCESS_ENABLED": "false",
            "AUTHENTICATION_APIKEY_ENABLED": "true",
            "AUTHENTICATION_APIKEY_ALLOWED_KEYS": API_KEY,
            "AUTHENTICATION_APIKEY_USERS": API_USER,
            "AUTHORIZATION_ADMINLIST_ENABLED": "true",
            "AUTHORIZATION_ADMINLIST_USERS": API_USER,
        }
        for key, value in environment.items():
            arguments.extend(["--env", f"{key}={value}"])
        self.created = True  # Even a failed docker run can leave its named container.
        self.command(*arguments, f"{image}@{IMAGE_DIGESTS[image]}")
        self.running = True
        self.eventually(lambda: self.request("GET", "/v1/.well-known/ready"))
        metadata = self.request("GET", "/v1/meta")
        if metadata["version"] != image.rsplit(":", 1)[1]:
            raise RuntimeError(f"Unexpected running version: {metadata['version']}")
        try:
            self.request("GET", "/v1/schema", authenticated=False)
        except urllib.error.HTTPError as error:
            if error.code not in (401, 403):
                raise
        else:
            raise RuntimeError("Unauthenticated schema request was allowed")
        self.record_binary(image)

    def record_binary(self, image: str) -> None:
        if image in self.report["images"]:
            return
        info = json.loads(
            self.command(
                "docker", "image", "inspect", f"{image}@{IMAGE_DIGESTS[image]}"
            )
        )[0]
        if info["Architecture"] != self.architecture or info["Os"] != "linux":
            raise RuntimeError("Image architecture does not match native runner")
        if not info.get("RepoDigests"):
            raise RuntimeError("Image has no registry digest")
        binary = self.temporary / f"weaviate-{image.rsplit(':', 1)[1]}"
        # Official v1.39.13 Dockerfile copies its server executable to /bin/weaviate.
        self.command("docker", "cp", f"{self.name}:/bin/weaviate", str(binary))
        build = self.command("go", "version", "-m", str(binary))
        (self.evidence / f"go-build-{image.rsplit(':', 1)[1]}.txt").write_text(build)
        match = re.search(r":\s+go(\d+)\.(\d+)\.(\d+)(?:\s|$)", build.splitlines()[0])
        if not match:
            raise RuntimeError(
                "Unknown Go build version format; refusing to accept image"
            )
        version = tuple(map(int, match.groups()))
        self.report["images"][image] = {
            "tag": image,
            "requested_reference": f"{image}@{IMAGE_DIGESTS[image]}",
            "id": info["Id"],
            "repo_digests": info["RepoDigests"],
            "architecture": info["Architecture"],
            "go_version": ".".join(match.groups()),
            "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        }
        self.save()
        if image == TARGET_IMAGE and version < (1, 26, 10):
            raise RuntimeError(
                f"Target Go {version} is below required security floor 1.26.10"
            )

    def stop(self) -> None:
        self.command("docker", "stop", "--time", "60", self.name, timeout=90)
        self.running = False
        state = json.loads(
            self.command("docker", "inspect", "--format", "{{json .State}}", self.name)
        )
        self.report["shutdowns"].append(
            {
                "stage": self.stage,
                "exit_code": state["ExitCode"],
                "oom_killed": state["OOMKilled"],
            }
        )
        if state["Running"] or state["OOMKilled"] or state["ExitCode"] != 0:
            raise RuntimeError("Weaviate did not stop cleanly; snapshot refused")
        self.command("docker", "rm", self.name)
        self.created = False

    def snapshot(self, data: Path, label: str) -> Path:
        if self.created or self.running:
            raise RuntimeError(
                "Snapshots require the container to have stopped cleanly"
            )
        archive = self.temporary / f"{label}.tar.gz"
        with tarfile.open(archive, "w:gz") as snapshot:
            snapshot.add(data, arcname="data")
        self.report["snapshots"].append(
            {
                "label": label,
                "bytes": archive.stat().st_size,
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "consistent_graceful_stop": True,
            }
        )
        self.save()
        return archive

    def restore(self, archive: Path, directory: str) -> Path:
        if self.created or self.running:
            raise RuntimeError("Restore requires the previous container to be removed")
        destination = self.temporary / directory
        destination.mkdir()
        with tarfile.open(archive, "r:gz") as snapshot:
            snapshot.extractall(destination, filter="data")
        return destination / "data"

    def insert(self, obj: dict[str, Any]) -> None:
        # Writes are never automatically retried: an ambiguous outcome must fail this run.
        self.request(
            "POST",
            "/v1/objects",
            {
                "class": COLLECTION,
                "id": obj["id"],
                "properties": {"body": obj["body"]},
                "vector": obj["vector"],
            },
        )

    def verify(self, count: int) -> None:
        def read_and_check() -> dict[str, Any]:
            schema = self.request("GET", f"/v1/schema/{COLLECTION}")
            normalized = {
                "class": schema["class"],
                "vectorizer": schema["vectorizer"],
                "distance": schema["vectorIndexConfig"]["distance"],
                "properties": [
                    {"name": item["name"], "dataType": item["dataType"]}
                    for item in schema["properties"]
                ],
            }
            assert normalized == self.baseline_schema, (
                normalized,
                self.baseline_schema,
            )
            objects = []
            for expected in OBJECTS[:count]:
                actual = self.request(
                    "GET", f"/v1/objects/{COLLECTION}/{expected['id']}?include=vector"
                )
                assert actual["properties"] == {"body": expected["body"]}, actual
                assert len(actual["vector"]) == len(expected["vector"]), actual
                assert all(
                    abs(a - b) < 1e-6
                    for a, b in zip(actual["vector"], expected["vector"], strict=True)
                ), actual
                objects.append(actual)
            query = """{ Get { UpgradeProbe(nearVector: {vector: [1, 0, 0]}, limit: 2) {
                body _additional { id distance }
            } } }"""
            vector = self.request("POST", "/v1/graphql", {"query": query})
            assert "errors" not in vector, vector
            hits = vector["data"]["Get"][COLLECTION]
            assert [item["_additional"]["id"] for item in hits] == [
                obj["id"] for obj in OBJECTS[:2]
            ], vector
            query = '{ Get { UpgradeProbe(bm25: {query: "upgradebaseline", properties: ["body"]}) { body _additional { id score } } } }'
            bm25 = self.request("POST", "/v1/graphql", {"query": query})
            assert "errors" not in bm25, bm25
            assert [
                item["_additional"]["id"] for item in bm25["data"]["Get"][COLLECTION]
            ] == [OBJECTS[0]["id"]], bm25
            count_result = self.request(
                "POST",
                "/v1/graphql",
                {"query": "{ Aggregate { UpgradeProbe { meta { count } } } }"},
            )
            assert (
                count_result["data"]["Aggregate"][COLLECTION][0]["meta"]["count"]
                == count
            ), count_result
            return {
                "schema": normalized,
                "objects": objects,
                "vector_query": vector,
                "bm25_query": bm25,
                "count": count_result,
            }

        results = self.eventually(read_and_check)
        self.report["stages"].append(
            {"stage": self.stage, "checks": results, "status": "passed"}
        )
        self.save()

    def run(self) -> None:
        for image in (OLD_IMAGE, TARGET_IMAGE):
            self.command(
                "docker", "pull", f"{image}@{IMAGE_DIGESTS[image]}", timeout=600
            )
        original = self.temporary / "original"
        self.start(OLD_IMAGE, original, "old-baseline")
        self.request(
            "POST",
            "/v1/schema",
            {
                "class": COLLECTION,
                "vectorizer": "none",
                "vectorIndexConfig": {"distance": "cosine"},
                "properties": [
                    {"name": "body", "dataType": ["text"], "indexSearchable": True}
                ],
            },
        )
        self.baseline_schema = {
            "class": COLLECTION,
            "vectorizer": "none",
            "distance": "cosine",
            "properties": [{"name": "body", "dataType": ["text"]}],
        }
        for obj in OBJECTS[:2]:
            self.insert(obj)
        self.verify(2)
        self.stop()
        baseline = self.snapshot(original, "old-baseline")
        self.start(TARGET_IMAGE, original, "upgraded-existing-data")
        self.verify(2)
        self.stage = "target-write"
        self.insert(OBJECTS[2])
        self.verify(3)
        self.stop()
        self.start(TARGET_IMAGE, original, "target-restart")
        self.verify(3)
        self.stop()
        upgraded = self.snapshot(original, "upgraded-data")
        restored = self.restore(upgraded, "target-restore")
        self.start(TARGET_IMAGE, restored, "target-filesystem-restore")
        self.verify(3)
        self.stop()
        rollback = self.restore(baseline, "old-restore")
        self.start(OLD_IMAGE, rollback, "old-image-baseline-filesystem-restore")
        self.verify(2)
        self.stop()
        final = self.restore(upgraded, "target-final")
        self.start(TARGET_IMAGE, final, "target-final-restore")
        self.verify(3)
        self.stage = "existing-sdk-integration"
        command = [
            "uv",
            "run",
            "--project",
            str(self.source / "api"),
            "--frozen",
            "--no-sync",
            "pytest",
            "-p",
            "no:benchmark",
            "--no-cov",
            "--timeout",
            "180",
            f"--junitxml={self.evidence / 'sdk-integration.xml'}",
            "api/providers/vdb/vdb-weaviate/tests/integration_tests/test_weaviate.py",
        ]
        with (self.evidence / "sdk-integration.log").open("w") as log:
            result = subprocess.run(
                command,
                cwd=self.source,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=600,
                check=False,
            )
        if result.returncode:
            raise RuntimeError(
                "Existing SDK integration failed; see sdk-integration.log"
            )
        self.report["stages"].append(
            {"stage": self.stage, "status": "passed", "command": command}
        )
        self.stop()
        self.report["status"] = "passed"
        self.save()

    def cleanup(self) -> None:
        if not self.created:
            return
        inspect = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                '{{index .Config.Labels "dify-upgrade-owner"}}',
                self.name,
            ],
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        if inspect.returncode:
            return
        if inspect.stdout.strip() != self.name:
            raise RuntimeError(
                "Refusing to clean a container without this run's ownership label"
            )
        logs = subprocess.run(
            ["docker", "logs", "--tail", "200", self.name],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
            check=False,
        )
        (self.evidence / "failed-container-tail.log").write_text(logs.stdout)
        self.command("docker", "rm", "--force", self.name, timeout=90)
        self.created = False


def main() -> None:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit(
            "CI-only validation: GITHUB_ACTIONS=true is required; do not run local integration services"
        )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--architecture", choices=("amd64", "arm64"), required=True)
    args = parser.parse_args()
    args.evidence = args.evidence.resolve()
    args.evidence.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="dify-weaviate-upgrade-"))
    validation = Validation(
        args.source.resolve(), args.evidence, args.architecture, temporary
    )

    def interrupted(signum: int, _frame: Any) -> None:
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, interrupted)
    try:
        validation.run()
    except BaseException as error:
        validation.report.update(
            status="failed", failed_stage=validation.stage, error=str(error)
        )
        raise
    finally:
        try:
            validation.cleanup()
        finally:
            validation.save()
            shutil.rmtree(temporary)


if __name__ == "__main__":
    main()
