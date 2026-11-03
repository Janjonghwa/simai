param([Parameter(Mandatory = $true)][string]$Record)
$ErrorActionPreference = 'Stop'
# Resolve the record's single external reference locally; keep the canonical schemas unchanged.
$taskContracts = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../../contracts'))
$taskSchemaText = Get-Content -LiteralPath (Join-Path $taskContracts 'experiments/run_record.schema.json') -Raw
$taskSchema = $taskSchemaText.Replace('../common/defs.schema.json#', '#') | ConvertFrom-Json -AsHashtable
$taskDefs = Get-Content -LiteralPath (Join-Path $taskContracts 'common/defs.schema.json') -Raw | ConvertFrom-Json -AsHashtable
$taskSchema['$defs'] = $taskDefs['$defs']
$taskSchemaJson = ConvertTo-Json -InputObject $taskSchema -Depth 100
$taskRecordJson = Get-Content -LiteralPath $Record -Raw
if (-not (Test-Json -Json $taskRecordJson -Schema $taskSchemaJson)) {
    throw 'Run record schema validation failed'
}
Write-Output "Schema OK: $Record"
