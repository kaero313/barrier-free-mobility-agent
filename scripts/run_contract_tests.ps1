param(
    [string]$McpProject = "C:\project\barrier_free_mobility_mcp",
    [int]$Port = 18080,
    [int]$AgentPort = 18081
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

Push-Location $projectRoot
try {
    uv run --locked python scripts/run_contract_tests.py `
        --mcp-project $McpProject `
        --mcp-port $Port `
        --agent-port $AgentPort
    if ($LASTEXITCODE -ne 0) {
        throw "Contract/E2E verification failed with exit code $LASTEXITCODE."
    }
} finally {
    Pop-Location
}
