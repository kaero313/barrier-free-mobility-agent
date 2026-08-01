param(
    [ValidateSet("smoke", "release")]
    [string]$Profile = "smoke",
    [double]$PostgresOutageSeconds = 20,
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

$arguments = @(
    "run",
    "--locked",
    "python",
    "scripts/run_replica_release_gate.py",
    "--profile",
    $Profile,
    "--postgres-outage-seconds",
    $PostgresOutageSeconds
)
if ($SkipBuild) {
    $arguments += "--skip-build"
}

Push-Location $projectRoot
try {
    & uv @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Replica release gate failed with exit code $LASTEXITCODE."
    }
} finally {
    Pop-Location
}
