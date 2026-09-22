from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github/workflows/upstream-sync.yml").read_text()
CONTRACT = json.loads((ROOT / ".fork-sync/contract.json").read_text())


class PublisherWorkflowContractTests(unittest.TestCase):
    def test_schedule_is_timezone_aware_monday_at_nine(self) -> None:
        self.assertIn('cron: "0 9 * * 1"', WORKFLOW)
        self.assertIn('timezone: "America/New_York"', WORKFLOW)

    def test_contract_is_draft_only_and_singleton(self) -> None:
        self.assertTrue(CONTRACT["publication"]["draft"])
        self.assertFalse(CONTRACT["publication"]["auto_merge"])
        self.assertIn("group: templates-upstream-sync", WORKFLOW)
        self.assertIn("cancel-in-progress: false", WORKFLOW)
        self.assertNotIn("gh pr merge", WORKFLOW)

    def test_publisher_requires_bot_identity_credentials_and_signing_proof(self) -> None:
        for required in (
            "GH_TOKEN",
            "BOT_NAME",
            "BOT_EMAIL",
            "SSH_SIGNING_KEY",
            "git commit-tree -S",
            'git verify-commit "$COMMIT_SHA"',
        ):
            self.assertIn(required, WORKFLOW)
        self.assertNotIn("commit.gpgsign false", WORKFLOW)
        self.assertNotIn("--no-gpg-sign", WORKFLOW)

    def test_publisher_is_bound_to_one_branch_with_exact_lease(self) -> None:
        branch = CONTRACT["publication"]["branch"]
        self.assertIn(f"BRANCH={branch}", WORKFLOW)
        self.assertIn('--force-with-lease="refs/heads/${BRANCH}:${REMOTE_SHA}"', WORKFLOW)
        self.assertIn("gh pr list", WORKFLOW)
        self.assertIn("gh pr edit", WORKFLOW)
        self.assertIn("gh pr create", WORKFLOW)
        self.assertIsNone(re.search(r"git push[^\n]*\bmain\b", WORKFLOW))

    def test_non_ready_receipts_cannot_publish(self) -> None:
        self.assertIn("if: needs.validate.outputs.status == 'ready'", WORKFLOW)
        self.assertIn('if [ "$(jq -r .status /tmp/receipt.json)" != "ready" ]', WORKFLOW)
        self.assertIn("The upstream audit failed closed", WORKFLOW)
        self.assertIn("requires attended review", WORKFLOW)

    def test_candidate_digest_and_two_exact_parents_are_verified(self) -> None:
        self.assertIn("Candidate digest mismatch", WORKFLOW)
        self.assertIn('-p "$FORK_SHA" -p "$UPSTREAM_SHA"', WORKFLOW)
        self.assertIn('!= "$FORK_SHA $UPSTREAM_SHA"', WORKFLOW)


if __name__ == "__main__":
    unittest.main()
