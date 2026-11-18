# Provision Langfuse org/project/API keys on local Langfuse (localhost:3100)
# for the General Agent Platform backend. Usage: powershell -File provision_langfuse_keys.ps1
param(
    [string]$BaseUrl = "http://localhost:3100",
    [string]$Email = "admin@general-agent-platform.local",
    [string]$Password = "AgPlat-Langfuse-2026!",
    [string]$ProjectName = "general-agent-platform"
)

$ErrorActionPreference = "Stop"
$session = New-Object Microsoft.PowerShell.Commands.WebRequestSession

# 1) Sign in (NextAuth credentials flow)
$csrf = (Invoke-WebRequest -Uri "$BaseUrl/api/auth/csrf" -WebSession $session -UseBasicParsing).Content | ConvertFrom-Json
$loginBody = "csrfToken=$([uri]::EscapeDataString($csrf.csrfToken))&email=$([uri]::EscapeDataString($Email))&password=$([uri]::EscapeDataString($Password))&callbackUrl=$([uri]::EscapeDataString("$BaseUrl/"))"
$loginResp = Invoke-WebRequest -Uri "$BaseUrl/api/auth/callback/credentials" -Method POST -ContentType "application/x-www-form-urlencoded" `
    -Body $loginBody -WebSession $session -UseBasicParsing -MaximumRedirection 0 -ErrorAction SilentlyContinue
$sess = (Invoke-WebRequest -Uri "$BaseUrl/api/auth/session" -WebSession $session -UseBasicParsing).Content | ConvertFrom-Json
if (-not $sess.user) { throw "Langfuse sign-in failed (no session). login response: $loginResp.StatusCode" }
Write-Host "signed in as: $($sess.user.email)"

# 2) tRPC helper
function Invoke-Trpc {
    param([string]$Proc, [string]$Json)
    $uri = $BaseUrl + "/api/trpc/" + $Proc + "?batch=1"
    $body = '{"0":{"json":' + $Json + '}}'
    $resp = Invoke-WebRequest -Uri $uri -Method POST -ContentType "application/json" `
        -Body $body -WebSession $session -UseBasicParsing
    $data = $resp.Content | ConvertFrom-Json
    if ($data[0].error) { throw ("tRPC " + $Proc + " failed: " + $data[0].error.message) }
    return $data[0].result.data.json
}

# 3) Organization
$org = Invoke-Trpc -Proc "organizations.create" -Json ('{"name":"' + $ProjectName + '"}')
$orgId = $org.id
Write-Host ("org: " + $orgId)

# 4) Project
$project = Invoke-Trpc -Proc "projects.create" -Json ('{"name":"' + $ProjectName + '","orgId":"' + $orgId + '"}')
$projectId = $project.id
Write-Host ("project: " + $projectId)

# 5) API keys (secret shown only once, at creation)
$key = Invoke-Trpc -Proc "projectApiKeys.create" -Json ('{"projectId":"' + $projectId + '","note":"general-agent-platform backend"}')
Write-Host ("PUBLIC_KEY=" + $key.publicKey)
Write-Host ("SECRET_KEY=" + $key.secretKey)
Write-Host "PROVISION_DONE"
