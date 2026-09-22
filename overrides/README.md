# Blueprint Overrides

`manifest.json` is the machine-readable list of blueprint directories whose
runtime files intentionally differ from `Dokploy/templates:canary`.

The manifest starts empty after the deterministic rebaseline. A workflow,
watchlist entry, or fork-only CI file does not by itself make a blueprint an
override.

## Adding an override

1. Change the minimum required file under `blueprints/<id>/`.
2. Add the blueprint ID and a concrete reason to `manifest.json`.
3. Validate both `docker-compose.yml` and `template.toml` through Dagger.
4. Review the draft pull request; no workflow may merge it automatically.

Client Portal consumes the complete blueprint from the exact fork SHA for IDs
in this manifest, so every active override must keep Compose and TOML mutually
compatible.

## Removing an override

Remove the manifest entry and restore the complete upstream blueprint. The next
catalog dispatch will move Client Portal back to the upstream source.
