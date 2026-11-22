#!/usr/bin/env bash
# Run by wheels.yml on a v* tag: both projects must have the tag's version.
set -euo pipefail
tag="${GITHUB_REF_NAME#v}"
for f in pyproject.toml kernel/pyproject.toml; do
    v=$(python3 -c "import tomllib,sys; print(tomllib.load(open(sys.argv[1],'rb'))['project']['version'])" "$f")
    [ "$v" = "$tag" ] || { echo "$f has version $v, tag is $tag"; exit 1; }
done
echo "version $tag in both projects"
