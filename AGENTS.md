# Repository Agent Instructions

Follow `CONTRIBUTING.md` for all development and GitHub operations.

## Development workflow

- Work from an Issue with a stated purpose, completion criteria, affected areas,
  test perspectives, and whether a Release update may be needed.
- Use one branch for one Issue-sized purpose. Split unrelated changes into
  separate Issues and branches.
- Record the related Issue in the Pull Request. If work reveals additional
  scope, do not expand the branch. Identify an existing Issue or report the
  scope to the maintainer; create a new Issue only with maintainer approval.
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
  and link an existing follow-up Issue or report it for maintainer triage.
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

## Complexity control

Ponytail and similar minimalism aids are advisory complexity controls. They
do not override repository requirements or authorize removal of existing
safeguards.

- Use Ponytail `lite` by default in this repository.
- Ponytail `full` may be used only for a small and explicitly scoped
  implementation.
- Do not use Ponytail `ultra` unless the maintainer explicitly requests it
  for a specific task.
- Do not run a whole-repository Ponytail audit unless the maintainer
  explicitly requests it.
- Ponytail review is an additional over-engineering review. It does not
  replace correctness, security, performance, compatibility, licensing,
  Release, or manual review.
- `CONTRIBUTING.md` and the relevant manual test checklist override
  Ponytail's preference for the smallest possible test.
- Do not remove or weaken validation, process-identity checks, transaction
  recovery, data-loss prevention, installer isolation, license checks,
  Release gates, or compatibility behavior merely to reduce code or file
  count.
- Do not simplify code that manages OBS processes, user recordings,
  application settings, credentials, installer cleanup, or external runtime
  boundaries without an explicitly scoped requirement and appropriate
  regression tests.
- Do not create follow-up Issues without maintainer approval. Link an existing
  Issue when available; otherwise report the candidate for maintainer triage.
- When choosing discretionary work, prefer observed user-flow failures over
  speculative hardening. Required correctness, security, licensing, and
  Release gates remain mandatory.
- Before introducing an abstraction, require either two concrete production
  implementations, a documented external-boundary or testability need, or
  explicit maintainer approval.
- Prefer the smallest correct change after tracing the complete affected
  flow. A small diff in the wrong layer is not an acceptable simplification.
- The current product priority is one complete real League of Legends
  recording and replay flow, not minimizing the repository's total line
  count.
- Passing a Ponytail review or automated tests does not prove that real LoL,
  OBS, mpv, FFmpeg, Windows installer, or uninstall flows work.
- If the Ponytail plugin is unavailable or disabled, the rules in this
  section still apply as repository instructions.
