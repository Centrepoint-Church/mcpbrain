# mcpbrain wheel index

This branch is the **published PEP 503 wheel index** for `mcpbrain`, served by
GitHub Pages at <https://centrepoint-church.github.io/mcpbrain/simple/>.

It is an **orphan branch** — it shares no history with `main`, so published wheels
never enter `main`'s history and never appear in a source checkout.

Do not edit by hand. `bin/release.py --dist <worktree>` regenerates it; see
`docs/RELEASE-RUNBOOK.md` on `main`.

```bash
git worktree add ../mcpbrain-pages gh-pages     # once per machine
uv run python bin/release.py --dist ../mcpbrain-pages
cd ../mcpbrain-pages && git add -A && git commit -m "release: mcpbrain X.Y.Z" && git push
```

`.nojekyll` is required: without it Pages runs Jekyll, which skips files and
directories beginning with an underscore.
