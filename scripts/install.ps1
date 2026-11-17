# Reverie: one-command install for Windows (PowerShell).
#
#   irm https://raw.githubusercontent.com/muditsarda1122/Reverie/main/scripts/install.ps1 | iex
#
# Same steps as install.sh: use uv (installed for your user if missing), install Reverie as an
# isolated tool, then run `reverie install`. You then need one LLM API key in your environment.
# Overrides: $env:REVERIE_SRC (what to install), $env:REVERIE_REF (git ref when installing from GitHub).
$ErrorActionPreference = "Stop"
$ref = if ($env:REVERIE_REF) { $env:REVERIE_REF } else { "main" }
$src = if ($env:REVERIE_SRC) { $env:REVERIE_SRC } else { "git+https://github.com/muditsarda1122/Reverie.git@$ref" }

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Write-Host "Installing uv (a fast Python installer) for your user..."
  Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
  # uv's installer puts uv in the account's real profile and edits the saved PATH, not this session's.
  $real = [Environment]::GetFolderPath("UserProfile")
  $saved = [Environment]::GetEnvironmentVariable("Path", "User")
  $env:Path = "$real\.local\bin;$env:USERPROFILE\.local\bin;$env:USERPROFILE\.cargo\bin;$saved;$env:Path"
  if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw "uv was installed but is not on PATH; open a new terminal and run this again" }
}
if ($src -like "git+*" -and -not (Get-Command git -ErrorAction SilentlyContinue)) { throw "git is required to install from GitHub" }

Write-Host "Installing Reverie..."
uv tool install --quiet --python 3.12 --force $src
if ($LASTEXITCODE -ne 0) { throw "uv could not install Reverie from $src" }

$bin = (uv tool dir --bin).Trim()
$exe = Join-Path $bin "reverie.exe"
if (-not (Test-Path $exe)) { throw "Reverie installed but reverie.exe was not found in $bin" }
& $exe install --all
if ($env:Path -notlike "*$bin*") { Write-Host ""; Write-Host "Add $bin to your PATH so 'reverie' works in new terminals." }
