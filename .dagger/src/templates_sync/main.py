import asyncio
import hashlib
import json

import dagger
from dagger import dag, function, object_type

PYTHON_GIT_IMAGE = (
    "python:3.12.11-bookworm@"
    "sha256:13c9584604a99ca134c4f41800f74ffc64ee6ac8cf555cf1e704a6087fc84f12"
)
NODE_IMAGE = (
    "node:22-bookworm-slim@"
    "sha256:53ada149d435c38b14476cb57e4a7da73c15595aba79bd6971b547ceb6d018bf"
)
ACTIONLINT_IMAGE = (
    "rhysd/actionlint:1.7.12@"
    "sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667"
)
ZIZMOR_IMAGE = (
    "ghcr.io/zizmorcore/zizmor:1.29.0@"
    "sha256:863026d54f91271b10b60b67ad8054cb37120167e162482597db102b3026a284"
)
TARGET_PLATFORM = dagger.Platform("linux/amd64")


@object_type
class TemplatesSync:
    def _engine_container(
        self,
        source: dagger.Directory,
        bundle: dagger.File,
    ) -> dagger.Container:
        return (
            dag.container(platform=TARGET_PLATFORM)
            .from_(PYTHON_GIT_IMAGE)
            .with_mounted_directory("/overlay", source)
            .with_mounted_file("/input/source.bundle", bundle)
            .with_exec(["git", "clone", "/input/source.bundle", "/repo"])
        )

    def _engine_args(
        self,
        command: str,
        fork_sha: str,
        upstream_sha: str,
        bootstrap: bool,
    ) -> list[str]:
        args = [
            "python3",
            "/overlay/.fork-sync/sync_engine.py",
            command,
            "--repo",
            "/repo",
            "--overlay-root",
            "/overlay",
            "--contract",
            "/overlay/.fork-sync/contract.json",
            "--fork-sha",
            fork_sha,
            "--upstream-sha",
            upstream_sha,
        ]
        if bootstrap:
            args.append("--bootstrap")
        return args

    def _materialized(
        self,
        source: dagger.Directory,
        bundle: dagger.File,
        fork_sha: str,
        upstream_sha: str,
        bootstrap: bool,
        allow_needs_review: bool,
    ) -> dagger.Container:
        args = self._engine_args("materialize", fork_sha, upstream_sha, bootstrap)
        args.extend(["--candidate-out", "/candidate", "--receipt-out", "/receipt.json"])
        if allow_needs_review:
            args.append("--allow-needs-review")
        return self._engine_container(source, bundle).with_exec(args)

    async def _validate_candidate(
        self,
        candidate: dagger.Directory,
        blueprints: list[str],
    ) -> dict[str, object]:
        contract = json.loads(await candidate.file(".fork-sync/contract.json").contents())
        node = (
            dag.container(platform=TARGET_PLATFORM)
            .from_(NODE_IMAGE)
            .with_mounted_directory("/src", candidate)
            .with_workdir("/src")
            .with_exec(["corepack", "enable"])
            .with_exec(
                [
                    "corepack",
                    "prepare",
                    f"pnpm@{contract['validation']['pnpm_version']}",
                    "--activate",
                ]
            )
            .with_exec(["node", "build-scripts/generate-meta.js", "--output", "/tmp/meta.json"])
            .with_exec(["pnpm", "--dir", "build-scripts", "install", "--frozen-lockfile"])
        )
        for blueprint in blueprints:
            node = node.with_exec(
                [
                    "pnpm",
                    "--dir",
                    "build-scripts",
                    "exec",
                    "tsx",
                    "validate-docker-compose.ts",
                    "--file",
                    f"../blueprints/{blueprint}/docker-compose.yml",
                ]
            ).with_exec(
                [
                    "pnpm",
                    "--dir",
                    "build-scripts",
                    "exec",
                    "tsx",
                    "validate-template.ts",
                    "--dir",
                    f"../blueprints/{blueprint}",
                ]
            )
        node = node.with_exec(["pnpm", "--dir", "app", "install", "--frozen-lockfile"]).with_exec(
            ["pnpm", "--dir", "app", "build"]
        )
        actionlint = (
            dag.container(platform=TARGET_PLATFORM)
            .from_(ACTIONLINT_IMAGE)
            .with_entrypoint([])
            .with_mounted_directory("/src", candidate)
            .with_workdir("/src")
            .with_exec(["actionlint", "-color"])
        )
        zizmor = (
            dag.container(platform=TARGET_PLATFORM)
            .from_(ZIZMOR_IMAGE)
            .with_entrypoint([])
            .with_mounted_directory("/src", candidate)
            .with_workdir("/src")
            .with_exec(
                [
                    "zizmor",
                    "--offline",
                    "--min-severity",
                    "medium",
                    "--min-confidence",
                    "high",
                    *contract["validation"]["security_workflows"],
                ]
            )
        )
        await asyncio.gather(node.sync(), actionlint.sync(), zizmor.sync())
        generated = await node.file("/tmp/meta.json").contents()
        return {
            "generated_catalog_sha256": hashlib.sha256(generated.encode()).hexdigest(),
            "generated_catalog_count": len(json.loads(generated)),
            "validations": [
                "candidate-structure",
                "per-blueprint-metadata",
                "changed-blueprints",
                "app-frozen-build",
                "actionlint",
                "zizmor-offline",
            ],
        }

    @function
    async def audit(
        self,
        source: dagger.Directory,
        bundle: dagger.File,
        fork_sha: str,
        upstream_sha: str,
        bootstrap: bool = False,
    ) -> str:
        """Audit exact fork/upstream commits and return ForkSyncReceiptV1 JSON."""
        container = self._engine_container(source, bundle).with_exec(
            self._engine_args("audit", fork_sha, upstream_sha, bootstrap)
        )
        return await container.stdout()

    @function
    def materialize(
        self,
        source: dagger.Directory,
        bundle: dagger.File,
        fork_sha: str,
        upstream_sha: str,
        bootstrap: bool = False,
        allow_needs_review: bool = False,
    ) -> dagger.Directory:
        """Return the deterministic upstream-plus-overlay candidate directory."""
        return self._materialized(
            source,
            bundle,
            fork_sha,
            upstream_sha,
            bootstrap,
            allow_needs_review,
        ).directory("/candidate")

    @function
    async def validate(self, source: dagger.Directory) -> str:
        """Validate a checked-out candidate without requiring Git credentials."""
        structure = (
            dag.container(platform=TARGET_PLATFORM)
            .from_(PYTHON_GIT_IMAGE)
            .with_mounted_directory("/src", source)
            .with_workdir("/src")
            .with_exec(
                [
                    "python3",
                    ".fork-sync/sync_engine.py",
                    "validate-candidate",
                    "--candidate-root",
                    ".",
                ]
            )
        )
        overrides = json.loads(await source.file("overrides/manifest.json").contents())
        blueprints = sorted(item["id"] for item in overrides["locally_modified"])
        validation, _ = await asyncio.gather(
            self._validate_candidate(source, blueprints),
            structure.sync(),
        )
        return f"{json.dumps({'status': 'passed', **validation}, indent=2, sort_keys=True)}\n"

    @function
    async def check(
        self,
        source: dagger.Directory,
        bundle: dagger.File,
        fork_sha: str,
        upstream_sha: str,
        bootstrap: bool = False,
        allow_needs_review: bool = False,
    ) -> str:
        """Materialize and validate a candidate, returning its bound receipt JSON."""
        materialized = self._materialized(
            source,
            bundle,
            fork_sha,
            upstream_sha,
            bootstrap,
            allow_needs_review,
        )
        candidate = materialized.directory("/candidate")
        receipt = json.loads(await materialized.file("/receipt.json").contents())
        available_blueprints = set(await candidate.directory("blueprints").entries())
        changed_blueprints = [
            blueprint
            for blueprint in receipt["changed_blueprints"]
            if blueprint in available_blueprints
        ]
        receipt.update(await self._validate_candidate(candidate, changed_blueprints))
        return f"{json.dumps(receipt, indent=2, sort_keys=True)}\n"
