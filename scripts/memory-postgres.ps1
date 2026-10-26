param(
    [Parameter(Mandatory=$true)][ValidateSet('Install', 'Start', 'Stop', 'Status')][string]$Action,
    [string]$PostgresRoot,
    [string]$VectorBuild,
    [ValidateRange(1024,65535)][int]$Port = 15432
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeRoot = Join-Path $projectRoot '.runtime'
$postgresPath = Join-Path $runtimeRoot 'postgresql'
$dataPath = Join-Path $projectRoot 'data/memory-postgres'
$configPath = Join-Path $runtimeRoot 'memory-postgres.json'
$logPath = Join-Path $dataPath 'server.log'
$utf8 = New-Object System.Text.UTF8Encoding($false)

function Invoke-Postgres {
    param([string]$Program, [string[]]$Arguments)
    & (Join-Path $postgresPath ('bin/' + $Program + '.exe')) @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program failed (exit $LASTEXITCODE)" }
}

function Start-Database {
    & (Join-Path $postgresPath 'bin/pg_ctl.exe') -D $dataPath status *> $null
    if ($LASTEXITCODE -eq 0) {
        Write-Output 'Project PostgreSQL is already running.'
        return
    }
    Invoke-Postgres 'pg_ctl' @('-D', $dataPath, '-l', $logPath, '-w', '-t', '30', 'start')
}

if ($Action -eq 'Install') {
    if (-not $PostgresRoot -or -not $VectorBuild) {
        throw 'Install requires -PostgresRoot and -VectorBuild (compiled pgvector 0.8.1 for PostgreSQL 13).'
    }
    $sourceRoot = (Resolve-Path -LiteralPath $PostgresRoot).Path
    $vectorRoot = (Resolve-Path -LiteralPath $VectorBuild).Path
    $version = & (Join-Path $sourceRoot 'bin/pg_config.exe') --version
    if ($LASTEXITCODE -ne 0 -or $version -notmatch '^PostgreSQL 13\.') {
        throw 'This pgvector build requires PostgreSQL 13.'
    }
    if (-not (Test-Path -LiteralPath (Join-Path $vectorRoot 'vector.dll'))) {
        throw 'Compiled vector.dll not found.'
    }
    New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
    if (-not (Test-Path -LiteralPath (Join-Path $postgresPath 'bin/pg_ctl.exe'))) {
        New-Item -ItemType Directory -Path $postgresPath -Force | Out-Null
        foreach ($folder in @('bin', 'lib', 'share')) {
            Copy-Item -LiteralPath (Join-Path $sourceRoot $folder) -Destination $postgresPath -Recurse
        }
        foreach ($license in @('server_license.txt', 'commandlinetools_3rd_party_licenses.txt')) {
            Copy-Item -LiteralPath (Join-Path $sourceRoot $license) -Destination $postgresPath
        }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $postgresPath 'lib/vector.dll'))) {
        & (Join-Path $PSScriptRoot 'install-pgvector.ps1') -BuildDirectory $vectorRoot -PostgresRoot $postgresPath
    }
    if (-not (Test-Path -LiteralPath $configPath)) {
        if (Test-Path -LiteralPath (Join-Path $dataPath 'PG_VERSION')) {
            throw 'Data exists but credentials are missing; restore memory-postgres.json before continuing.'
        }
        $randomBytes = New-Object byte[] 32
        $random = [System.Security.Cryptography.RandomNumberGenerator]::Create()
        $random.GetBytes($randomBytes)
        $random.Dispose()
        $password = [BitConverter]::ToString($randomBytes).Replace('-', '').ToLowerInvariant()
        $config = @{
            database_url = "postgresql://agentsteam_memory:${password}@127.0.0.1:${Port}/agentsteam_memory"
            port = $Port
            postgres_version = $version
            pgvector_version = '0.8.1'
        }
        [IO.File]::WriteAllText($configPath, ($config | ConvertTo-Json), $utf8)
    }
    $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    if ($config.port -ne $Port) { throw 'Existing port differs; use the port saved in memory-postgres.json.' }
    if (-not (Test-Path -LiteralPath (Join-Path $dataPath 'PG_VERSION'))) {
        $passwordPath = Join-Path $runtimeRoot 'memory-init-password.tmp'
        $password = ([Uri]$config.database_url).UserInfo.Split(':', 2)[1]
        try {
            [IO.File]::WriteAllText($passwordPath, $password, $utf8)
            Invoke-Postgres 'initdb' @('-D', $dataPath, '-U', 'agentsteam_memory', '--auth=scram-sha-256',
                '--encoding=UTF8', '--locale=C', "--pwfile=$passwordPath")
            [IO.File]::AppendAllText((Join-Path $dataPath 'postgresql.conf'),
                "`nlisten_addresses = '127.0.0.1'`nport = $Port`npassword_encryption = 'scram-sha-256'`n", $utf8)
        } finally {
            Remove-Item -LiteralPath $passwordPath -ErrorAction SilentlyContinue
        }
    }
    Start-Database
    $previousPassword = $env:PGPASSWORD
    try {
        $env:PGPASSWORD = ([Uri]$config.database_url).UserInfo.Split(':', 2)[1]
        $connection = @('-h', '127.0.0.1', '-p', [string]$config.port, '-U', 'agentsteam_memory', '-w')
        $exists = Invoke-Postgres 'psql' ($connection + @('-d', 'postgres', '-tA', '-c', "SELECT 1 FROM pg_database WHERE datname='agentsteam_memory'"))
        if ($exists -ne '1') {
            Invoke-Postgres 'createdb' ($connection + @('agentsteam_memory'))
        }
        Invoke-Postgres 'psql' ($connection + @('-d', 'agentsteam_memory', '-v', 'ON_ERROR_STOP=1', '-c', 'CREATE EXTENSION IF NOT EXISTS vector;'))
    } finally {
        $env:PGPASSWORD = $previousPassword
    }
    Write-Output "Project database ready at 127.0.0.1:$($config.port). Credentials saved in .runtime/memory-postgres.json (not printed)."
    exit 0
}

if (-not (Test-Path -LiteralPath $configPath)) { throw 'Project PostgreSQL is not installed. Run Install first.' }
switch ($Action) {
    'Start' { Start-Database }
    'Status' {
        & (Join-Path $postgresPath 'bin/pg_ctl.exe') -D $dataPath status
        exit $LASTEXITCODE
    }
    'Stop' {
        & (Join-Path $postgresPath 'bin/pg_ctl.exe') -D $dataPath status *> $null
        if ($LASTEXITCODE -eq 0) {
            Invoke-Postgres 'pg_ctl' @('-D', $dataPath, '-m', 'fast', '-w', '-t', '30', 'stop')
        } else {
            Write-Output 'Project PostgreSQL is already stopped.'
        }
    }
}
