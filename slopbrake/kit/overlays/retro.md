
## Our additions (Slopbrake)

5. Also read: PR review comments since the last retro, `review:` commits (`git log --grep '^review:'`), MECHANICAL tags in PR bodies under `docs/agents/prs/`, and `scripts/check` failures.
6. For each mechanical finding, implement the check now: show it failing on a fixture, then passing. Prose instead of a check needs a written reason in the ledger.
7. Append every finding to `docs/agents/retro-log.md`: date | finding | class | change | commit.
8. If a finding already appears in `retro-log.md` as prose, it must become a check this time.
9. Keep `CLAUDE.md` at 40 lines or fewer (navigation pointers and the check command only), and `CODING_STANDARDS.md` under about 300 lines: move items to checks or behind pointers when it grows.
10. Unlike step 4 above, apply the changes you are confident about and record them; present to the operator only what needs their decision.
