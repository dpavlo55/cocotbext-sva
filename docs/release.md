# Releasing to PyPI

Two projects are published together, with the same version:

| Project | Built by | Files |
|---|---|---|
| `cocotbext-sva` | `python -m build` | pure-Python wheel, sdist |
| `cocotbext-sva-kernel` | maturin (`kernel/`) | abi3 wheels (Linux x86_64/aarch64, Windows x64, macOS x86_64/arm64), sdist |

Uploads use PyPI *trusted publishing*: PyPI trusts the GitHub workflow
`wheels.yml` in `dpavlo55/cocotbext-sva`, in a given GitHub environment. No
token is stored anywhere.

## One-time setup

1. **Accounts.** An account on https://pypi.org and one on
   https://test.pypi.org (separate sites), each with two-factor
   authentication.
2. **Pending publishers.** The projects do not exist yet, so add a *pending*
   publisher for each, on each site: Account → Publishing → "Add a new
   pending publisher" → GitHub, with

   | Field | PyPI | TestPyPI |
   |---|---|---|
   | PyPI project name | `cocotbext-sva`, then again `cocotbext-sva-kernel` | the same two |
   | Owner | `dpavlo55` | `dpavlo55` |
   | Repository name | `cocotbext-sva` | `cocotbext-sva` |
   | Workflow name | `wheels.yml` | `wheels.yml` |
   | Environment name | `pypi` | `testpypi` |

   The first upload creates each project and turns its pending publisher
   into a normal one.
3. **GitHub environments.** Repository → Settings → Environments: create
   `pypi` and `testpypi`. For `pypi`, add yourself as a required reviewer:
   every upload to PyPI then waits for your approval in the Actions tab.

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
3. The Wheels workflow runs on the tag. Its `publish` job checks that both
   versions equal the tag, waits for approval (environment `pypi`), and
   uploads all files to PyPI.

A version can never be uploaded twice to PyPI, even after deleting it: fix
problems with a new version.
