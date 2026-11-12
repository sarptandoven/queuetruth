- **Changelog fragments.** A pull request adds its changelog entry as one file, `changelog.d/<slug>.<section>.md`,
  instead of editing `CHANGELOG.md`, so parallel pull requests no longer conflict on the changelog.
  `python tools/changelog.py check` (in `make lint` and CI) validates the fragments, `show` previews the Unreleased
  section and `release X.Y.Z` folds them into `CHANGELOG.md` at release. Entries already under Unreleased stay.
