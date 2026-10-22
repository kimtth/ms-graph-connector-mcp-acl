#Requires -Version 7.0
[CmdletBinding()]
param(
    [Parameter(Mandatory)][guid]$TenantId,
    [Parameter(Mandatory)][uri]$PublicUrl,
    [Parameter(Mandatory)][string]$AliceUpn,
    [Parameter(Mandatory)][string]$BobUpn,
    [Parameter(Mandatory)][string]$EveUpn,
    [ValidatePattern('^[a-zA-Z0-9-]{3,40}$')][string]$Prefix = "helpdesk-demo",
    [switch]$DeviceCode,
    [ValidateRange(1, 15)][int]$SignInTimeoutMinutes = 10
)
$ErrorActionPreference = "Stop"
$envPath = Join-Path (Split-Path $PSScriptRoot -Parent) ".env"
if (Test-Path $envPath) { throw ".env already exists; do not overwrite existing credentials." }
if ($PublicUrl.Scheme -ne "https" -or $PublicUrl.AbsolutePath -ne "/" -or
    $PublicUrl.Query -or $PublicUrl.Fragment) { throw "Use a public HTTPS origin." }
Import-Module Microsoft.Graph.Authentication -ErrorAction Stop
Disconnect-MgGraph -ErrorAction SilentlyContinue | Out-Null
# Process scope keeps tokens in memory only, which avoids failures from a stale on-disk token cache.
$connect = @{TenantId = $TenantId; NoWelcome = $true; ContextScope = "Process"; Scopes = @(
    "Application.ReadWrite.All", "AppRoleAssignment.ReadWrite.All",
    "DelegatedPermissionGrant.ReadWrite.All", "User.Read.All"
)}
# Device code avoids the browser/WAM window, which can open hidden behind other windows.
# Connect-MgGraph stops waiting after 120 seconds, so run the device-code flow here instead.
if ($DeviceCode) {
    $base = "https://login.microsoftonline.com/$TenantId/oauth2/v2.0"
    $graphClient = "14d82eec-204b-4c2f-b7e8-296a70dab67e"  # Microsoft Graph Command Line Tools
    $device = Invoke-RestMethod -Method Post "$base/devicecode" -Body @{
        client_id = $graphClient
        scope = ($connect.Scopes | ForEach-Object { "https://graph.microsoft.com/$_" }) -join " "
    }
    Write-Host $device.message
    # Microsoft limits device codes to 15 minutes.
    $deadline = (Get-Date).AddMinutes([Math]::Min($SignInTimeoutMinutes, $device.expires_in / 60))
    $interval = [Math]::Max([int]$device.interval, 1)
    $token = $null
    while (-not $token) {
        if ((Get-Date) -ge $deadline) { throw "Sign-in not completed within $SignInTimeoutMinutes minutes." }
        Start-Sleep -Seconds $interval
        $reply = Invoke-WebRequest -Method Post "$base/token" -SkipHttpErrorCheck -Body @{
            client_id = $graphClient; device_code = $device.device_code
            grant_type = "urn:ietf:params:oauth:grant-type:device_code"
        }
        $result = $reply.Content | ConvertFrom-Json
        switch ($result.error) {
            $null { $token = $result.access_token }
            "authorization_pending" { }
            "slow_down" { $interval += 5 }
            default { throw "Sign-in failed: $($result.error_description)" }
        }
    }
    Connect-MgGraph -AccessToken (ConvertTo-SecureString $token -AsPlainText -Force) -NoWelcome
} else {
    Connect-MgGraph @connect
}
try {
    $me = Invoke-MgGraphRequest -Method GET -Uri "https://graph.microsoft.com/v1.0/organization?`$select=id"
    if (@($me.value).Count -ne 1 -or $me.value[0].id -ne $TenantId.ToString()) {
        throw "Signed in to a different tenant than -TenantId $TenantId."
    }
} catch {
    throw "Sign-in finished but the first Graph call failed: $($_.Exception.Message). " +
        "Try again without -DeviceCode, or update the module: Update-Module Microsoft.Graph.Authentication."
}
function Graph($Method, $Path, $Body = $null) {
    $requestParams = @{Method = $Method; Uri = "https://graph.microsoft.com/v1.0/$Path"}
    if ($null -ne $Body) {
        $requestParams.Body = $Body | ConvertTo-Json -Depth 20 -Compress
        $requestParams.ContentType = "application/json"
    }
    Invoke-MgGraphRequest @requestParams
}
function Find-One($Type, $Filter) {
    $items = @((Graph GET "$Type`?`$filter=$([uri]::EscapeDataString($Filter))").value)
    if ($items.Count -ne 1) { throw "Expected exactly one $Type for $Filter." }
    $items[0]
}
function New-App($Name, $Extra) {
    $filter = [uri]::EscapeDataString("displayName eq '$Name'")
    if (@((Graph GET "applications`?`$filter=$filter").value).Count) {
        throw "$Name already exists. Inspect existing resources before rerunning."
    }
    $body = @{displayName = $Name; signInAudience = "AzureADMyOrg"}
    foreach ($key in $Extra.Keys) { $body[$key] = $Extra[$key] }
    $app = Graph POST "applications" $body
    Write-Host "Created $Name : $($app.appId), object $($app.id)"
    @{app = $app; sp = (Graph POST "servicePrincipals" @{appId = $app.appId})}
}
function Grant($Client, $Resource, $Scope) {
    Graph POST "oauth2PermissionGrants" @{
        clientId = $Client; resourceId = $Resource; consentType = "AllPrincipals"; scope = $Scope
    } | Out-Null
}
$users = @($AliceUpn, $BobUpn, $EveUpn) | ForEach-Object {
    Find-One "users" "userPrincipalName eq '$($_.Replace("'", "''"))'"
}
if (@($users.id | Select-Object -Unique).Count -ne 3) { throw "Use three distinct existing test users." }
$graph = Find-One "servicePrincipals" "appId eq '00000003-0000-0000-c000-000000000000'"
$read = ($graph.oauth2PermissionScopes | Where-Object value -eq "ExternalItem.Read.All").id
$roles = @("ExternalConnection.ReadWrite.OwnedBy", "ExternalItem.ReadWrite.OwnedBy") |
    ForEach-Object { $name = $_; ($graph.appRoles | Where-Object value -eq $name).id }
