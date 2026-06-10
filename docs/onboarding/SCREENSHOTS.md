# Onboarding screenshots

PNGs live in `mcpbrain/wizard/img/` (shipped in the wheel, served at `/img/<name>`).
Capture at ~1200px wide, light mode, and **redact any personal data** (email,
workspace names, real list names) — these ship publicly. Until a file exists, its
`<img>` hides itself (`onerror`), so the wizard ships text-only meanwhile.

| Filename | What it must show |
|----------|-------------------|
| `google-unverified-advanced.png` | Google consent "hasn't verified this app" → Advanced → Continue |
| `clickup-settings.png` | ClickUp avatar menu, Settings highlighted |
| `clickup-apps-token.png` | Settings → Apps → API Token (Generate/Copy) |
| `clickup-list-copylink.png` | Right-click List → Copy link |
| `clickup-list-id-url.png` | Copied URL with the `/li/<id>` portion highlighted |
| `claude-quit-reopen.png` | macOS menu bar Claude → Quit |
| `cowork-projects-plus.png` | Cowork Projects → + (the 3 options) |
| `cowork-use-existing-folder.png` | "Use an existing folder" picker |
| `cowork-project-create.png` | Naming the project + Create |
| `cowork-scheduled-new.png` | Scheduled → New → Local |
| `cowork-scheduled-fields.png` | Routine form: name, folder, Schedule = Hourly |
| `cowork-run-now-allow.png` | Run now + "Always allow" prompt |

The wizard currently references these filenames (Task 13): `clickup-apps-token.png`,
`clickup-list-copylink.png`, `cowork-scheduled-fields.png`, `cowork-use-existing-folder.png`.
Add the rest as the corresponding expanders gain screenshots.
