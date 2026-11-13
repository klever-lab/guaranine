# Guaranine documentation

Start with [SETUP.md](../SETUP.md) for first-time deployment. These guides own the ongoing documentation:

- [Architecture](architecture.md) — component ownership, image flow, controller resources and availability limits.
- [Laptop hardware](hardware.md) — model inventory, Wi-Fi coverage, boot requirements and qualification evidence.
- [Networking and security](security.md) — tailnet policy, LAN isolation, workload restrictions and private material.
- [Operations](operations.md) — image batches, node retirement, backups, recovery, migration and OS updates.
- [Development](development.md) — local checks, CI, disposable image tests and dependency pins.
- [Superseded approaches](history.md) — earlier platforms and removed workflows, with their Git references.

_Generated from commit `b4e124f` on 2026-11-12._

## Check for drift

Compare the generation commit with the current checkout before relying on specific implementation details:

```bash
git status --short
git diff b4e124f..HEAD --stat
git diff b4e124f --stat
```

The commit comparison shows committed changes; the final command also includes tracked working-tree changes. Check untracked files separately through `git status`.

For a broad refresh, use `$generate_docs` and inspect the current `AGENTS.md`, `CLAUDE.md` and `docs/` before writing. Preserve repository rules and handwritten material unless the user authorizes changes. Read the current README, setup guide and repository instructions, then inventory all current non-ignored files with `git ls-files -co --exclude-standard` and read them. Include newly added files; earlier plans and changed-file lists are not a complete inventory. Never read ignored operator files to generate public docs.

Derive details from current code, profiles, manifests, tests and workflows. Keep test coverage, recorded test results and pending hardware/live-network verification distinct. For a targeted change, update the one owning guide and its links. Before committing, check Markdown, local links and anchors, index coverage, the `CLAUDE.md` symlink and preservation of `AGENTS.md` rules. Refresh the generation stamp after a full regeneration.

## Notes

- These docs are AI-generated, after-the-fact documentation of the implementation, not a design specification.
- Each fact belongs in exactly one guide. Link to its owner instead of repeating information in other docs, README.md or AGENTS.md. Setup instructions belong in SETUP.md.
- Experiments, dead ends and superseded approaches belong in their own topic documentation, separate from current instructions.
- Keep each doc file under roughly 400 lines. Split larger files by sub-topic and list every part here.
