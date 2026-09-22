#!/usr/bin/env python3
"""Deterministic, credential-free fork synchronization engine."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
BLUEPRINT_RE = re.compile(r"^blueprints/([^/]+)/")
GENERATED_OVERLAY_PARTS = {".git", ".venv", "__pycache__"}
MATERIALIZED_SYMLINK_PATH = "app/public/blueprints"
MATERIALIZED_SYMLINK_TARGET = "../../blueprints"
MATERIALIZED_SYMLINK_SOURCE = "blueprints"


class SyncError(RuntimeError):
    pass


def git(repo: Path, *args: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if check and completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise SyncError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout


def canonical_relative_path(raw: str) -> str:
    path = PurePosixPath(raw)
    if not raw or raw.startswith("/") or ".." in path.parts or path.as_posix() != raw:
        raise SyncError(f"non-canonical repository path: {raw!r}")
    return raw


def load_contract(path: Path) -> dict[str, Any]:
    try:
        contract = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise SyncError(f"invalid sync contract: {error}") from error
    if contract.get("schema_version") != 1:
        raise SyncError("sync contract schema_version must be 1")
    for side in ("fork", "upstream"):
        value = contract.get(side)
        if not isinstance(value, dict) or not value.get("repository") or not value.get("branch"):
            raise SyncError(f"sync contract {side} repository and branch are required")
    accepted = contract.get("last_accepted_upstream_sha", "")
    if not SHA_RE.fullmatch(accepted):
        raise SyncError("last_accepted_upstream_sha must be an exact lowercase SHA")
    owned = contract.get("owned_paths")
    tombstones = contract.get("tombstones")
    if not isinstance(owned, list) or not isinstance(tombstones, list):
        raise SyncError("owned_paths and tombstones must be arrays")
    for value in [*owned, *tombstones]:
        if not isinstance(value, str):
            raise SyncError("contract paths must be strings")
        canonical_relative_path(value)
    if owned != sorted(set(owned)) or tombstones != sorted(set(tombstones)):
        raise SyncError("owned_paths and tombstones must be unique and sorted")
    for owned_path in owned:
        for tombstone in tombstones:
            if owned_path == tombstone or owned_path.startswith(f"{tombstone}/"):
                raise SyncError(f"owned path {owned_path} overlaps tombstone {tombstone}")
    limits = contract.get("limits", {})
    for key in ("max_incoming_commits", "max_changed_files"):
        if not isinstance(limits.get(key), int) or limits[key] < 0:
            raise SyncError(f"limits.{key} must be a non-negative integer")
    publication = contract.get("publication", {})
    if publication.get("draft") is not True or publication.get("auto_merge") is not False:
        raise SyncError("publication must be draft-only with auto_merge disabled")
    return contract


def validate_sha(repo: Path, value: str, label: str) -> None:
    if not SHA_RE.fullmatch(value):
        raise SyncError(f"{label} must be an exact lowercase SHA")
    git(repo, "cat-file", "-e", f"{value}^{{commit}}")


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", ancestor, descendant],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode not in (0, 1):
        raise SyncError(f"git merge-base failed: {completed.stderr.strip()}")
    return completed.returncode == 0


def changed_paths(repo: Path, base: str, head: str) -> list[str]:
    return sorted(
        path
        for path in git(repo, "diff", "--name-only", "--diff-filter=ACMRTD", base, head)
        .splitlines()
        if path
    )


def upstream_structural_violations(
    repo: Path,
    upstream_sha: str,
    tombstones: Iterable[str],
) -> list[str]:
    violations: list[str] = []
    declared_tombstones = tuple(tombstones)
    for line in git(repo, "ls-tree", "-r", upstream_sha).splitlines():
        metadata, path = line.split("\t", 1)
        mode, object_type, _object_sha = metadata.split(" ", 2)
        if any(path_matches(path, tombstone) for tombstone in declared_tombstones):
            continue
        if mode == "120000":
            target = git(repo, "cat-file", "-p", f"{upstream_sha}:{path}")
            if path != MATERIALIZED_SYMLINK_PATH:
                violations.append(f"symlink:{path}")
            elif target != MATERIALIZED_SYMLINK_TARGET:
                violations.append(f"symlink-target:{path}")
        elif mode == "160000" or object_type == "commit":
            violations.append(f"submodule:{path}")
    if "meta.json" not in declared_tombstones:
        completed = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-e", f"{upstream_sha}:meta.json"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if completed.returncode == 0:
            violations.append("forbidden-root:meta.json")
    return sorted(violations)


def path_matches(path: str, declared: str) -> bool:
    return path == declared or path.startswith(f"{declared}/")


def ignored_overlay_path(path: PurePosixPath) -> bool:
    """Return true for local/generated material that is never part of the overlay."""
    if any(part in GENERATED_OVERLAY_PARTS for part in path.parts):
        return True
    if len(path.parts) >= 2 and path.parts[:2] == (".dagger", "sdk"):
        return True
    return path.name == ".DS_Store" or path.suffix == ".pyc"


def overlay_digest(root: Path, declared_paths: Iterable[str]) -> str:
    digest = hashlib.sha256()
    seen: set[Path] = set()
    for declared in sorted(declared_paths):
        start = root / declared
        if not start.exists() and not start.is_symlink():
            raise SyncError(f"owned path is missing from overlay: {declared}")
        paths = [start]
        if start.is_dir() and not start.is_symlink():
            paths.extend(sorted(start.rglob("*")))
        for path in paths:
            relative_path = PurePosixPath(path.relative_to(root).as_posix())
            if ignored_overlay_path(relative_path):
                continue
            if path in seen:
                continue
            seen.add(path)
            relative = relative_path.as_posix().encode()
            if path.is_symlink():
                raise SyncError(f"overlay contains forbidden symlink: {relative.decode()}")
            if path.is_dir():
                digest.update(b"D\0" + relative + b"\0")
                continue
            if not path.is_file():
                raise SyncError(f"overlay contains non-regular file: {relative.decode()}")
            executable = b"1" if path.stat().st_mode & stat.S_IXUSR else b"0"
            digest.update(b"F\0" + relative + b"\0" + executable + b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for current, directories, files in os.walk(root):
        directories.sort()
        files.sort()
        current_path = Path(current)
        for directory in directories:
            path = current_path / directory
            relative = path.relative_to(root).as_posix().encode()
            if path.is_symlink():
                raise SyncError(f"candidate contains forbidden symlink: {relative.decode()}")
            digest.update(b"D\0" + relative + b"\0")
        for filename in files:
            path = current_path / filename
            relative = path.relative_to(root).as_posix().encode()
            if path.is_symlink():
                raise SyncError(f"candidate contains forbidden symlink: {relative.decode()}")
            if not path.is_file():
                raise SyncError(f"candidate contains non-regular file: {relative.decode()}")
            executable = b"1" if path.stat().st_mode & stat.S_IXUSR else b"0"
            digest.update(b"F\0" + relative + b"\0" + executable + b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def build_audit(
    repo: Path,
    overlay_root: Path,
    contract: dict[str, Any],
    fork_sha: str,
    upstream_sha: str,
    bootstrap: bool = False,
) -> dict[str, Any]:
    validate_sha(repo, fork_sha, "fork_sha")
    validate_sha(repo, upstream_sha, "upstream_sha")
    accepted = contract["last_accepted_upstream_sha"]
    validate_sha(repo, accepted, "last_accepted_upstream_sha")
    source_digest = overlay_digest(overlay_root, contract["owned_paths"])

    if is_ancestor(repo, upstream_sha, fork_sha):
        return {
            "schema_version": 1,
            "status": "noop",
            "fork_sha": fork_sha,
            "upstream_sha": upstream_sha,
            "last_accepted_upstream_sha": accepted,
            "upstream_rewrite": False,
            "incoming_commit_count": 0,
            "incoming_file_count": 0,
            "changed_paths": [],
            "changed_blueprints": [],
            "owned_path_collisions": [],
            "thresholds_exceeded": [],
            "structural_violations": [],
            "source_digest": source_digest,
            "candidate_digest": None,
            "validations": [],
        }

    rewrite = not is_ancestor(repo, accepted, upstream_sha)
    incoming_paths = [] if rewrite else changed_paths(repo, accepted, upstream_sha)
    incoming_commits = (
        0 if rewrite else int(git(repo, "rev-list", "--count", f"{accepted}..{upstream_sha}").strip())
    )
    collisions = sorted(
        path
        for path in incoming_paths
        if any(path_matches(path, owned) for owned in contract["owned_paths"])
    )
    changed_blueprints = sorted(
        {match.group(1) for path in incoming_paths if (match := BLUEPRINT_RE.match(path))}
    )
    limits = contract["limits"]
    structural_violations = upstream_structural_violations(
        repo,
        upstream_sha,
        contract["tombstones"],
    )
    exceeded = []
    if incoming_commits > limits["max_incoming_commits"]:
        exceeded.append("max_incoming_commits")
    if len(incoming_paths) > limits["max_changed_files"]:
        exceeded.append("max_changed_files")

    if structural_violations:
        status = "failed"
    elif rewrite and not limits.get("allow_upstream_rewrite", False):
        status = "failed"
    elif bootstrap or collisions or exceeded:
        status = "needs_review"
    else:
        status = "ready"
    return {
        "schema_version": 1,
        "status": status,
        "fork_sha": fork_sha,
        "upstream_sha": upstream_sha,
        "last_accepted_upstream_sha": accepted,
        "upstream_rewrite": rewrite,
        "incoming_commit_count": incoming_commits,
        "incoming_file_count": len(incoming_paths),
        "changed_paths": incoming_paths,
        "changed_blueprints": changed_blueprints,
        "owned_path_collisions": collisions,
        "thresholds_exceeded": exceeded,
        "structural_violations": structural_violations,
        "source_digest": source_digest,
        "candidate_digest": None,
        "validations": [],
    }


def remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def copy_overlay_path(overlay_root: Path, candidate_root: Path, relative: str) -> None:
    source = overlay_root / relative
    target = candidate_root / relative
    if not source.exists() and not source.is_symlink():
        raise SyncError(f"owned path is missing from overlay: {relative}")
    if target.exists() or target.is_symlink():
        remove_path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        raise SyncError(f"owned path cannot be a symlink: {relative}")
    if source.is_dir():
        def ignore_generated(directory: str, names: list[str]) -> list[str]:
            directory_relative = Path(directory).relative_to(overlay_root)
            return [
                name
                for name in names
                if ignored_overlay_path(
                    PurePosixPath((directory_relative / name).as_posix())
                )
            ]

        shutil.copytree(
            source,
            target,
            copy_function=shutil.copy2,
            ignore=ignore_generated,
        )
    elif source.is_file():
        shutil.copy2(source, target)
    else:
        raise SyncError(f"owned path is not regular: {relative}")


def normalize_modes(root: Path) -> None:
    for current, directories, files in os.walk(root):
        current_path = Path(current)
        current_path.chmod(0o755)
        for directory in directories:
            (current_path / directory).chmod(0o755)
        for filename in files:
            path = current_path / filename
            executable = bool(path.stat().st_mode & stat.S_IXUSR)
            path.chmod(0o755 if executable else 0o644)


def assert_no_submodules(repo: Path, upstream_sha: str) -> None:
    for line in git(repo, "ls-tree", "-r", upstream_sha).splitlines():
        if line.startswith("160000 "):
            raise SyncError(f"upstream candidate contains a submodule: {line.split(chr(9), 1)[-1]}")


def materialize_known_upstream_symlink(candidate_root: Path) -> None:
    link = candidate_root / MATERIALIZED_SYMLINK_PATH
    if not link.is_symlink():
        return
    if os.readlink(link) != MATERIALIZED_SYMLINK_TARGET:
        raise SyncError(f"unexpected symlink target: {MATERIALIZED_SYMLINK_PATH}")
    source = candidate_root / MATERIALIZED_SYMLINK_SOURCE
    if source.is_symlink() or not source.is_dir():
        raise SyncError(f"symlink source is not a regular directory: {MATERIALIZED_SYMLINK_SOURCE}")
    link.unlink()
    shutil.copytree(source, link, copy_function=shutil.copy2)


def validate_candidate(candidate_root: Path) -> None:
    if (candidate_root / "meta.json").exists():
        raise SyncError("candidate contains forbidden root meta.json")
    for path in candidate_root.rglob("*"):
        relative = path.relative_to(candidate_root).as_posix()
        if ignored_overlay_path(PurePosixPath(relative)):
            raise SyncError(f"candidate contains generated overlay material: {relative}")
        if path.is_symlink():
            raise SyncError(f"candidate contains forbidden symlink: {relative}")
        if not path.is_dir() and not path.is_file():
            raise SyncError(f"candidate contains non-regular path: {relative}")
    contract = load_contract(candidate_root / ".fork-sync/contract.json")
    for tombstone in contract["tombstones"]:
        path = candidate_root / tombstone
        if path.exists() or path.is_symlink():
            raise SyncError(f"candidate retained tombstoned path: {tombstone}")


def materialize(
    repo: Path,
    overlay_root: Path,
    contract_path: Path,
    candidate_root: Path,
    fork_sha: str,
    upstream_sha: str,
    bootstrap: bool = False,
    allow_needs_review: bool = False,
) -> dict[str, Any]:
    contract = load_contract(contract_path)
    receipt = build_audit(repo, overlay_root, contract, fork_sha, upstream_sha, bootstrap)
    if receipt["status"] == "failed":
        raise SyncError("audit failed; candidate materialization is forbidden")
    if receipt["status"] == "needs_review" and not allow_needs_review:
        raise SyncError("audit requires review; pass --allow-needs-review only for attended bootstrap")
    if receipt["status"] == "noop":
        raise SyncError("upstream is already contained by the fork; no candidate is required")
    assert_no_submodules(repo, upstream_sha)

    if candidate_root.exists():
        if candidate_root.is_symlink() or not candidate_root.is_dir():
            raise SyncError("candidate output must be a directory")
        if any(candidate_root.iterdir()):
            raise SyncError("candidate output directory must be empty")
    else:
        candidate_root.mkdir(parents=True)

    with tempfile.NamedTemporaryFile(suffix=".tar") as archive:
        completed = subprocess.run(
            ["git", "-C", str(repo), "archive", "--format=tar", "-o", archive.name, upstream_sha],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if completed.returncode != 0:
            raise SyncError(f"failed to archive upstream tree: {completed.stderr.strip()}")
        with tarfile.open(archive.name) as tar:
            tar.extractall(candidate_root, filter="data")

    materialize_known_upstream_symlink(candidate_root)

    for relative in contract["owned_paths"]:
        copy_overlay_path(overlay_root, candidate_root, relative)
    for relative in contract["tombstones"]:
        target = candidate_root / relative
        if target.exists() or target.is_symlink():
            remove_path(target)

    candidate_contract_path = candidate_root / ".fork-sync/contract.json"
    candidate_contract = load_contract(candidate_contract_path)
    candidate_contract["last_accepted_upstream_sha"] = upstream_sha
    candidate_contract_path.write_text(f"{json.dumps(candidate_contract, indent=2)}\n")
    normalize_modes(candidate_root)
    validate_candidate(candidate_root)
    receipt["candidate_digest"] = tree_digest(candidate_root)
    return receipt


def write_receipt(receipt: dict[str, Any], path: Path | None) -> None:
    encoded = f"{json.dumps(receipt, indent=2, sort_keys=True)}\n"
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(encoded)
    print(encoded, end="")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("audit", "materialize"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--repo", type=Path, required=True)
        sub.add_argument("--overlay-root", type=Path, required=True)
        sub.add_argument("--contract", type=Path, required=True)
        sub.add_argument("--fork-sha", required=True)
        sub.add_argument("--upstream-sha", required=True)
        sub.add_argument("--receipt-out", type=Path)
        sub.add_argument("--bootstrap", action="store_true")
        if command == "materialize":
            sub.add_argument("--candidate-out", type=Path, required=True)
            sub.add_argument("--allow-needs-review", action="store_true")
    validate = subparsers.add_parser("validate-candidate")
    validate.add_argument("--candidate-root", type=Path, required=True)
    digest = subparsers.add_parser("digest")
    digest.add_argument("--root", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "validate-candidate":
            validate_candidate(args.candidate_root.resolve())
            print("candidate-valid")
            return 0
        if args.command == "digest":
            print(tree_digest(args.root.resolve()))
            return 0
        contract = load_contract(args.contract.resolve())
        if args.command == "audit":
            receipt = build_audit(
                args.repo.resolve(),
                args.overlay_root.resolve(),
                contract,
                args.fork_sha,
                args.upstream_sha,
                args.bootstrap,
            )
        else:
            receipt = materialize(
                args.repo.resolve(),
                args.overlay_root.resolve(),
                args.contract.resolve(),
                args.candidate_out.resolve(),
                args.fork_sha,
                args.upstream_sha,
                args.bootstrap,
                args.allow_needs_review,
            )
        write_receipt(receipt, args.receipt_out.resolve() if args.receipt_out else None)
        # A typed failed audit is still a valid receipt. Publishing is governed by
        # the receipt status, while workflows retain and report the evidence.
        return 0
    except SyncError as error:
        print(f"sync-error: {error}", file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
