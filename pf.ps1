# Windows entry point for the project CLI. Without arguments, show the active project.
$pfArgs = $args
if ($pfArgs.Count -eq 0) {
    $pfArgs = @('status', '--brief')
}

$paths = @(Join-Path $PSScriptRoot '.venv\Scripts\python.exe')
if ($env:VIRTUAL_ENV) {
    $paths = @(Join-Path $env:VIRTUAL_ENV 'Scripts\python.exe') + $paths
}
$paths += (Get-Command python.exe, python3.exe -CommandType Application -All -ErrorAction SilentlyContinue).Source
if ($env:USERPROFILE) {
    $paths += Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
}

$interpreters = foreach ($path in ($paths | Select-Object -Unique)) {
    if ($path -and $path -notlike '*\Microsoft\WindowsApps\*' -and (Test-Path -LiteralPath $path -PathType Leaf)) {
        @{ Path = $path; Flags = @() }
    }
}
$launcher = Get-Command py.exe -CommandType Application -ErrorAction SilentlyContinue
if ($launcher) {
    $interpreters = @($interpreters) + @{ Path = $launcher.Source; Flags = @('-3') }
}

$script = Join-Path $PSScriptRoot 'tools\pf.py'
foreach ($interpreter in $interpreters) {
    if (-not $interpreter) { continue }
    $python = $interpreter.Path
    $flags = $interpreter.Flags
    try {
        & $python @flags -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>$null
        if ($LASTEXITCODE -ne 0) { continue }
    } catch {
        continue
    }
    & $python @flags $script @pfArgs
    exit $LASTEXITCODE
}

[Console]::Error.WriteLine('Python 3.10+ was not found. Install Python or open this folder in Codex with the bundled Python runtime.')
exit 1
