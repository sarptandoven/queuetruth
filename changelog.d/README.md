# Changelog fragments

A pull request that users will notice adds one file here instead of editing `CHANGELOG.md`, so parallel pull requests
never conflict on the changelog. At release, `python tools/changelog.py release X.Y.Z` folds every fragment into
`CHANGELOG.md` under `## [X.Y.Z] - DATE` and deletes them.

Name: `<slug>.<section>.md`, where `<slug>` is lowercase letters, digits and hyphens (your branch or topic, for
example `compare-watch`) and `<section>` is one of `added`, `changed`, `deprecated`, `removed`, `fixed`, `security`.
A change that belongs in two sections gets two files.

Content: one or more Markdown list items, written as the entries in `CHANGELOG.md` are (plain sentences, no em or en
dashes; continuation lines indented by two spaces; no blank lines and no headings):

```markdown
- **`queuetruth compare --watch`.** Re-runs the comparison when new live records arrive and prints one line per run
  with the drift since the baseline window.
```

`python tools/changelog.py check` validates the files (`make lint` runs it) and `python tools/changelog.py show`
prints the Unreleased section as it will read.
