from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("sync_engine.py")
SPEC = importlib.util.spec_from_file_location("sync_engine", MODULE_PATH)
assert SPEC and SPEC.loader
ENGINE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ENGINE)


class SyncEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git_run("init")
        self.git_run("config", "user.name", "Test")
        self.git_run("config", "user.email", "test@example.com")
        self.git_run("config", "commit.gpgsign", "false")
        (self.repo / "blueprints/demo").mkdir(parents=True)
        (self.repo / "blueprints/demo/meta.json").write_text('{"id":"demo"}\n')
        (self.repo / "blueprints/demo/template.toml").write_text("[config]\n")
        (self.repo / "blueprints/demo/docker-compose.yml").write_text("services: {}\n")
        (self.repo / "obsolete.txt").write_text("remove\n")
        self.commit("base")
        self.base = self.git_run("rev-parse", "HEAD").strip()

        self.git_run("switch", "-c", "upstream")
        (self.repo / "README.md").write_text("upstream\n")
        self.commit("upstream")
        self.upstream = self.git_run("rev-parse", "HEAD").strip()

        self.git_run("switch", "-c", "fork", self.base)
        (self.repo / ".fork-sync").mkdir()
        (self.repo / ".fork-sync/sync_engine.py").write_text("engine\n")
        self.contract_path = self.repo / ".fork-sync/contract.json"
        self.write_contract()
        self.commit("fork overlay")
        self.fork = self.git_run("rev-parse", "HEAD").strip()
        self.overlay = self.root / "overlay"
        shutil.copytree(self.repo / ".fork-sync", self.overlay / ".fork-sync")
        self.contract_path = self.overlay / ".fork-sync/contract.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def git_run(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout

    def commit(self, message: str) -> None:
        self.git_run("add", "-A")
        self.git_run("commit", "-m", message)

    def write_contract(self, **limits: int) -> None:
        contract = {
            "schema_version": 1,
            "fork": {"repository": "masonjames/templates", "branch": "main"},
            "upstream": {"repository": "Dokploy/templates", "branch": "canary"},
            "last_accepted_upstream_sha": self.base,
            "owned_paths": [".fork-sync"],
            "tombstones": ["obsolete.txt"],
            "limits": {
                "max_incoming_commits": limits.get("max_incoming_commits", 100),
                "max_changed_files": limits.get("max_changed_files", 500),
                "allow_upstream_rewrite": False,
            },
            "validation": {"pnpm_version": "10.15.1", "security_workflows": []},
            "publication": {
                "branch": "upstream-sync/dokploy-canary",
                "label": "upstream-sync",
                "draft": True,
                "auto_merge": False,
            },
        }
        self.contract_path.write_text(f"{json.dumps(contract, indent=2)}\n")

    def audit(self, **kwargs):
        return ENGINE.build_audit(
            self.repo,
            self.overlay,
            ENGINE.load_contract(self.contract_path),
            kwargs.get("fork_sha", self.fork),
            kwargs.get("upstream_sha", self.upstream),
            kwargs.get("bootstrap", False),
        )

    def test_clean_descendant_is_ready(self) -> None:
        receipt = self.audit()
        self.assertEqual(receipt["status"], "ready")
        self.assertEqual(receipt["incoming_commit_count"], 1)
        self.assertEqual(receipt["changed_paths"], ["README.md"])

    def test_owned_path_collision_requires_review(self) -> None:
        self.git_run("switch", "upstream")
        (self.repo / ".fork-sync").mkdir(exist_ok=True)
        (self.repo / ".fork-sync/sync_engine.py").write_text("upstream changed\n")
        self.commit("collide")
        upstream = self.git_run("rev-parse", "HEAD").strip()
        receipt = self.audit(upstream_sha=upstream)
        self.assertEqual(receipt["status"], "needs_review")
        self.assertEqual(receipt["owned_path_collisions"], [".fork-sync/sync_engine.py"])

    def test_threshold_excess_requires_review(self) -> None:
        self.write_contract(max_incoming_commits=0)
        receipt = self.audit()
        self.assertEqual(receipt["status"], "needs_review")
        self.assertEqual(receipt["thresholds_exceeded"], ["max_incoming_commits"])

    def test_changed_file_threshold_requires_review(self) -> None:
        self.write_contract(max_changed_files=0)
        receipt = self.audit()
        self.assertEqual(receipt["status"], "needs_review")
        self.assertEqual(receipt["thresholds_exceeded"], ["max_changed_files"])

    def test_contained_upstream_is_noop(self) -> None:
        receipt = self.audit(upstream_sha=self.base)
        self.assertEqual(receipt["status"], "noop")
        self.assertEqual(receipt["incoming_commit_count"], 0)
        self.assertEqual(receipt["incoming_file_count"], 0)

    def test_upstream_rewrite_fails_closed(self) -> None:
        self.git_run("switch", "--orphan", "rewritten")
        (self.repo / "replacement.txt").write_text("replacement\n")
        self.commit("rewrite")
        rewritten = self.git_run("rev-parse", "HEAD").strip()
        receipt = self.audit(upstream_sha=rewritten)
        self.assertEqual(receipt["status"], "failed")
        self.assertTrue(receipt["upstream_rewrite"])

    def test_upstream_symlink_fails_closed(self) -> None:
        self.git_run("switch", "upstream")
        os.symlink("README.md", self.repo / "linked-readme")
        self.commit("symlink")
        upstream = self.git_run("rev-parse", "HEAD").strip()
        receipt = self.audit(upstream_sha=upstream)
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["structural_violations"], ["symlink:linked-readme"])

    def test_materialize_starts_from_upstream_and_applies_overlay_and_tombstone(self) -> None:
        candidate = self.root / "candidate"
        receipt = ENGINE.materialize(
            self.repo,
            self.overlay,
            self.contract_path,
            candidate,
            self.fork,
            self.upstream,
        )
        self.assertEqual(receipt["status"], "ready")
        self.assertEqual((candidate / "README.md").read_text(), "upstream\n")
        self.assertEqual((candidate / ".fork-sync/sync_engine.py").read_text(), "engine\n")
        self.assertFalse((candidate / "obsolete.txt").exists())
        candidate_contract = json.loads((candidate / ".fork-sync/contract.json").read_text())
        self.assertEqual(candidate_contract["last_accepted_upstream_sha"], self.upstream)
        self.assertRegex(receipt["candidate_digest"], r"^sha256:[0-9a-f]{64}$")

    def test_materialize_excludes_generated_overlay_files(self) -> None:
        generated = self.overlay / ".fork-sync/__pycache__"
        generated.mkdir()
        (generated / "sync_engine.cpython-312.pyc").write_bytes(b"generated")
        candidate = self.root / "candidate"
        ENGINE.materialize(
            self.repo,
            self.overlay,
            self.contract_path,
            candidate,
            self.fork,
            self.upstream,
        )
        self.assertFalse((candidate / ".fork-sync/__pycache__").exists())

    def test_materialize_refuses_needs_review_without_attended_override(self) -> None:
        with self.assertRaisesRegex(ENGINE.SyncError, "requires review"):
            ENGINE.materialize(
                self.repo,
                self.overlay,
                self.contract_path,
                self.root / "candidate",
                self.fork,
                self.upstream,
                bootstrap=True,
            )


if __name__ == "__main__":
    unittest.main()
