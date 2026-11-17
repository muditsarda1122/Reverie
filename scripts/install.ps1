# Reverie: one-command install for Windows (PowerShell).
#
#   irm https://raw.githubusercontent.com/muditsarda1122/Reverie/main/scripts/install.ps1 | iex
#
# Same steps as install.sh: use uv (installed for your user if missing), install Reverie as an
# isolated tool, then run `reverie install`. You then need one LLM API key in your environment.
# Overrides: $env:REVERIE_SRC (what to install), $env:REVERIE_REF (git ref when installing from GitHub).
$ErrorActionPreference = "Stop"
$ref = if ($env:REVERIE_REF) { $env:REVERIE_REF } else { "main" }
$gitSrc = "git+https://github.com/muditsarda1122/Reverie.git@$ref"
$src = $env:REVERIE_SRC   # empty: PyPI package "reverie-mem" first, the GitHub branch as the fallback

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Write-Host "Installing uv (a fast Python installer) for your user..."
  Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
  # uv's installer puts uv in the account's real profile and edits the saved PATH, not this session's.
  $real = [Environment]::GetFolderPath("UserProfile")
  $saved = [Environment]::GetEnvironmentVariable("Path", "User")
  $env:Path = "$real\.local\bin;$env:USERPROFILE\.local\bin;$env:USERPROFILE\.cargo\bin;$saved;$env:Path"
  if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw "uv was installed but is not on PATH; open a new terminal and run this again" }
}
Write-Host "Installing Reverie..."
if ($src) {
  uv tool install --quiet --python 3.12 --force $src
  if ($LASTEXITCODE -ne 0) { throw "uv could not install Reverie from $src" }
} else {
  uv tool install --quiet --python 3.12 --force reverie-mem 2>$null
  if ($LASTEXITCODE -ne 0) {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw "reverie-mem is not on PyPI yet and git is missing; install git, then run this again" }
    uv tool install --quiet --python 3.12 --force $gitSrc
    if ($LASTEXITCODE -ne 0) { throw "uv could not install Reverie from $gitSrc" }
  }
}

$bin = (uv tool dir --bin).Trim()
$exe = Join-Path $bin "reverie.exe"
if (-not (Test-Path $exe)) { throw "Reverie installed but reverie.exe was not found in $bin" }
& $exe install --all
if ($env:Path -notlike "*$bin*") { Write-Host ""; Write-Host "Add $bin to your PATH so 'reverie' works in new terminals." }
