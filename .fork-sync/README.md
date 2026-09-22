# Governed upstream synchronization

This fork is materialized from the exact `Dokploy/templates:canary` commit plus
the paths declared in `contract.json`. Dagger performs the credential-free
audit, materialization, and validation. A separate publisher may update one
draft pull request only after the receipt status is `ready`.

The contract intentionally fails closed for upstream rewrites, undeclared
overlay material, owned-path collisions, more than 100 incoming commits, or
more than 500 changed files. Root metadata, submodules, special files, and
symlinks are structural violations unless an exact path is explicitly
tombstoned. The sole symlink exception is upstream's exact
`app/public/blueprints -> ../../blueprints` link, which is materialized as
ordinary files; a changed target or any additional symlink fails closed.
The one-time rebaseline is an attended exception to drift limits only:
it may be materialized and validated with `bootstrap`, but it is never
published by the scheduled workflow and cannot bypass structural violations.

## Fork policy

| Repository condition | Default treatment |
|---|---|
| No durable source difference | Track upstream directly or maintain a mirror. |
| Small declarative customization | Consume upstream plus a versioned overlay or patch artifact. |
| Workflow-only customization | Keep scheduling and reusable workflows in the operations repository where practical. |
| Persistent source changes that are built or shipped | Maintain a governed fork with this sync contract. |
| Change accepted upstream | Remove the local patch and reassess whether the fork can be retired. |

`masonjames/templates` remains a fork for the two-cycle pilot because it owns
catalog curation and Client Portal notifications. It currently has no active
blueprint content override. After two successful Monday cycles, reassess direct
upstream consumption plus a small external overlay before onboarding another
fork.

## Operator contract

- Run `.fork-sync/run-dagger.sh call audit ...` without repository credentials.
- A `noop` receipt creates no branch or pull request.
- `needs_review` and `failed` receipts never enter the publisher.
- `ready` may update only `upstream-sync/dokploy-canary` with exact
  force-with-lease protection and a verified signed two-parent commit.
- The bot cannot merge or deploy. Missing token, identity, signing key, or
  signature proof is terminal.
