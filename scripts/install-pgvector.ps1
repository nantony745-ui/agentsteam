param(
    [Parameter(Mandatory=$true)][string]$BuildDirectory,
    [Parameter(Mandatory=$true)][string]$PostgresRoot
)
$ErrorActionPreference = 'Stop'
$sourceRoot = (Resolve-Path -LiteralPath $BuildDirectory).Path
$targetRoot = (Resolve-Path -LiteralPath $PostgresRoot).Path
if (-not (Test-Path -LiteralPath (Join-Path $sourceRoot 'vector.dll'))) { throw '缺少已编译 vector.dll' }
if (-not (Test-Path -LiteralPath (Join-Path $targetRoot 'bin/pg_config.exe'))) { throw '不是 PostgreSQL 安装目录' }
$version = & (Join-Path $targetRoot 'bin/pg_config.exe') --version
if ($version -notmatch '^PostgreSQL 13\.') { throw '本次构建仅适用于 PostgreSQL 13' }
Copy-Item -LiteralPath (Join-Path $sourceRoot 'vector.dll') -Destination (Join-Path $targetRoot 'lib/vector.dll')
Copy-Item -LiteralPath (Join-Path $sourceRoot 'vector.control') -Destination (Join-Path $targetRoot 'share/extension/vector.control')
Get-ChildItem -LiteralPath (Join-Path $sourceRoot 'sql') -Filter 'vector--*.sql' | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $targetRoot ('share/extension/' + $_.Name))
}
Write-Output 'pgvector 扩展文件安装完成；未连接或修改任何数据库。'
