## What and why

<!-- What does this change, and why? Link the issue it addresses (for example "Closes #12"). -->

## How it was tested

<!-- Commands you ran (for example `make lint test`, `make test-e2e`, `make check-docs`) and the tests you added.
     A new behaviour or a fixed bug comes with a test that would fail without it. -->

## Checklist

- [ ] Every commit is signed off (`git commit -s`, see CONTRIBUTING.md).
- [ ] `make lint` and the relevant tests pass locally.
- [ ] No real market data, orders, fills or prices are added to the repository, tests or docs.
- [ ] Generated files are regenerated, not hand-edited (`make schemas`, `make openapi`, `docs/gen_reference.py`).
- [ ] Public names (CLI flags, Python API, schemas, HTTP endpoints) are unchanged, or the change is described above
      with its migration path.
- [ ] Docs and `CHANGELOG.md` are updated if users will notice the change.
- [ ] No dependency was added or upgraded (or it was agreed in an issue first).
