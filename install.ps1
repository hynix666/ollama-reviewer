# Link this repo's commands/ into ~/.claude/commands/ollama.
#
# Uses a directory junction rather than a symlink: on Windows, symlink creation
# needs Developer Mode or an elevated shell, whereas junctions work unprivileged.
# A junction points at a path, so it survives git checkout and pull - unlike a
# hardlink, which git silently breaks by replacing the file.

$ErrorActionPreference = "Stop"

$repo   = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path $repo "commands"
$link   = Join-Path $env:USERPROFILE ".claude\commands\ollama"

if (-not (Test-Path $target)) {
    Write-Error "commands/ not found in $repo - is this the right directory?"
    exit 1
}

$parent = Split-Path -Parent $link
if (-not (Test-Path $parent)) {
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
}

if (Test-Path $link) {
    $existing = Get-Item $link -Force
    if ($existing.LinkType -eq "Junction") {
        Write-Output "Replacing existing junction at $link"
        Remove-Item -Recurse -Force $link
    } else {
        Write-Error "$link already exists and is not a junction. Move it aside first."
        exit 1
    }
}

cmd /c mklink /J "$link" "$target" | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Error "Failed to create the junction."
    exit 1
}

$count = (Get-ChildItem $link -Filter *.md).Count
Write-Output "Linked $link -> $target ($count commands)"
Write-Output "Available as: /ollama:review  /ollama:review-file  /ollama:adversarial  /ollama:status"

# Verify the reviewer itself is functional.
# Relax the error preference first: under "Stop", any stderr from a native
# executable surfaces as a NativeCommandError, which would misreport a passing
# selftest as an installer failure. Check the exit code explicitly instead.
$ErrorActionPreference = "Continue"
$selftest = Join-Path $repo "scripts\selftest.py"
Write-Output ""
Write-Output "Running selftest..."
python $selftest
if ($LASTEXITCODE -ne 0) {
    Write-Output ""
    Write-Output "Selftest reported failures (exit $LASTEXITCODE). The commands are"
    Write-Output "linked, but check that Ollama is running: ollama serve"
    exit $LASTEXITCODE
}
