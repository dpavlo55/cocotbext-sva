#!/usr/bin/env bash
# Runs inside WSL. Mirrors the Windows working tree into the Linux filesystem
# (fast builds, real exec bits), keeps a Linux venv there, then runs nox.
#   usage: wsl-test.sh <windows-tree-as-/mnt-path> [nox args...]
set -euo pipefail
src="${1:?source tree}"; shift
dst="$HOME/work/cocotbext-sva"
oss="$HOME/tools/oss-cad-suite"
shims="$HOME/tools/shims"
# OSS CAD Suite's vvp wrapper forces its bundled Python (PYTHONHOME), which breaks
# cocotb in our venv. The shim runs the same binary without those overrides.
mkdir -p "$shims"
cat > "$shims/vvp" <<SHIM
#!/usr/bin/env bash
exec "$oss/lib/ld-linux-x86-64.so.2" --inhibit-cache --inhibit-rpath "" --library-path "$oss/lib" "$oss/libexec/vvp" "\$@"
SHIM
chmod +x "$shims/vvp"
# appended: system python stays first; shims win over the suite's wrappers
export PATH="$PATH:$shims:$oss/bin"

mkdir -p "$dst"
rsync -a --delete --exclude '.git/' --exclude '.venv/' --exclude 'sim_build/' \
      --exclude '__pycache__/' --exclude 'local/' --exclude 'target/' "$src/" "$dst/"
cd "$dst"

if [ ! -x .venv/bin/python ]; then python3 -m venv .venv; fi
# Reinstall when dependencies or the package layout change (editable installs
# record which directories are packages).
stamp=.venv/.requirements.sha
want=$({ cat requirements.txt pyproject.toml; find src -name '__init__.py' | sort; } | sha256sum)
if [ "$(cat "$stamp" 2>/dev/null)" != "$want" ]; then
    .venv/bin/pip install -q --upgrade pip
    .venv/bin/pip install -q -r requirements.txt
    echo "$want" > "$stamp"
fi
exec .venv/bin/nox "$@"
