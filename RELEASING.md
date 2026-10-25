# Releasing

How a version of `django-vantage` gets from this repository to PyPI. Written for the
first release, `0.1.0a1`, and kept as the checklist for every one after it.

Publishing uses **PyPI Trusted Publishing** from `.github/workflows/release.yml`. No API
token exists anywhere, and none should be created.

---

## Once, before the first release

1. **Create the GitHub repository** `Pythondeveloper6/django-vantage` (empty: no README,
   licence or `.gitignore`, since the repository already has them).

2. **Push the history.**

   ```bash
   git remote add origin git@github.com:Pythondeveloper6/django-vantage.git
   git push -u origin main
   git push origin release/0.1.0a1 release/arm-publishing
   ```

   Open a pull request from `release/0.1.0a1` into `main`, let CI go green (ruff, the
   3 × 3 Python/Django matrix, the browser job and the packaging job), and merge it.
   Leave `release/arm-publishing` for step 5.

3. **Check the name is still free:** <https://pypi.org/project/django-vantage/> should
   be a 404. If it is taken, stop here. Renaming the distribution touches
   `pyproject.toml`, the README install lines and `release.yml`.

4. **Register the pending publishers.** On <https://pypi.org/manage/account/publishing/>
   and again on <https://test.pypi.org/manage/account/publishing/>, add a pending
   publisher with exactly these values:

   | Field | PyPI | TestPyPI |
   |---|---|---|
   | PyPI project name | `django-vantage` | `django-vantage` |
   | Owner | `Pythondeveloper6` | `Pythondeveloper6` |
   | Repository name | `django-vantage` | `django-vantage` |
   | Workflow name | `release.yml` | `release.yml` |
   | Environment name | `pypi` | `testpypi` |

   Then, in the GitHub repository's **Settings → Environments**, create `pypi` and
   `testpypi`. Add yourself as a required reviewer on `pypi`: every real upload then
   waits for one click.

5. **Arm the workflow.** Merge `release/arm-publishing` into `main`. Its only change is
   uncommenting the TestPyPI, PyPI and GitHub-release jobs in `release.yml`. Don't merge
   it before step 4: a tag pushed against an unregistered publisher fails at the upload,
   and a version number can never be reused on PyPI.

## Every release

1. **Version.** Set `__version__` in `src/vantage/__init__.py`. It is the only place
   the version lives, because hatchling reads it from there. Use PEP 440 spelling:
   `0.1.0a1`, `0.1.0b1`, `0.1.0rc1`, `0.1.0`.

2. **Changelog.** Move the `[Unreleased]` entries under `## [<version>] — <date>`, using
   the day you tag. Update the compare links at the foot of the file.
   `tests/test_release.py` fails if the packaged version has no heading.

3. **Check locally.**

   ```bash
   uv run pytest
   uv run ruff check src tests && uv run ruff format --check src tests
   uvx --with tox-uv tox -e build        # builds, twine-checks, installs and renders the wheel
   ```

4. **Tag and push.**

   ```bash
   git tag -a v0.1.0a1 -m "django-vantage 0.1.0a1"
   git push origin v0.1.0a1
   ```

   `release.yml` refuses a tag that disagrees with `vantage.__version__`, builds,
   verifies the wheel, uploads to TestPyPI, waits for your approval on the `pypi`
   environment, uploads to PyPI with attestations, and creates the GitHub release with
   the distributions attached.

5. **Smoke-test what was published**, from a fresh directory:

   ```bash
   uv venv /tmp/vantage-smoke && source /tmp/vantage-smoke/bin/activate
   uv pip install --pre django-vantage==0.1.0a1
   python -c "import vantage; print(vantage.__version__)"
   ```

   `--pre` is needed for an alpha, beta or rc: pip skips pre-releases otherwise.

6. **Open the next cycle.** Add an empty `## [Unreleased]` section if there isn't one.

## If something goes wrong

- **The upload failed halfway.** Re-run the workflow. TestPyPI uses `skip-existing`, so
  the half that already landed does not block it.
- **A broken version reached PyPI.** Yank it (PyPI → Manage → Release → Options → Yank)
  and release the fix under the next number. A version can't be deleted and re-uploaded.
