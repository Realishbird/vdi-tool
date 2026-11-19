# vdi-tool workload for Windows desktops. Started by the harness through the QEMU guest agent.
# Runs the disk workloads with Microsoft DiskSpd and reports results in the same shape as the Linux agent.
param(
  [Parameter(Mandatory = $true)][string]$Spec,      # base64 JSON: {kind, fio?}
  [int]$Duration = 120,
  [Parameter(Mandatory = $true)][string]$Report,    # URL to post progress and the result to
  [Parameter(Mandatory = $true)][string]$Base,      # the tool's base URL
  [int]$SizeGb = 1
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Work = 'C:\vdi-workload'
New-Item -ItemType Directory -Force -Path $Work | Out-Null

function Send($payload) {
  # Many desktops report at the same moment, so one lost request must not lose the report.
  $body = $payload | ConvertTo-Json -Depth 8 -Compress
  foreach ($i in 0..3) {
    try { Invoke-RestMethod -Uri $Report -Method Post -Body $body -ContentType 'application/json' -TimeoutSec 10 | Out-Null; return }
    catch { Start-Sleep -Seconds (1 + $i * 2) }
  }
}

function Bytes([string]$text) {
  $t = $text.Trim().ToLower()
  if ($t.EndsWith('k')) { return [double]$t.TrimEnd('k') * 1KB }
  if ($t.EndsWith('m')) { return [double]$t.TrimEnd('m') * 1MB }
  return [double]$t
}

function Prepare([string]$target) {
  # Write the test file with real random data. A file that is only reserved would be read back
  # as zeros without touching the storage.
  $marker = "$target.ok"
  $want = [int64]$SizeGb * 1GB
  if ((Test-Path $marker) -and (Test-Path $target) -and ((Get-Content $marker -Raw).Trim() -eq "$SizeGb") -and ((Get-Item $target).Length -eq $want)) { return }
  Remove-Item -Force -ErrorAction SilentlyContinue $marker, $target
  Send @{ type = 'preparing' }
  $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  $buf = New-Object byte[] (4MB)
  $fs = New-Object System.IO.FileStream($target, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None, 4MB, [System.IO.FileOptions]::WriteThrough)
  try {
    $left = $want
    while ($left -gt 0) {
      $rng.GetBytes($buf)
      $n = [int][Math]::Min([int64]$buf.Length, $left)
      $fs.Write($buf, 0, $n)
      $left -= $n
    }
    $fs.Flush($true)
  } finally { $fs.Dispose(); $rng.Dispose() }
  Set-Content -Path $marker -Value "$SizeGb" -NoNewline
}

function Side($xml, [string]$kind, [double]$seconds) {
  # kind is Read or Write. Sums DiskSpd's per-thread counters and reads the latency percentiles.
  $targets = @($xml.Results.TimeSpan.Thread | ForEach-Object { $_.Target })
  $bytes = ($targets | ForEach-Object { [double]$_."${kind}Bytes" } | Measure-Object -Sum).Sum
  $count = ($targets | ForEach-Object { [double]$_."${kind}Count" } | Measure-Object -Sum).Sum
  $avgNode = $xml.Results.TimeSpan.Latency."Average${kind}Milliseconds"
  $pct = @{}
  foreach ($b in $xml.Results.TimeSpan.Latency.Bucket) { $pct[[double]$b.Percentile] = $b."${kind}Milliseconds" }
  $at = { param($p) if ($pct.ContainsKey([double]$p) -and $pct[[double]$p]) { [Math]::Round([double]$pct[[double]$p], 2) } else { 0 } }
  return @{
    iops = [int][Math]::Round($count / $seconds); mbps = [Math]::Round($bytes / 1e6 / $seconds, 1)
    lat_ms = $(if ($avgNode) { [Math]::Round([double]$avgNode, 2) } else { 0 })
    p50_ms = (& $at 50); p95_ms = (& $at 95); p99_ms = (& $at 99); max_ms = (& $at 100)
  }
}

function Disk($f) {
  $target = Join-Path $Work 'test.dat'
  Get-Process diskspd -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
  $exe = Join-Path $Work 'diskspd.exe'
  if (-not (Test-Path $exe)) { Invoke-WebRequest -Uri "$Base/agent/diskspd.exe" -OutFile $exe -UseBasicParsing -TimeoutSec 120 }
  Prepare $target
  Send @{ type = 'prepared' }
  $go = ($Report -split '/report/')[0] + '/go'
  while ($true) {
    $state = $null
    try { $state = Invoke-RestMethod -Uri $go -TimeoutSec 10 } catch { }
    if ($state -and $state.go) { break }
    if ($state -and $state.over) { throw 'the run ended before this desktop was ready' }
    Start-Sleep -Seconds 2
  }
  Send @{ type = 'started' }

  $rw = if ($f.rw) { [string]$f.rw } else { 'randrw' }
  $mix = if ($null -ne $f.rwmixread) { [int]$f.rwmixread } else { 70 }
  $write = switch ($rw) { 'randread' { 0 } 'read' { 0 } 'randwrite' { 100 } 'write' { 100 } default { 100 - $mix } }
  # DiskSpd has one block size per run, so a mix of sizes becomes its weighted average.
  $bs = 4KB
  if ($f.bssplit) {
    $sum = 0.0; $w = 0.0
    foreach ($part in ([string]$f.bssplit).Split(':')) { $a, $p = $part.Split('/'); $sum += (Bytes $a) * [double]$p; $w += [double]$p }
    $bs = [int]([Math]::Round($sum / $w / 4KB) * 4KB)
  } elseif ($f.bs) { $bs = [int](Bytes ([string]$f.bs)) }
  $depth = if ($f.iodepth) { [int]$f.iodepth } else { 16 }
  $jobs = if ($f.numjobs) { [int]$f.numjobs } else { 1 }
  $a = @("-b$bs", "-o$depth", "-t$jobs", "-w$write", "-d$Duration", '-W0', '-Sh', '-L', '-Rxml')
  if ($rw -like 'rand*') { $a += '-r' } else { $a += '-si' }
  if ($f.rate_iops) { $a += ('-g{0}i' -f [int][Math]::Max(1, [Math]::Round([double]$f.rate_iops / $jobs))) }
  $a += $target
  $out = & $exe @a 2>&1 | Out-String
  if ($LASTEXITCODE -ne 0) { throw ('diskspd failed: ' + $out.Substring([Math]::Max(0, $out.Length - 250))) }
  $xml = [xml]$out.Substring($out.IndexOf('<Results'))
  $secs = [double]$xml.Results.TimeSpan.TestTimeSeconds
  $r = Side $xml 'Read' $secs
  $w = Side $xml 'Write' $secs
  return @{ read = $r; write = $w; iops = $r.iops + $w.iops; mbps = [Math]::Round($r.mbps + $w.mbps, 1) }
}

try {
  $s = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Spec)) | ConvertFrom-Json
  if ($s.kind -ne 'fio') { throw "the $($s.kind) workload is not available on Windows yet" }
  Send @{ type = 'result'; result = (Disk $s.fio) }
} catch {
  $msg = "$($_.Exception.Message)"
  Send @{ type = 'error'; error = $msg.Substring(0, [Math]::Min(300, $msg.Length)) }
}
