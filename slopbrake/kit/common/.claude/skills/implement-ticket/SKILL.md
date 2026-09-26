---
name: implement-ticket
description: Implement a ticket or spec end to end - TDD at agreed seams, the scripts/check gate, a fresh-context review that commits its fixes, and a PR with a computed door. User-invoked.
disable-model-invocation: true
---

Each step ends on a **Done when**. Don't start a step until the previous one is done.

1. **Seams.** Read the ticket/spec (tracker: `docs/agents/issue-tracker.md`). Create a branch off the base (`git switch -c <type>/<slug>`). Write the seams under test into the ticket file under `## Seams`: the public entry points the tests will call.
   Done when: the ticket lists the seams and you are on the feature branch.

2. **Build test-first** with the `tdd` skill: one failing test, the least code that passes it, repeat. Save the output of the first red run (for a bug fix: the regression test failing on the unfixed code) to quote in Evidence.
   Done when: every behaviour in the ticket has a test that was red before it was green.

3. **Gate.** Commit, then run `scripts/check` (full: includes the mutation floor on changed lines). Fix until it exits 0; add tests where mutants survive.
   Done when: `scripts/check` exits 0 on a clean working tree.

4. **Review in fresh contexts.** Launch two `reviewer` subagents in parallel, each given only: `BASE`, `SPEC` (ticket path), `GATE` (the step 3 output), and `MODE: review AXIS=standards` or `MODE: review AXIS=spec`. Never pass this conversation. Then launch one `reviewer` with `MODE: fix`, the same pointers, and both reports verbatim. It commits `review:` fixes and re-gates, at most 3 rounds.
   Done when: the fix-mode reviewer returned its Review log, Open questions and MECHANICAL list, and `scripts/check` is green.

5. **Door.** Run `python3 scripts/slopbrake/door_classify.py --base <base>`. You may raise the result to one-way; never lower it. For one-way, write the reason and a rollback or mitigation plan.
   Done when: the door and its reasons are known.

6. **PR body.** Write it with the `pr` skill: Summary visual, Evidence (the saved red run, then green), Merge Danger (Door + Blast Radius), Review log, Open questions. Save it to `docs/agents/prs/<branch>.md` and commit it. Check it: `python3 scripts/slopbrake/pr_body_check.py --body-file docs/agents/prs/<branch>.md --base <base>`.
   Done when: the PR-body check prints `ok`.

7. **Publish.**
   - GitHub remote and pushing allowed for this repo: `git push -u origin <branch>`, then `gh pr create --body-file docs/agents/prs/<branch>.md` with `--draft` for a one-way door.
   - No GitHub remote (local-only repo): the branch plus its committed PR body is the PR. Do not merge it yourself.
   - Two-way + gate green + reviewer clean is auto-merge *eligible* (rule G3); merging stays with the operator unless the repo's CLAUDE.md says otherwise. One-way doors are never auto-merged.
   Done when: the PR exists (remote PR URL, or branch + PR body path), and you have told the operator the door, the blast radius and any open questions.

8. **Retro input.** If the operator corrected you, or the reviewer tagged anything MECHANICAL, say so in your final message so `/retro` can pick it up.
