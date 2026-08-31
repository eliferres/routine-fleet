# nightly-link-sweep

Walk every Markdown file in `docs/` and check that each link resolves. Append
one line per broken link to `out/link-report.md`: the file, the line number,
and the target that failed.

Fix nothing. This routine reports; a human decides.
