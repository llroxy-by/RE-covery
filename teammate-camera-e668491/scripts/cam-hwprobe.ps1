# cam-hwprobe.ps1 - READ-ONLY hardware probe: why do the arm RGB cameras return no frame?
#
# STRICTLY READ-ONLY. It does NOT call robot.init(), does NOT enable anything, does NOT
# send motion, does NOT write / install / restart anything on the robot. It only asks the
# OS which video devices exist, who is holding them, and what the SDK docstrings say.
#
# WHY THIS EXISTS
#   robot-cam.ps1 proved: HEAD_LEFT_CAMERA -> jpeg 1280x960 OK, LEFT/RIGHT_ARM_DEPTH ->
#   16uc1 1280x720 OK, but LEFT_ARM_CAMERA / RIGHT_ARM_CAMERA -> empty in every format
#   variant, and LEFT/RIGHT_ARM_DOWN_CAMERA -> init refused. "init OK + stream silent"
#   has three possible causes and the robot's own OS can separate them:
#     a) not physically fitted / stream not enabled in robot config
#     b) fitted, but another process holds the video node
#     c) fitted and free, but the SDK does not map this enum to it on this hardware rev
#
# WHY THE WHOLE REMOTE SCRIPT IS BASE64 (v3)
#   v1 shipped python inline after -c with doubled single quotes. A single-quoted here-string
#   is LITERAL: '' is NOT collapsed to ', so bash got '''<code>''' and died with
#   "syntax error near unexpected token".
#   v2 switched to a heredoc, which fixed the quoting - but measurement on this machine
#   showed PowerShell 5.1 destroys a multi-line argument on its way to a native program:
#   an 1884-char script arrived at bash as 1596 chars with every space and newline gone.
#   So the transport is the only thing that must not contain whitespace.
#   => base64 the script, send ONE single-token command: echo <b64> | base64 -d | bash.
#   This is the same proven pattern robot-cam.ps1 uses for its 28,924-char remote program.
#
# OUTPUT CONTRACT (see the earlier false-positive lesson)
#   Every finding is a positive MARK_ line. A section that produced no MARK_ line is
#   reported as NO EVIDENCE - never as "fine". MARK_DONE at the end proves the remote
#   script ran to completion; if MARK_DONE is missing, no conclusion may be drawn.
#
# USAGE
#   .\cam-hwprobe.ps1                      # will prompt for the password, interactively
#   .\cam-hwprobe.ps1 -KeyFile $env:USERPROFILE\.ssh\g1_ed25519
#   .\cam-hwprobe.ps1 -SaveLog probe2.log

param(
    [string]$RobotIp    = '192.168.167.46',
    [string]$User       = 'galbot',
    [string]$KeyFile    = '',
    [int]   $TimeoutSec = 150,
    [string]$SaveLog    = ''
)

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'

function Step($n, $t) { Write-Host ""; Write-Host "[$n] $t" -ForegroundColor Cyan }

