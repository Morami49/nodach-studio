# Nodach Pointer — adds the pointer MCP server to Claude Desktop's config (v0.1.1)
# Writes to every config location Claude Desktop may read (normal install and Microsoft Store/MSIX
# install), keeps a timestamped backup of each existing file, and never removes other servers.
$ErrorActionPreference = "Stop"

$cmd = Get-Command python -ErrorAction SilentlyContinue
$python = if ($cmd) { $cmd.Source } else { $null }
if (-not $python -or $python -like "*\WindowsApps\*") {
    Write-Host "Real Python not found on PATH."
    Write-Host "Install it from https://www.python.org/downloads/windows/ (tick 'Add python.exe to PATH'), then run this again."
    exit 1
}
$server = Join-Path $PSScriptRoot "pointer_mcp.py"
if (-not (Test-Path $server)) { Write-Host "pointer_mcp.py not found next to this script."; exit 1 }

$targets = @(Join-Path $env:APPDATA "Claude\claude_desktop_config.json")
$msix = Get-ChildItem (Join-Path $env:LOCALAPPDATA "Packages") -Directory -Filter "Claude_*" -ErrorAction SilentlyContinue
foreach ($m in $msix) { $targets += (Join-Path $m.FullName "LocalCache\Roaming\Claude\claude_desktop_config.json") }

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$utf8 = New-Object System.Text.UTF8Encoding $false   # no BOM: Claude's JSON parser rejects it

foreach ($cfg in $targets) {
    $dir = Split-Path $cfg
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $obj = [pscustomobject]@{}
    if (Test-Path $cfg) {
        Copy-Item $cfg "$cfg.bak-$stamp"
        $raw = [System.IO.File]::ReadAllText($cfg)
        if ($raw.Trim()) {
            try { $obj = $raw | ConvertFrom-Json }
            catch { Write-Host "Skipped $cfg - it isn't valid JSON (backup kept at $cfg.bak-$stamp). Paste it to Claude to fix."; continue }
        }
    }
    if (-not ($obj.PSObject.Properties.Name -contains "mcpServers")) {
        $obj | Add-Member -NotePropertyName "mcpServers" -NotePropertyValue ([pscustomobject]@{})
    }
    $entry = [pscustomobject]@{ command = $python; args = @($server) }
    if ($obj.mcpServers.PSObject.Properties.Name -contains "nodach-pointer") {
        $obj.mcpServers."nodach-pointer" = $entry
    } else {
        $obj.mcpServers | Add-Member -NotePropertyName "nodach-pointer" -NotePropertyValue $entry
    }
    [System.IO.File]::WriteAllText($cfg, ($obj | ConvertTo-Json -Depth 20), $utf8)
    Write-Host "Updated: $cfg"
}
Write-Host ""
Write-Host "Done. Quit Claude completely (right-click its icon by the clock > Quit), then reopen it."
