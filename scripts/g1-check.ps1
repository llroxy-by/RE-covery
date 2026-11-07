# g1-check.ps1 - read-only wireless pre-flight check for the G1 robot.
# Usage:  powershell -ExecutionPolicy Bypass -File .\g1-check.ps1 -RobotIp 192.168.167.46
# Does NOT log in, does NOT write to known_hosts, does NOT touch the robot.
# Exit codes: 0 = all pass, 1 = subnet mismatch, 2 = port closed, 3 = fingerprint mismatch.

param(
    [string]$RobotIp    = '192.168.167.46',
    [int]   $Port       = 22,
    [string]$ExpectedFp = 'SHA256:1MH5iIhYyb2gK5Q02SOYqO0Ynil5Xqgh14oqNz/5s6k',
    [int]   $PrefixLen  = 23
)

$ErrorActionPreference = 'Continue'

function Section($t) { Write-Host ""; Write-Host "=== $t ===" }

# Network number for an address under a given prefix length. Uses UInt64 because
# PowerShell parses the literal 0xFFFFFFFF as Int32 -1 and [uint32] cast then throws.
function Get-NetNum([string]$ip, [int]$p) {
    $b = ([System.Net.IPAddress]::Parse($ip)).GetAddressBytes()
    $v = ([uint64]$b[0] -shl 24) -bor ([uint64]$b[1] -shl 16) -bor ([uint64]$b[2] -shl 8) -bor [uint64]$b[3]
    if ($p -le 0)  { return [uint64]0 }
    if ($p -ge 32) { return $v }
    $mask = (([uint64]4294967295) -shl (32 - $p)) -band ([uint64]4294967295)
    return ($v -band $mask)
}

# --- 1. local IPv4 ------------------------------------------------------------
Section "1. Local IPv4"
$locals = @(Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' })
$locals | Select-Object InterfaceAlias, IPAddress, PrefixLength | Format-Table -AutoSize |
    Out-String -Width 200 | Write-Host

$ssidLine = (netsh wlan show interfaces | Out-String) -split "`r?`n" |
    Where-Object { $_ -match '^\s*SSID\s*:' } | Select-Object -First 1
if ($ssidLine) { Write-Host ("Current SSID : " + ($ssidLine -replace '^\s*SSID\s*:\s*', '').Trim()) }

# --- 2. subnet membership -----------------------------------------------------
Section "2. Subnet membership (/$PrefixLen)"
$robotNet = Get-NetNum $RobotIp $PrefixLen
$hit = $locals | Where-Object {
    $_.PrefixLength -eq $PrefixLen -and (Get-NetNum $_.IPAddress $PrefixLen) -eq $robotNet
} | Select-Object -First 1
if ($hit) {
    Write-Host ("OK   {0}/{1} is on the same network as {2}/{3}" -f $hit.IPAddress, $hit.PrefixLength, $RobotIp, $PrefixLen)
} else {
    Write-Host ("FAIL no local interface sits on {0}/{1}" -f $RobotIp, $PrefixLen)
    Write-Host "     Join the venue Wi-Fi and re-run. Never hand-set your IP to the robot's."
    exit 1
}

# --- 3. TCP port (TcpClient: quiet, no progress spam) -------------------------
Section "3. TCP $Port"
$tcpOk = $false
$cl = New-Object System.Net.Sockets.TcpClient
try {
    $iar = $cl.BeginConnect($RobotIp, $Port, $null, $null)
    $tcpOk = $iar.AsyncWaitHandle.WaitOne(5000, $false) -and $cl.Connected
} catch { $tcpOk = $false } finally { $cl.Close() }
Write-Host ("Port $Port reachable : $tcpOk")

$pingOk = $false
try { $pingOk = Test-Connection -ComputerName $RobotIp -Count 1 -Quiet -ErrorAction Stop } catch { $pingOk = $false }
Write-Host ("ICMP echo            : $pingOk  (may be blocked; irrelevant when the port is open)")

if (-not $tcpOk) {
    Write-Host "FAIL sshd unreachable. Re-read the IP on the robot screen: DHCP reassigns it on reboot."
    exit 2
}

# --- 4. host key fingerprint, no known_hosts pollution ------------------------
Section "4. SSH ED25519 host key"
$kh  = Join-Path $env:TEMP ("g1probe_" + [guid]::NewGuid().ToString('N'))
$out = ssh -v -o BatchMode=yes -o StrictHostKeyChecking=no -o PreferredAuthentications=none `
           -o UserKnownHostsFile=$kh -o ConnectTimeout=8 "galbot@$RobotIp" exit 2>&1 |
       Out-String -Width 4096
Remove-Item $kh -ErrorAction SilentlyContinue

$m = [regex]::Match($out, 'Server host key: (ssh-\S+) (SHA256:\S+)')
if (-not $m.Success) {
    Write-Host "WARN could not extract the host key from ssh -v output"
} else {
    $seen = $m.Groups[2].Value
    Write-Host ("Algorithm : " + $m.Groups[1].Value)
    Write-Host ("Observed  : " + $seen + "  (" + $seen.Length + " chars)")
    Write-Host ("Expected  : " + $ExpectedFp + "  (" + $ExpectedFp.Length + " chars)")
    if ($seen -ceq $ExpectedFp) {
        Write-Host "OK   fingerprint matches exactly"
    } else {
        Write-Host "FAIL fingerprint MISMATCH - do not accept. Ask on-site staff."
        exit 3
    }
}

Section "Ready"
Write-Host "Next   : ssh galbot@$RobotIp    (type the password yourself; never store it)"
Write-Host "Then   : hostname ; ip -brief address ; df -h /dev/shm"
Write-Host "Note   : no motion command without an explicit handover from the team lead."
exit 0
