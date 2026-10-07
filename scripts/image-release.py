#!/usr/bin/env python3
"""Verify GHCR image configs and promote only an entirely smoke-tested release.

Only registry reads and buildx manifest copies are performed. Credentials are
read from CI environment and never included in evidence or command arguments.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request

COMPONENTS = ("api", "web", "agent-backend", "agent-local-sandbox")
ARCHITECTURES = ("amd64", "arm64")
ACCEPT = ", ".join(("application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json", "application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"))


class Registry:
    def __init__(self, image):
        assert image.startswith("ghcr.io/")
        self.repository = image.removeprefix("ghcr.io/")
        query = urllib.parse.urlencode({"service": "ghcr.io", "scope": f"repository:{self.repository}:pull"})
        headers = {}
        token = os.environ.get("GHCR_TOKEN")
        if token:
            auth = f"{os.environ['GHCR_ACTOR']}:{token}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(auth).decode()
        with urllib.request.urlopen(urllib.request.Request("https://ghcr.io/token?" + query, headers=headers), timeout=60) as response:
            self.token = json.load(response)["token"]

    def read(self, kind, reference, missing_ok=False):
        url = f"https://ghcr.io/v2/{self.repository}/{kind}/{reference}"
        request = urllib.request.Request(url, headers={"Authorization": "Bearer " + self.token, "Accept": ACCEPT})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
                digest = response.headers.get("Docker-Content-Digest", "sha256:" + hashlib.sha256(raw).hexdigest())
                if reference.startswith("sha256:"):
                    assert "sha256:" + hashlib.sha256(raw).hexdigest() == reference, "Registry digest mismatch"
                return json.loads(raw), digest
        except urllib.error.HTTPError as error:
            if error.code == 404 and missing_ok:
                return None, None
            raise RuntimeError(f"GHCR {kind} read failed: HTTP {error.code}") from None

    def verify_child(self, digest, architecture, revision):
        manifest, actual = self.read("manifests", digest)
        assert actual == digest
        assert "manifests" not in manifest, "Architecture candidate must be a single image"
        config, _ = self.read("blobs", manifest["config"]["digest"])
        assert config["os"] == "linux" and config["architecture"] == architecture, "Wrong native platform"
        assert config["config"]["Labels"]["org.opencontainers.image.revision"] == revision, "Wrong source revision"
        assert f"COMMIT_SHA={revision}" in config["config"]["Env"], "Wrong runtime COMMIT_SHA"

    def verify_index(self, reference, children, revision):
        manifest, digest = self.read("manifests", reference)
        entries = manifest.get("manifests", [])
        assert len(entries) == 2, "Release requires exactly two native image manifests"
        actual = {entry["platform"]["architecture"]: entry["digest"] for entry in entries if entry["platform"]["os"] == "linux"}
        assert actual == children, "Unexpected release platform or child digest"
        for architecture, child in children.items():
            self.verify_child(child, architecture, revision)
        return digest


def record(args):
    registry = Registry(args.image)
    manifest, digest = registry.read("manifests", f"sha-{args.revision}-{args.architecture}")
    registry.verify_child(digest, args.architecture, args.revision)
    result = {"component": args.component, "architecture": args.architecture, "image": args.image, "digest": digest, "source_revision": args.revision, "smoke": "passed", "scope": "native image startup and dependencies; API includes disposable PostgreSQL dual migrations"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")


def promote(args):
    assert re.fullmatch(r"\d+\.\d+\.\d+-plus\.[1-9]\d*", args.version), "Invalid fork release version"
    records = [json.loads(path.read_text()) for path in args.evidence.glob("*.json")]
    assert len(records) == 8, "Expected eight native smoke evidence files"
    images = []
    for component in COMPONENTS:
        image = f"{args.prefix}-{component}"
        selected = [item for item in records if item["component"] == component]
        assert len(selected) == 2
        children = {item["architecture"]: item["digest"] for item in selected}
        assert set(children) == set(ARCHITECTURES)
        registry = Registry(image)
        for item in selected:
            assert item["image"] == image and item["source_revision"] == args.revision and item["smoke"] == "passed"
            registry.verify_child(item["digest"], item["architecture"], args.revision)
        candidate = f"{image}:sha-{args.revision}"
        subprocess.run(["docker", "buildx", "imagetools", "create", "--tag", candidate, *[f"{image}@{children[arch]}" for arch in ARCHITECTURES]], check=True)
        digest = registry.verify_index(f"sha-{args.revision}", children, args.revision)
        _, existing = registry.read("manifests", args.version, missing_ok=True)
        assert existing in (None, digest), f"Refusing to overwrite existing release: {image}:{args.version}"
        images.append({"component": component, "image": image, "candidate": candidate, "digest": digest, "platforms": children})
    # All four images pass preflight before the first stable tag is written.
    # Registry has no multi-package transaction; evidence is complete only after all four readbacks.
    for item in images:
        subprocess.run(["docker", "buildx", "imagetools", "create", "--tag", f"{item['image']}:{args.version}", f"{item['image']}@{item['digest']}"], check=True)
        actual = Registry(item["image"]).verify_index(args.version, item["platforms"], args.revision)
        assert actual == item["digest"], "Stable digest differs from candidate"
    result = {"release_version": args.version, "source_revision": args.revision, "registry": "ghcr.io", "native_smoke": "8/8 passed", "images": images, "production_acceptance": "not asserted"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    one = modes.add_parser("record")
    one.add_argument("--component", choices=COMPONENTS, required=True)
    one.add_argument("--architecture", choices=ARCHITECTURES, required=True)
    one.add_argument("--image", required=True)
    one.add_argument("--revision", required=True)
    one.add_argument("--output", type=Path, required=True)
    release = modes.add_parser("promote")
    release.add_argument("--version", required=True)
    release.add_argument("--revision", required=True)
    release.add_argument("--prefix", required=True)
    release.add_argument("--evidence", type=Path, required=True)
    release.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    assert re.fullmatch(r"[0-9a-f]{40}", args.revision), "A full source commit is required"
    if args.mode == "record": record(args)
    else: promote(args)


if __name__ == "__main__":
    main()
