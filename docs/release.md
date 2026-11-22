# Releasing to PyPI

Two projects are published together, with the same version:

| Project | Built by | Files |
|---|---|---|
| `cocotbext-sva` | `python -m build` | pure-Python wheel, sdist |
| `cocotbext-sva-kernel` | maturin (`kernel/`) | abi3 wheels (Linux x86_64/aarch64, Windows x64, macOS x86_64/arm64), sdist |

Uploads use PyPI *trusted publishing*: PyPI trusts the GitHub workflow
`wheels.yml` in `dpavlo55/cocotbext-sva`, in a given GitHub environment. No
token is stored anywhere. Each project has its own environment, because PyPI
accepts only one pending publisher per configuration (owner, repository,
workflow, environment): with a shared environment, the second project is
refused ("A pending trusted publisher matching this configuration has
already been registered for a different project name").

## One-time setup

1. **Accounts.** An account on https://pypi.org and one on
   https://test.pypi.org (separate sites), each with two-factor
   authentication.
2. **Pending publishers.** The projects do not exist yet, so add a *pending*
   publisher for each, on each site: Account → Publishing → "Add a new
   pending publisher" → GitHub. Owner `dpavlo55`, repository name
   `cocotbext-sva` and workflow name `wheels.yml` for all four; the project
   name and environment name are:

   | Site | PyPI project name | Environment name |
   |---|---|---|
   | pypi.org | `cocotbext-sva` | `pypi` |
   | pypi.org | `cocotbext-sva-kernel` | `pypi-kernel` |
   | test.pypi.org | `cocotbext-sva` | `testpypi` |
   | test.pypi.org | `cocotbext-sva-kernel` | `testpypi-kernel` |

   The first upload creates each project and turns its pending publisher
   into a normal one.
3. **GitHub environments.** Repository → Settings → Environments: create
   `pypi`, `pypi-kernel`, `testpypi` and `testpypi-kernel`. For `pypi` and
   `pypi-kernel`, add yourself as a required reviewer: every upload to PyPI
   then waits for your approval in the Actions tab (once per project).

## Trial run on TestPyPI

Actions → Wheels → "Run workflow" on `main`, with "Upload the built
distributions to TestPyPI" ticked. The workflow builds everything, tests the
native wheels, then uploads to TestPyPI. Check it:

```bash
python -m venv /tmp/try && . /tmp/try/bin/activate
pip install --index-url https://test.pypi.org/simple/ \
    --extra-index-url https://pypi.org/simple/ "cocotbext-sva[fast]"
python -c "import cocotbext.sva, cocotbext_sva_kernel"
```

(`--extra-index-url` fetches cocotb from PyPI.) TestPyPI refuses a version
it already has: change the version (e.g. `0.1.0.dev1`) to try again.

## Release

1. Set the same version in `pyproject.toml` and `kernel/pyproject.toml`
   (`0.1.0`, `0.1.1`, ...), and in `kernel/Cargo.toml`. Commit.
2. Tag and push:

   ```bash
   git tag v0.1.0
   git push origin v0.1.0
   ```
3. The Wheels workflow runs on the tag. Its publish jobs check that both
   versions equal the tag (`.github/check-version.sh`), wait for approval,
   and upload to PyPI: first `cocotbext-sva-kernel` (environment
   `pypi-kernel`), then `cocotbext-sva` (environment `pypi`), so a failed
   kernel upload never leaves a release whose `[fast]` extra is missing.

A version can never be uploaded twice to PyPI, even after deleting it: fix
problems with a new version.
