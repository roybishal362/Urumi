<#
.SYNOPSIS
  Bring the whole system up on a local k3d cluster (Windows PowerShell).

.DESCRIPTION
  Creates the cluster if needed, builds the four images, loads them into the
  cluster, and installs the two Helm releases with the local values files.
  Safe to run again: every step is an upgrade-or-skip.

  The chat agent needs an LLM API key. Put GROQ_API_KEY=... in a .env file at
  the repository root (it is git-ignored) or set it in your shell before
  running this. Without it everything else still works.

.EXAMPLE
  .\scripts\local-up.ps1
  .\scripts\local-up.ps1 -SkipBuild      # reuse the images already built
#>
param(
    [string]$Cluster = "urumi",
    [string]$Tag = "0.1.0",
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }

function Invoke-Tool {
    # Run a command line tool and stop the script if it fails.
    & $args[0] $args[1..($args.Length - 1)]
    if ($LASTEXITCODE -ne 0) { throw "Command failed: $($args -join ' ')" }
}

foreach ($tool in "docker", "kubectl", "helm", "k3d") {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "$tool is not on PATH. See README.md, 'Prerequisites'."
    }
}

# --- LLM key: from the environment, or from .env ---
$llmKey = $env:GROQ_API_KEY
if (-not $llmKey) { $llmKey = $env:LLM_API_KEY }
if (-not $llmKey -and (Test-Path "$root\.env")) {
    foreach ($line in Get-Content "$root\.env") {
        if ($line -match '^\s*(GROQ_API_KEY|LLM_API_KEY)\s*=\s*"?([^"#]+?)"?\s*$') { $llmKey = $Matches[2].Trim() }
    }
}

# --- cluster ---
Step "Cluster"
$exists = (k3d cluster list -o json | ConvertFrom-Json) | Where-Object { $_.name -eq $Cluster }
if (-not $exists) {
    Invoke-Tool k3d cluster create --config deploy/k3d-cluster.yaml
} else {
    Write-Host "Cluster '$Cluster' already exists."
}
Invoke-Tool kubectl config use-context "k3d-$Cluster"

# --- images ---
$images = [ordered]@{
    "urumi/woo-mcp"   = "services/woo-mcp"
    "urumi/gateway"   = "services/gateway"
    "urumi/demo-mcp"  = "services/demo-mcp"
    "urumi/dashboard" = "dashboard"
}
if (-not $SkipBuild) {
    Step "Build images"
    foreach ($name in $images.Keys) {
        Write-Host "building ${name}:$Tag"
        Invoke-Tool docker build -q -t "${name}:$Tag" $images[$name]
    }
}
Step "Load images into the cluster"
$refs = $images.Keys | ForEach-Object { "${_}:$Tag" }
Invoke-Tool k3d image import @refs -c $Cluster

# --- store ---
Step "Install the store (WordPress + WooCommerce + MCP server)"
Invoke-Tool helm upgrade --install store charts/store --namespace store --create-namespace -f charts/store/values-local.yaml
Write-Host "Waiting for the store. The first start installs WordPress and WooCommerce and takes a few minutes."
Invoke-Tool kubectl -n store rollout status statefulset/store-mariadb --timeout=300s
Invoke-Tool kubectl -n store rollout status deployment/store-wordpress --timeout=900s
Invoke-Tool kubectl -n store rollout status deployment/store-woo-mcp --timeout=180s

# --- gateway ---
# Installed after the store on purpose: the chart reads the MCP server's token
# from the store's Secret so the gateway can register it automatically.
Step "Install the gateway (backend + dashboard + database + demo MCP servers)"
$helmArgs = @("upgrade", "--install", "gateway", "charts/gateway", "--namespace", "gateway", "--create-namespace", "-f", "charts/gateway/values-local.yaml")
if ($llmKey) { $helmArgs += @("--set", "llm.apiKey=$llmKey") }
Invoke-Tool helm @helmArgs
Invoke-Tool kubectl -n gateway rollout status statefulset/gateway-postgres --timeout=300s
Invoke-Tool kubectl -n gateway rollout status deployment/gateway --timeout=300s
Invoke-Tool kubectl -n gateway rollout status deployment/gateway-dashboard --timeout=180s

if (-not $SkipBuild) {
    # Image tags do not change between local builds, so restart the pods that
    # run locally built images to pick up what was just loaded.
    Step "Restart workloads to pick up rebuilt images"
    Invoke-Tool kubectl -n store rollout restart deployment/store-woo-mcp
    Invoke-Tool kubectl -n gateway rollout restart deployment/gateway deployment/gateway-dashboard deployment/gateway-fx deployment/gateway-shipping
    Invoke-Tool kubectl -n store rollout status deployment/store-woo-mcp --timeout=180s
    Invoke-Tool kubectl -n gateway rollout status deployment/gateway --timeout=300s
    Invoke-Tool kubectl -n gateway rollout status deployment/gateway-dashboard --timeout=180s
}

function Read-Secret($namespace, $name, $key) {
    $encoded = kubectl -n $namespace get secret $name -o "jsonpath={.data.$key}"
    return [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($encoded))
}

Step "Ready"
Write-Host "Storefront      http://store.localtest.me"
Write-Host "Store admin     http://store.localtest.me/wp-admin    admin / $(Read-Secret store store-wp-admin password)"
Write-Host "Dashboard       http://gateway.localtest.me           admin@example.com / $(Read-Secret gateway gateway-app admin-password)"
Write-Host "MCP endpoint    http://gateway.localtest.me/mcp"
if (-not $llmKey) {
    Write-Host "`nNo LLM API key was found, so the chat agent is off. Add GROQ_API_KEY=... to .env and run this script again." -ForegroundColor Yellow
}
