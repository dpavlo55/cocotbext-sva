# Run the nox task runner inside WSL on the current Windows working tree.
#   .\scripts\wsl-test.ps1 -s smoke
#   .\scripts\wsl-test.ps1 -s smoke -- ghdl
$root = (Resolve-Path "$PSScriptRoot\..").Path
$src = (wsl -e wslpath -a $root).Trim()
wsl -e bash "$src/scripts/wsl-test.sh" $src @args
exit $LASTEXITCODE
