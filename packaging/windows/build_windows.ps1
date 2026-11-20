# Build Vault-<version>-windows-setup.exe: Vault with its own Python and ffmpeg, nothing to install.
#   pwsh build_windows.ps1 -Release <release dir> -Out <out dir>
# Needs Inno Setup 6 (ISCC.exe). Runs on GitHub's Windows machines (.github/workflows/installers.yml).
param([Parameter(Mandatory)][string]$Release, [Parameter(Mandatory)][string]$Out)
$ErrorActionPreference = "Stop"
$Here = $PSScriptRoot
$Version = (Get-Content (Join-Path $Release "VERSION") -Raw).Trim()
New-Item -ItemType Directory -Force -Path $Out | Out-Null
$Dl = Join-Path $Out "_downloads"; New-Item -ItemType Directory -Force -Path $Dl | Out-Null

# Pinned inputs with checksums: standalone CPython and an LGPL ffmpeg build (BtbN/FFmpeg-Builds).
$Inputs = @(
  @{ Url = "https://github.com/astral-sh/python-build-standalone/releases/download/20261110/cpython-3.12.16%2B20261110-x86_64-pc-windows-msvc-install_only_stripped.tar.gz";
     File = "win-python.tar.gz"; Sha = "355ee61b8207b91574491b1c6dc9b0ebb4408fc56bfafe498f3ef439314b52dc" },
  @{ Url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-11-20-12-40/ffmpeg-n8.1.3-39-g660b5aa7db-win64-lgpl-8.1.zip";
     File = "win-ffmpeg.zip"; Sha = "a16d63673d8b28ec0b4fb7311677fd8f6fa9f221f40bf00b48c01ce8e9122be8" }
)
foreach ($i in $Inputs) {
  $f = Join-Path $Dl $i.File
  if (-not (Test-Path $f)) { Invoke-WebRequest -Uri $i.Url -OutFile $f -UseBasicParsing }
  $h = (Get-FileHash $f -Algorithm SHA256).Hash.ToLower()
  if ($h -ne $i.Sha) { Remove-Item $f; throw "checksum mismatch: $($i.File) ($h)" }
}

$Stage = Join-Path $Out "stage"
if (Test-Path $Stage) { Remove-Item -Recurse -Force $Stage }
New-Item -ItemType Directory -Force -Path (Join-Path $Stage "app"), (Join-Path $Stage "bin") | Out-Null

Copy-Item -Recurse -Force (Join-Path $Release "*") (Join-Path $Stage "app")
foreach ($x in ".git", ".github", "config.json", "cache", "tests", "start.command", "make_app.sh", "packaging", "Vault.icns") {
  $p = Join-Path (Join-Path $Stage "app") $x
  if (Test-Path $p) { Remove-Item -Recurse -Force $p }
}

tar -xzf (Join-Path $Dl "win-python.tar.gz") -C $Stage          # -> python\
foreach ($x in "Lib\test", "Lib\idlelib", "Lib\turtledemo") {
  $p = Join-Path (Join-Path $Stage "python") $x
  if (Test-Path $p) { Remove-Item -Recurse -Force $p }
}

$Ff = Join-Path $Out "ffmpeg-unpacked"
if (Test-Path $Ff) { Remove-Item -Recurse -Force $Ff }
Expand-Archive -Path (Join-Path $Dl "win-ffmpeg.zip") -DestinationPath $Ff
$FfBin = Get-ChildItem -Path $Ff -Recurse -Filter "ffmpeg.exe" | Select-Object -First 1
Copy-Item $FfBin.FullName (Join-Path $Stage "bin")
Copy-Item (Join-Path $FfBin.DirectoryName "ffprobe.exe") (Join-Path $Stage "bin")
Get-ChildItem -Path $FfBin.DirectoryName -Filter "*.dll" | Copy-Item -Destination (Join-Path $Stage "bin")
$FfLicense = Get-ChildItem -Path $Ff -Recurse -Filter "LICENSE*" | Select-Object -First 1
if ($FfLicense) { Copy-Item $FfLicense.FullName (Join-Path $Stage "bin\FFMPEG-LICENSE.txt") }

Copy-Item (Join-Path $Here "launcher.pyw") $Stage
Copy-Item (Join-Path $Here "Vault.ico") $Stage
Copy-Item (Join-Path $Here "THIRD-PARTY.txt") $Stage

$Iscc = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source
if (-not $Iscc) { $Iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" }
& $Iscc /Q "/DVersion=$Version" "/DStage=$Stage" "/DOutDir=$Out" (Join-Path $Here "vault.iss")
if ($LASTEXITCODE -ne 0) { throw "ISCC failed" }
Get-Item (Join-Path $Out "Vault-$Version-windows-setup.exe") | Select-Object Name, Length
