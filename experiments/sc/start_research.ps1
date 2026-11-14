$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
docker compose up -d
if ($LASTEXITCODE -ne 0) { throw 'Could not start AgentField control plane.' }
$env:AGENTFIELD_REGISTER = 'true'
$env:AGENTFIELD_SERVER = 'http://localhost:18080'
$env:AGENT_CALLBACK_URL = 'http://host.docker.internal:18081'
& .\.venv\Scripts\python.exe -X utf8 market_research.py --serve
