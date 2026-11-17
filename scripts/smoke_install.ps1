# Install smoke test (Windows): the stranger's path from a source tree, in a throwaway home.
param([string]$Src = (Resolve-Path "$PSScriptRoot\.."))
$ErrorActionPreference = "Stop"
$home_ = Join-Path ([IO.Path]::GetTempPath()) ("reverie-smoke-" + [guid]::NewGuid())
New-Item -ItemType Directory -Force "$home_\bin" | Out-Null
$env:USERPROFILE = $home_; $env:HOME = $home_
foreach ($v in "ANTHROPIC_API_KEY","OPENAI_API_KEY","GEMINI_API_KEY","OPENROUTER_API_KEY","OPENCODE_ZEN_API_KEY") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
foreach ($a in "claude","codex") { Set-Content "$home_\bin\$a.cmd" "@echo 2.1.130 (stub)" }
$env:Path = "$home_\bin;$home_\.local\bin;$env:Path"
function Fail($m) { Write-Host "SMOKE FAIL: $m"; exit 1 }

$env:REVERIE_SRC = $Src
& "$Src\scripts\install.ps1"; if ($LASTEXITCODE -ne 0) { Fail "install.ps1" }
$v = reverie --version; if ($v -notmatch '^reverie \d') { Fail "--version ($v)" }
New-Item -ItemType Directory "$home_\proj" | Out-Null; Set-Location "$home_\proj"
git init -q .; git -c user.email=a@b -c user.name=a commit -q --allow-empty -m init
$st = (reverie status 2>&1 | Out-String); if ($st -notmatch "No LLM key found") { Fail "status did not warn about the missing key: $st" }
reverie digest | Out-Null; if ($LASTEXITCODE -ne 0) { Fail "digest" }
reverie uninstall --yes | Out-Null
Write-Host "SMOKE OK: $v"
