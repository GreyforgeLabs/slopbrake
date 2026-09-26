
## Our additions (Slopbrake)

- **Record the seams, don't block on them.** When the operator isn't in the loop (for example inside `implement-ticket`), write the seams under test into the ticket under `## Seams` instead of waiting for confirmation; the reviewer checks tests against that list.
- **Keep the first red run.** Save the output of each slice's first failing run. The PR's Evidence section quotes red then green; for a bug fix, the regression test must be shown failing on the unfixed code.
- **The gate backs these rules up.** `scripts/check` fails on the mechanical tautology patterns and on a mutation score below the floor on changed lines. Surviving mutants mean a test can't fail: strengthen the assertion rather than adding more tests of the same shape.
