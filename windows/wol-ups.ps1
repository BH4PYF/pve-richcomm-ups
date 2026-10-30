param([switch]$Test, [switch]$Force)

# Wake PVE and NAS. Retries for a few minutes so a slow POST still ends up online.
# Run by scheduled task "UPS-WOL-0800".
# Reachability is checked with a TCP port, never ping: the PVE board's Intel AMT
# answers ICMP while the OS is off (TTL 255), which made ping useless there.

$ErrorActionPreference = "Continue"

$log = Join-Path $PSScriptRoot "wol.log"
$broadcasts = @("192.168.88.255", "255.255.255.255")
$ports = @(7, 9)

$targets = @(
    [pscustomobject]@{ Name = "PVE"; Ip = "192.168.88.1";  Mac = "00-D8-61-B2-3D-83"; Port = 8006 },
    [pscustomobject]@{ Name = "NAS"; Ip = "192.168.88.99"; Mac = "48-F3-17-06-28-58"; Port = 5000 }
)

if ($Test) {
    $targets = @([pscustomobject]@{ Name = "TEST"; Ip = "192.168.88.250"; Mac = "02-00-00-00-00-01"; Port = 0 })
}

function Write-Log([string]$msg) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path $log -Value $line -Encoding UTF8
    Write-Output $line
}

function New-MagicPacket([string]$mac) {
    $bytes = [byte[]]($mac -split "[:-]" | ForEach-Object { [Convert]::ToByte($_, 16) })
    $packet = New-Object System.Collections.Generic.List[byte]
    for ($i = 0; $i -lt 6; $i++) { $packet.AddRange([byte[]](0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF)) }
    for ($i = 0; $i -lt 16; $i++) { $packet.AddRange($bytes) }
    return $packet.ToArray()
}

function Send-Wol($target) {
    $pkt = New-MagicPacket $target.Mac
    $sent = 0
    foreach ($bc in $broadcasts) {
        foreach ($port in $ports) {
            try {
                $udp = New-Object System.Net.Sockets.UdpClient
                $udp.EnableBroadcast = $true
                $udp.Send($pkt, $pkt.Length, $bc, $port) | Out-Null
                $udp.Close()
                $sent++
            } catch {
                Write-Log ("send {0} {1}:{2} failed: {3}" -f $target.Name, $bc, $port, $_.Exception.Message)
            }
        }
    }
    Write-Log ("magic packet -> {0} ({1}) x{2}" -f $target.Name, $target.Mac, $sent)
}

function Test-Up($t) {
    if (-not $t.Port) {
        try { return [bool](Test-Connection -ComputerName $t.Ip -Count 1 -Quiet -ErrorAction Stop) } catch { return $false }
    }
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $client.BeginConnect($t.Ip, $t.Port, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne(1200)) { return $false }
        $client.EndConnect($iar)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

if ($Test) {
    Write-Log "TEST MODE: sending to dummy MAC only, no real target will wake"
    Send-Wol $targets[0]
    exit 0
}

Write-Log "wake run start"

if ($Force) {
    foreach ($t in $targets) { Send-Wol $t }
    Start-Sleep -Seconds 20
}

$allUp = $false
for ($round = 1; $round -le 6; $round++) {
    $pending = @()
    foreach ($t in $targets) {
        if (Test-Up $t) {
            Write-Log ("{0} up (port {1})" -f $t.Name, $t.Port)
        } else {
            Send-Wol $t
            $pending += $t.Name
        }
    }
    if ($pending.Count -eq 0) { $allUp = $true; break }
    Write-Log ("round {0}: waiting 45 s for {1}" -f $round, ($pending -join ","))
    Start-Sleep -Seconds 45
}

foreach ($t in $targets) {
    if (Test-Up $t) { Write-Log ("final: {0} reachable" -f $t.Name) }
    else { Write-Log ("final: {0} NOT reachable" -f $t.Name) }
}
if ($allUp) { Write-Log "wake run done: both up" } else { Write-Log "wake run done: see above" }
