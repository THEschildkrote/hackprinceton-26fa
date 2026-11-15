# the belt demo: the laptop is the one server (api + imessage bot), on aaron's hotspot, driving the belt.
# run from the repo root in powershell:   .\tools\demo-laptop.ps1
# the laptop should be 172.20.10.3 on the hotspot; the isaac apps point at http://172.20.10.3:8765.
# while this runs, stop the railway copy (railway down) or texts get two replies, and point the phone line here
# with a tunnel:   cloudflared tunnel --url http://127.0.0.1:8765   then
#                  python tools/phone_line.py base --base https://<the tunnel url>

$ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -like '172.20.10.*' }).IPAddress
if (-not $ip) { Write-Host "Not on the hotspot: no 172.20.10.x address. Join Aaron's iPhone first." -ForegroundColor Red; exit 1 }
Write-Host "laptop on the hotspot at $ip  ->  apps use http://${ip}:8765"

$env:NUDGE_BELT_URL = "http://172.20.10.2:8000"
Remove-Item Env:NUDGE_BAND -ErrorAction SilentlyContinue
$env:NUDGE_SID = "R1FX6EmdcDt"

try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 4 "http://172.20.10.2:8000/state" | Out-Null; Write-Host "belt (pi) answers at 172.20.10.2" }
catch { Write-Host "belt (pi) does not answer at 172.20.10.2:8000 - is it on and on the hotspot?" -ForegroundColor Yellow }

python -m nudge.photon_bot
