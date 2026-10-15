# Traditional setup (the baseline)

```bash
git clone https://github.com/JashithaDesu/selection-and-mutation-testing
cd selection-and-mutation-testing
python3 -m testchain all --jobs 4
```

That is the whole setup, and it is exactly why this project is a good baseline: the
README says "nothing to install".

Implicit assumptions, none of them checked or recorded:
- `python3` exists and is new enough (the code needs at least 3.9 for `ast.unparse`)
- whichever version it is produces the same mutants as the version the published
  results were made with (see the Python-version matrix in `environments.yml`)
- on Windows, `py` instead of `python3`
- matplotlib is optional; if a different version is installed, the chart may look different
