# Repository Agent Instructions

Follow `CONTRIBUTING.md` for all development and GitHub operations.

## Development workflow

- Work from an Issue with a stated purpose, completion criteria, affected areas,
  test perspectives, and whether a Release update may be needed.
- Use one branch for one Issue-sized purpose. Split unrelated changes into
  separate Issues and branches.
- Record the related Issue in the Pull Request. If work reveals additional
  scope, create or identify another Issue instead of expanding the branch.
- Before opening a Pull Request, run Ruff and pytest as documented in
  `CONTRIBUTING.md` unless the change is documentation-only. Record skipped
  checks and their reasons in the Pull Request.
- Run the Windows build and packaged self-check when changing packaging,
  runtime dependencies, GUI startup, OBS, mpv, FFmpeg, paths, or the installer.
- Perform relevant checks from `docs/manual-test-checklist.md` when behavior
  depends on a real Windows, LoL, OBS, mpv, or FFmpeg environment. Record the
  environment, scenarios, and results in the Pull Request.
- Keep `main` releasable. Merge only after required CI, review fixes, and any
  required real-device checks have completed.
- Create a Release only when the merged change is intended for distribution;
  ordinary merges do not require a tag or Release.

## Review handling

Classify review feedback before responding:

- `Must`: correctness, security, data loss, compatibility, required tests, or
  violations of repository rules. Resolve before merge.
- `Should`: maintainability, clarity, resilience, or useful test improvements.
  Resolve before merge when practical; otherwise document why it is deferred
  and link or create a follow-up Issue.
- `Nit`: optional wording, style, or preference with no material behavioral
  effect. Apply when useful; declining it only requires a brief reason.

Summarize each category in the Pull Request. List feedback intentionally not
addressed together with its reason; do not silently ignore review comments.

## Naming

- Use `feature/`, `fix/`, `docs/`, or `chore/` branch prefixes.
- Never use `codex/`, `ai/`, `agent/`, or a tool/vendor name in a branch name.
- Do not prefix or suffix commit messages, Pull Request titles, or Pull Request
  descriptions with `Codex`, `OpenAI`, `AI generated`, or similar attribution.
- Describe the change itself. Do not identify which automated tool performed it
  unless the user explicitly requests that attribution.
- Write commit messages and maintainer-authored Pull Request titles and
  descriptions in Japanese. English submissions from external contributors are
  acceptable.
- Maintain `README.md` and `CHANGELOG.md` in Japanese, and maintain
  `README.en.md` and `CHANGELOG.en.md` as their English counterparts.
- Write GitHub Release notes in both Japanese and English, with Japanese first.
- Keep identifiers, commands, file names, API names, and product names in their
  original language when appropriate.
- Use concise Pull Request titles such as `Windows通知を設定可能にする` or
  conventional titles such as `fix: 試合開始検知を改善`.

## Tool-assisted work

- Treat automated assistants as review or implementation support, not as
  repository authors or contributors.
- Do not put assistant, model, tool, vendor, or generation-source attribution
  in branch names, commits, Pull Request titles or descriptions, Issues,
  review summaries, changelogs, or Release notes.
- Do not add automated-assistant `Co-authored-by` trailers or similar attribution
  metadata to commits.
- Do not use bot identities for ordinary developer commits when a maintainer is
  responsible for the change. Repository history must describe what changed,
  not which supporting tool was used.