# ------------------------------------------------------------------ remote program
# Single-quoted here-string: literal, no interpolation. No double quotes anywhere.
$remote = @'
echo MARK_HOST $(hostname)
echo MARK_UNAME $(uname -r)
echo MARK_VIDEO_NODES $(ls /dev/video* 2>/dev/null | wc -l)
ls -l /dev/video* 2>/dev/null | sed s/^/MARK_VNODE_/
command -v v4l2-ctl >/dev/null 2>&1 && echo MARK_V4L2CTL yes || echo MARK_V4L2CTL no
v4l2-ctl --list-devices 2>/dev/null | sed s/^/MARK_V4L_/
command -v lsusb >/dev/null 2>&1 && echo MARK_LSUSB yes || echo MARK_LSUSB no
lsusb 2>/dev/null | sed s/^/MARK_USB_/
echo MARK_PS_BEGIN
ps -eo pid,etime,args 2>/dev/null | grep -iE camera\|percept\|realsense\|orbbec\|galbot\|ros2\|python | grep -v grep | head -40 | sed s/^/MARK_PS_/
echo MARK_PS_END
command -v ros2 >/dev/null 2>&1 && echo MARK_ROS2 yes || echo MARK_ROS2 no
echo MARK_TOPIC_BEGIN
timeout 12 ros2 topic list 2>/dev/null | grep -i camera | head -40 | sed s/^/MARK_TOPIC_/
echo MARK_TOPIC_END
echo MARK_CFG_BEGIN
ls /data/galbot 2>/dev/null | sed s/^/MARK_GALBOTDIR_/
grep -rIlE arm_camera /data/galbot 2>/dev/null | head -20 | sed s/^/MARK_CFGFILE_/
grep -rIhE arm_camera /data/galbot 2>/dev/null | head -40 | sed s/^/MARK_CFGLINE_/
echo MARK_CFG_END
echo MARK_PY_BEGIN
command -v python3 >/dev/null 2>&1 && echo MARK_PY3 yes || echo MARK_PY3 no
PYTHONPATH=/data/galbot/lib LD_LIBRARY_PATH=/data/galbot/lib python3 -B - 2>&1 <<PYPY | sed s/^/MARK_PY_/
import sys
print('VER', sys.version.split()[0])
try:
    from galbot_sdk.g1 import SensorType as S, GalbotRobot as R
    print('ENUM', sorted(S.__members__))
    print('API', [x for x in dir(R) if not x.startswith('_')])
    d = (R.get_rgb_data.__doc__ or '-').replace(chr(10), ' ')
    print('DOC_RGB', d[:700])
    try:
        di = (R.init.__doc__ or '-').replace(chr(10), ' ')
        print('DOC_INIT', di[:700])
    except Exception as e:
        print('DOC_INIT_ERR', type(e).__name__)
except Exception as e:
    print('IMPORTERR', type(e).__name__, str(e)[:300])
PYPY
echo MARK_PY_END
echo MARK_DONE
'@

# ------------------------------------------------------------------ run
$sshArgs = @(
    '-T',
    '-o', 'ConnectTimeout=12',
    '-o', 'ServerAliveInterval=10',
    '-o', 'StrictHostKeyChecking=accept-new'
)
if ($KeyFile) {
    $sshArgs += @('-i', $KeyFile, '-o', 'BatchMode=yes')
}

# Transport: bash text -> UTF8 -> base64 -> ONE whitespace-free argv token.
# PS 5.1 mangles any multi-line argument on the way to a native program (measured: 1884
# chars in, 1596 out, all spaces and newlines stripped), and base64 has no whitespace.
$b64       = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($remote))
$remoteCmd = "echo $b64 | base64 -d | bash"
$sshArgs  += @("$User@$RobotIp", $remoteCmd)

Step 1 "Read-only probe of $RobotIp  (no init, no enable, no motion, no writes)"
if (-not $KeyFile) { Write-Host "Enter the robot password when prompted (it will not echo)." -ForegroundColor Yellow }

$sw = [System.Diagnostics.Stopwatch]::StartNew()
# stdin is NOT redirected (the password prompt needs the console) and stderr is left on
# the console so a remote bash error is visible verbatim. Only stdout is captured.
# EAP is lowered for the duration because PS 5.1 raises a terminating NativeCommandError
# the moment a native program writes to stderr, even though we set EAP to Stop above.
$prevEap = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$out  = ''
$code = 0
try {
    # -Width 4096: Out-String wraps at console width by default and would corrupt long lines.
    $out  = (& ssh.exe @sshArgs | Out-String -Width 4096)
    $code = $LASTEXITCODE
} catch {
    Write-Host ("ssh invocation failed: " + $_.Exception.Message) -ForegroundColor Red
    $code = -1
} finally {
    $ErrorActionPreference = $prevEap
    $sw.Stop()
}

if ($SaveLog -and $out) { $out | Set-Content -LiteralPath $SaveLog -Encoding UTF8 }

$lines = ($out -split "`r?`n") | Where-Object { $_.Trim() -ne '' }
$marks = @($lines | Where-Object { $_ -match '^MARK_' })

Step 2 "Facts"
Write-Host ("ssh exit code : {0}   elapsed: {1:N1}s" -f $code, $sw.Elapsed.TotalSeconds)
Write-Host ("MARK_ lines   : {0}" -f $marks.Count)