if (-not $read -or $roles.Count -ne 2 -or $roles -contains $null) { throw "Required Graph permissions unavailable." }
$scopeId = [guid]::NewGuid().ToString()
$scope = @{
    id = $scopeId; value = "Tickets.Access"; type = "Admin"; isEnabled = $true
    adminConsentDisplayName = "Access demo tickets"
    adminConsentDescription = "Read and update tickets subject to source permissions."
}
$api = New-App "$Prefix-api" @{
    api = @{requestedAccessTokenVersion = 2; oauth2PermissionScopes = @($scope)}
    web = @{redirectUris = @("https://teams.microsoft.com/api/platform/v1.0/oAuthConsentRedirect")}
}
Graph PATCH "applications/$($api.app.id)" @{identifierUris = @("api://$($api.app.appId)")} | Out-Null
$origin = $PublicUrl.GetLeftPart([System.UriPartial]::Authority)
$spa = New-App "$Prefix-browser" @{
    spa = @{redirectUris = @("$origin/", "http://localhost:8000/")}
    requiredResourceAccess = @(
        @{resourceAppId = $api.app.appId; resourceAccess = @(@{id = $scopeId; type = "Scope"})},
        @{resourceAppId = $graph.appId; resourceAccess = @(@{id = $read; type = "Scope"})}
    )
}
Graph PATCH "applications/$($api.app.id)" @{
    api = @{
        requestedAccessTokenVersion = 2; oauth2PermissionScopes = @($scope)
        preAuthorizedApplications = @(
            @{appId = $spa.app.appId; delegatedPermissionIds = @($scopeId)},
            @{appId = "ab3be6b7-f5df-413d-ac2d-abf1e3fd9c0b"; delegatedPermissionIds = @($scopeId)}
        )
    }
} | Out-Null
$indexer = New-App "$Prefix-indexer" @{
    requiredResourceAccess = @(@{
        resourceAppId = $graph.appId
        resourceAccess = @($roles | ForEach-Object { @{id = $_; type = "Role"} })
    })
}
foreach ($role in $roles) {
    Graph POST "servicePrincipals/$($indexer.sp.id)/appRoleAssignments" @{
        principalId = $indexer.sp.id; resourceId = $graph.id; appRoleId = $role
    } | Out-Null
}
Grant $spa.sp.id $api.sp.id "Tickets.Access"
Grant $spa.sp.id $graph.id "ExternalItem.Read.All"
$secret = Graph POST "applications/$($indexer.app.id)/addPassword" @{
    passwordCredential = @{displayName = "30-day demo"; endDateTime = (Get-Date).ToUniversalTime().AddDays(30).ToString("o")}
}
New-Item -ItemType File -Path $envPath | Out-Null
if ($IsWindows) {
    $acl = Get-Acl $envPath
    $acl.SetAccessRuleProtection($true, $false)
    $acl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new(
        [System.Security.Principal.WindowsIdentity]::GetCurrent().Name, "FullControl", "Allow"))
    Set-Acl $envPath $acl
} else {
    & chmod 600 $envPath
    if ($LASTEXITCODE -ne 0) { throw "Could not restrict .env permissions." }
}
$quotedSecret = $secret.secretText.Replace('\', '\\').Replace("'", "\'")
Set-Content -LiteralPath $envPath -Encoding utf8 -Value @(
    "DEMO_TENANT_ID=$TenantId", "DEMO_PUBLIC_URL=$origin",
    "DEMO_API_CLIENT_ID=$($api.app.appId)", "DEMO_SPA_CLIENT_ID=$($spa.app.appId)",
    "DEMO_INGEST_CLIENT_ID=$($indexer.app.appId)", "DEMO_INGEST_CLIENT_SECRET='$quotedSecret'",
    "DEMO_ALICE_OID=$($users[0].id)", "DEMO_BOB_OID=$($users[1].id)", "DEMO_EVE_OID=$($users[2].id)"
)
Write-Host "Private .env written. The indexer secret expires in 30 days."
Write-Host "Register API client $($api.app.appId) for SSO; scope api://$($api.app.appId)/Tickets.Access"
Write-Host "MCP endpoint: $origin/mcp/ . Finish the portal steps in README.md."
