## Summary

What changed and why?

## Verification

- [ ] `python -m unittest discover -s tests -q`
- [ ] `ruff check --select E9,F63,F7,F82,F601 nalar tests`
- [ ] `scripts/release-check.sh` when packaging changed
- [ ] Added a regression test that fails without this change
- [ ] Secrets/keys are not included in code, tests, logs or fixtures
- [ ] No auto-apply, auto-commit or unapproved shell execution was added
- [ ] Documentation updated when behavior changed

## Notes

Anything reviewers should know: trade-offs, compatibility, follow-ups.