$done   = @($marks | Where-Object { $_ -match '^MARK_DONE' }).Count -gt 0
$videoN = $marks | Where-Object { $_ -match '^MARK_VIDEO_NODES ' } | Select-Object -First 1
$v4l2   = $marks | Where-Object { $_ -match '^MARK_V4L2CTL ' }     | Select-Object -First 1
$lsusb  = $marks | Where-Object { $_ -match '^MARK_LSUSB ' }       | Select-Object -First 1
$ros2   = $marks | Where-Object { $_ -match '^MARK_ROS2 ' }        | Select-Object -First 1
$py3    = $marks | Where-Object { $_ -match '^MARK_PY3 ' }         | Select-Object -First 1

if (-not $done) {
    Write-Host "UNKNOWN  MARK_DONE is absent - the remote script did not finish. Do not conclude anything." -ForegroundColor Red
    Write-Host "         Remote bash errors, if any, were printed above on stderr." -ForegroundColor Red
} elseif ($code -ne 0) {
    Write-Host ("WARN     MARK_DONE seen but ssh exit code is {0}" -f $code) -ForegroundColor Yellow
} else {
    Write-Host "OK       MARK_DONE seen, the remote script finished" -ForegroundColor Green
}

Write-Host ""
Write-Host ("/dev/video* nodes : " + ($(if ($videoN) { $videoN -replace '^MARK_VIDEO_NODES ', '' } else { '?' })))
Write-Host ("v4l2-ctl present  : " + ($(if ($v4l2)  { $v4l2 -replace '^MARK_V4L2CTL ', '' }  else { '?' })))
Write-Host ("lsusb present     : " + ($(if ($lsusb) { $lsusb -replace '^MARK_LSUSB ', '' }    else { '?' })))
Write-Host ("ros2 CLI          : " + ($(if ($ros2)  { $ros2 -replace '^MARK_ROS2 ', '' }      else { '?' })))
Write-Host ("python3           : " + ($(if ($py3)   { $py3 -replace '^MARK_PY3 ', '' }        else { '?' })))

Write-Host ""
Write-Host "--- per-section evidence count ---" -ForegroundColor Cyan
$sections = [ordered]@{
    'VNODE (video nodes)' = '^MARK_VNODE_'
    'V4L  (device names)' = '^MARK_V4L_'
    'USB  (lsusb)'        = '^MARK_USB_'
    'PS   (processes)'    = '^MARK_PS_\d'
    'TOPIC(ros2 topics)'  = '^MARK_TOPIC_\S'
    'CFGFILE (configs)'   = '^MARK_CFGFILE_'
    'CFGLINE (configs)'   = '^MARK_CFGLINE_'
    'PY   (sdk enum/doc)' = '^MARK_PY_'
}
foreach ($k in $sections.Keys) {
    $n = @($marks | Where-Object { $_ -match $sections[$k] }).Count
    $verdict = if ($n -gt 0) { "evidence $n line(s)" } else { "NO EVIDENCE" }
    $color   = if ($n -gt 0) { 'Gray' } else { 'Yellow' }
    Write-Host ("  {0,-22} {1}" -f $k, $verdict) -ForegroundColor $color
}

Step 3 "Interpretation"
Write-Host "  /dev/video* == 0        -> the OS exposes no UVC node; the sensors go through the"
Write-Host "                             vendor driver service, so USB/V4L cannot answer the"
Write-Host "                             fitment question. Use the PS + CFG + PY lines instead."
Write-Host "  /dev/video* > 0 but no  -> a wrist node nobody opened (absent from PS, absent from"
Write-Host "  matching stream            TOPIC) is fitted-but-idle, which points at config (a)."
Write-Host "  CFGLINE enable = false  -> arm RGB disabled in the robot config. Editing config is"
Write-Host "                             a robot-side change: stop and ask the team lead."
Write-Host "  DOC_RGB states a        -> quote the SDK's own wording back to the lead; it is the"
Write-Host "  precondition               authoritative answer, not forum posts."
Write-Host ""

if ($marks.Count -gt 0) {
    Write-Host "--- raw MARK_ lines ---" -ForegroundColor Cyan
    $marks | ForEach-Object { Write-Host $_ }
}

Write-Host ""
Write-Host "Nothing was written to the robot and no SDK session was opened. Safe to re-run." -ForegroundColor Green
if ($SaveLog -and $out) { Write-Host "Log saved to $SaveLog" -ForegroundColor Green }
