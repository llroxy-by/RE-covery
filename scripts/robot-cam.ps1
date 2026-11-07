# robot-cam.ps1 - enumerate and capture EVERY camera on the Galbot G1, read-only.
#
# WHAT IT DOES
#   -List      ask the robot's SDK which camera sensors exist (no init, no resource cost)
#   -Split     grab every camera, ONE python process per camera (works where -Grab fails)
#   -Diag      isolate why robot.init() returned False (default / single camera / all)
#   -Grab      init the SDK with ALL camera sensors, pull one frame from each, save locally
#   -Selftest  exercise the image encoders without touching the robot (no SSH needed)
#
# WHY IT IS BUILT THIS WAY (SDK 1.10.0, developer.galbot.com/docs/SDK/1.10.0/g1)
#   robot.init(enable_sensor_set: Set[SensorType], enable_sync_mode=False) -> bool
#       ONLY sensors listed here can be read. Enabling all cameras costs startup time.
#   robot.get_rgb_data(camera_id, format=RgbOutputFormat.JPEG, once=True) -> dict
#   robot.get_depth_data(camera_id) -> dict      (only LEFT/RIGHT_ARM_DEPTH_CAMERA)
#   robot.get_ir_data(camera_id)    -> dict      (only *INFRA_CAMERA_*, needs ir_enabled)
#   Every dict carries header / format / width / height / data (+ depth_scale for depth).
#   get_rgb_data defaults to JPEG, so RGB frames are already-compressed bytes: no decoder
#   is needed on either side. Depth is raw uint16, IR is mono8 or JPEG.
#
# THE IMAGE PATH
#   Python on the robot encodes depth/IR to PNG using nothing but the standard library
#   (zlib + struct + array), then ships every frame back over the SAME ssh stdout channel
#   as base64. Nothing is written to the robot's disk - only this PC writes files.
#   stdout is read line by line, never through Out-String, because Out-String WRAPS long
#   lines and would silently corrupt base64.
#
# AUTH
#   Without -KeyFile, ssh shows its password prompt and reads it from the console, so
#   stdin and stderr are deliberately NOT redirected. With -KeyFile it is fully batch.
#
# SAFETY / SCOPE
#   No motion command. No enable. No file on the robot. No package installed. No service
#   restarted. One frame per camera, then the SDK is shut down and released.
#
# PREREQUISITE: tell the team lead before you take the camera channel. init() and the
# camera streams are shared robot resources; do not run this while someone else is
# driving the robot or debugging the perception stack.
#
# USAGE
#   .\robot-cam.ps1 -List
#   .\robot-cam.ps1 -Split
#   .\robot-cam.ps1 -Split -Only HEAD_LEFT_CAMERA,LEFT_ARM_DEPTH_CAMERA
#   .\robot-cam.ps1 -Diag
#   .\robot-cam.ps1 -Grab
#   .\robot-cam.ps1 -Grab -DefaultInit
#   .\robot-cam.ps1 -Grab -Only HEAD_LEFT_CAMERA,LEFT_ARM_DEPTH_CAMERA
#   .\robot-cam.ps1 -Grab -KeyFile $env:USERPROFILE\.ssh\g1_ed25519
#   .\robot-cam.ps1 -Selftest

param(
    [string]$RobotIp   = '192.168.167.46',
    [string]$User      = 'galbot',
    [string[]]$Only    = @(),
    [string]$KeyFile   = '',
    [string]$OutDir    = '',
    [int]   $WarmupSec = 6,
    [int]   $TimeoutSec = 240,
    [string]$LibDir    = '/data/galbot/lib',
    [switch]$Grab,
    [switch]$List,
    [switch]$Selftest,
    [switch]$Diag,
    [switch]$DefaultInit,
    [switch]$Split
)

$ErrorActionPreference = 'Stop'

function Step($n, $t) { Write-Host ""; Write-Host "[$n] $t" -ForegroundColor Cyan }

# ---------------------------------------------------------------- usage guard
if (-not $Grab -and -not $List -and -not $Selftest -and -not $Diag -and -not $DefaultInit -and -not $Split) {
    Write-Host "Nothing to do. Pick one:" -ForegroundColor Yellow
    Write-Host "  .\robot-cam.ps1 -List               # what cameras does this robot report?"
    Write-Host "  .\robot-cam.ps1 -Split              # grab every camera, one process per camera"
    Write-Host "  .\robot-cam.ps1 -Grab               # grab all cameras in one init (refused on this unit)"
    Write-Host "  .\robot-cam.ps1 -Diag               # why did init(sensor_set) return False?"
    Write-Host "  .\robot-cam.ps1 -Selftest           # verify the PNG encoders locally, no robot"
    exit 64
}

# ---------------------------------------------------------------- remote program
# Single-quoted here-string: NO PowerShell interpolation, so the $ signs that python
# does not have and the % format specifiers cannot be mangled. Placeholders are filled
# in afterwards with .Replace().
$pyTemplate = @'
import os, sys, time, json, zlib, struct, array, base64

MODE   = '__MODE__'
ONLY   = __ONLY__
WARM   = __WARM__
LIBDIR = '__LIBDIR__'

def emit(s):
    sys.stdout.write(s + '\n')
    sys.stdout.flush()

def b64(x):
    return base64.b64encode(x).decode('ascii')

# ---------------------------------------------------------------- PNG encoders
# No numpy, no cv2, no PIL: the robot's python is not guaranteed to have any of them,
# and installing packages on it is out of scope. PNG is just zlib over filtered
# scanlines, so the standard library is enough.
def _chunk(tag, data):
    body = tag + data
    return struct.pack('>I', len(data)) + body + struct.pack('>I', zlib.crc32(body) & 0xffffffff)

def _png(w, h, bitdepth, colortype, rows):
    raw = b''.join(b'\x00' + r for r in rows)          # filter type 0 = None, per row
    ihdr = struct.pack('>IIBBBBB', w, h, bitdepth, colortype, 0, 0, 0)
    return (b'\x89PNG\r\n\x1a\n'
            + _chunk(b'IHDR', ihdr)
            + _chunk(b'IDAT', zlib.compress(raw, 6))
            + _chunk(b'IEND', b''))

def png_gray8(w, h, buf):
    return _png(w, h, 8, 0, [buf[y * w:(y + 1) * w] for y in range(h)])

def png_gray16(w, h, buf):
    a = array.array('H')
    a.frombytes(buf[:w * h * 2])
    if a.itemsize != 2:
        raise RuntimeError('array "H" is not 16 bit on this platform')
    if sys.byteorder == 'little':
        a.byteswap()                                   # PNG stores 16 bit big-endian
    b = a.tobytes()
    st = w * 2
    return _png(w, h, 16, 0, [b[y * st:(y + 1) * st] for y in range(h)])

def png_rgb8(w, h, buf, order):
    rows = []
    for y in range(h):
        o = y * w * 3
        row = bytearray(buf[o:o + w * 3])
        if order == 'bgr':
            row[0::3], row[2::3] = row[2::3], row[0::3]
        rows.append(bytes(row))
    return _png(w, h, 8, 2, rows)

# ---------------------------------------------------------------- sensor helpers
def enum_items(E):
    # Works for a plain Python Enum and for a pybind11 enum.
    out = []
    m = getattr(E, '__members__', None)
    if isinstance(m, dict):
        for k, v in m.items():
            out.append((k, v))
        return out
    for k in dir(E):
        if k.startswith('_'):
            continue
        try:
            v = getattr(E, k)
        except Exception:
            continue
        if callable(v):
            continue
        out.append((k, v))
    return out

def kind_of(name):
    n = name.upper()
    if 'INFRA' in n or 'INFRARED' in n:
        return 'ir'
    if 'DEPTH' in n:
        return 'depth'
    if 'CAMERA' in n:
        return 'rgb'
    return None                                     # LIDAR / IMU / ULTRASONIC / ...

def encode(kind, fmt, w, h, data):
    is_jpeg = len(data) > 2 and data[0] == 0xFF and data[1] == 0xD8
    if fmt in ('jpeg', 'jpg') or is_jpeg:
        return data, 'jpg'
    if kind == 'depth' and (fmt.startswith('16') or fmt in ('', 'raw')):
        need = w * h * 2
        if len(data) < need:
            raise RuntimeError('depth buffer %d B < expected %d B' % (len(data), need))
        return png_gray16(w, h, data[:need]), 'png'
    if fmt == 'mono8':
        need = w * h
        if len(data) < need:
            raise RuntimeError('mono8 buffer %d B < expected %d B' % (len(data), need))
        return png_gray8(w, h, data[:need]), 'png'
    if fmt in ('rgb8', 'rgb'):
        return png_rgb8(w, h, data[:w * h * 3], 'rgb'), 'png'
    if fmt in ('bgr8', 'bgr'):
        return png_rgb8(w, h, data[:w * h * 3], 'bgr'), 'png'
    raise RuntimeError('unsupported pixel format "%s" (w=%d h=%d)' % (fmt, w, h))

# ---------------------------------------------------------------- list mode
def run_list():
    try:
        from galbot_sdk.g1 import SensorType
    except Exception as e:
        emit('MARK_ERR import ' + repr(e))
        emit('MARK_END ok=0 fail=0')
        return 2
    n = 0
    for name, val in enum_items(SensorType):
        k = kind_of(name)
        emit('MARK_ENUM %s %s' % (name, k or '-'))
        if k:
            n += 1
    emit('MARK_END ok=%d fail=0' % n)
    return 0

# ---------------------------------------------------------------- grab mode
def run_grab(default_init=False):
    try:
        from galbot_sdk.g1 import GalbotRobot, SensorType
    except Exception as e:
        emit('MARK_ERR import ' + repr(e))
        emit('MARK_END ok=0 fail=0')
        return 2
    try:
        from galbot_sdk.g1 import RgbOutputFormat
        jpeg = RgbOutputFormat.JPEG
    except Exception:
        jpeg = None                                  # fall back to the default (JPEG)

    cams = [(n, v, kind_of(n)) for n, v in enum_items(SensorType) if kind_of(n)]
    if ONLY:
        want = set(x.upper() for x in ONLY)
        cams = [c for c in cams if c[0].upper() in want]
    for n, v, k in cams:
        emit('MARK_SENSOR %s %s' % (n, k))
    if not cams:
        emit('MARK_ERR enum no camera matched in SensorType')
        emit('MARK_END ok=0 fail=0')
        return 3

    robot = GalbotRobot()
    ok_init = False
    if default_init:
        # init(enable_sensor_set) is refused on this unit while init() with no arguments
        # succeeds. Default init takes whatever the robot already has enabled, so the
        # frames may still be readable - worth trying before touching the sensor API.
        emit('MARK_WARN -DefaultInit: using init() without an explicit sensor set')
        try:
            ok_init = bool(robot.init())
            emit('MARK_WARN init() defaults -> %r' % ok_init)
        except Exception as e:
            emit('MARK_ERR init_default ' + repr(e).replace('\n', ' '))
    else:
        try:
            ok_init = bool(robot.init(set(v for _, v, _ in cams)))
            emit('MARK_WARN init(full set) -> %r' % ok_init)
        except Exception as e:
            emit('MARK_ERR init ' + repr(e).replace('\n', ' '))
    if not ok_init:
        # No in-process retry. GalbotRobot is a singleton and only the FIRST init() in a
        # process takes effect, so a second attempt would just report the state left by
        # the first and mean nothing. Re-running the script is the only valid retry.
        emit('MARK_WARN init refused - a real retry needs a NEW process (singleton, first init wins)')
    emit('MARK_INIT ok=%s cameras=%d' % (ok_init, len(cams)))
    if not ok_init:
        emit('MARK_END ok=0 fail=%d' % len(cams))
        return 4

    time.sleep(WARM)                                 # frames are not ready immediately

    ok = 0
    fail = 0
    for name, val, kind in cams:
        try:
            if kind == 'depth':
                d = robot.get_depth_data(val)
            elif kind == 'ir':
                d = robot.get_ir_data(val)
            elif jpeg is not None:
                d = robot.get_rgb_data(val, jpeg, True)
            else:
                d = robot.get_rgb_data(val)
            if not d or 'data' not in d or not d.get('data'):
                raise RuntimeError('empty frame (not enabled / not ready / ir_enabled=false)')
            fmt = str(d.get('format', '')).lower()
            w = int(d.get('width') or 0)
            h = int(d.get('height') or 0)
            if w == 0 or h == 0:
                raise RuntimeError('frame has no width/height, format="%s"' % fmt)
            blob, ext = encode(kind, fmt, w, h, d['data'])
            emit('MARK_IMG %s %s %s %d %d %s %d %s'
                 % (name, kind, fmt, w, h, ext, len(blob), b64(blob)))
            try:
                intr = robot.get_camera_intrinsic(val)
                if intr:
                    emit('MARK_INT %s %s'
                         % (name, b64(json.dumps(str(intr)).encode('utf-8'))))
            except Exception:
                pass
            ok += 1
        except Exception as e:
            emit('MARK_ERR %s %s' % (name, str(e).replace('\n', ' ')))
            fail += 1

    emit('MARK_END ok=%d fail=%d' % (ok, fail))
    try:
        robot.request_shutdown()
        robot.wait_for_shutdown()
        robot.destroy()
        emit('MARK_WARN sdk released')
    except Exception as e:
        emit('MARK_WARN shutdown ' + repr(e).replace('\n', ' '))
    return 0

# ---------------------------------------------------------------- split mode
# WHY THIS EXISTS
#   init(sensor_set) with many sensors is refused on this unit, while a single sensor in
#   a fresh process succeeds. GalbotRobot is a singleton and only the FIRST init() of a
#   process takes effect, so a batch cannot be retried in-process. Therefore: one child
#   process per camera, orchestrated by this parent, all inside a single ssh session -
#   one password, sixteen clean init attempts, and one failing camera cannot take the
#   others down with it.
#
#   The child ships raw pixel bytes and lets the parent run encode(), so the encoders are
#   defined exactly once.
CHILD_GRAB = """\
import sys, time, json, base64
from galbot_sdk.g1 import GalbotRobot, SensorType
try:
    from galbot_sdk.g1 import RgbOutputFormat
    JPEG = RgbOutputFormat.JPEG
except Exception:
    JPEG = None
name = sys.argv[1]
kind = sys.argv[2]
warm = float(sys.argv[3])
val = getattr(SensorType, name)
r = GalbotRobot()
try:
    ok = bool(r.init(set([val])))
except Exception as e:
    print('MARK_ERR %s init raised %s: %s' % (name, type(e).__name__, str(e).replace(chr(10), ' ')[:200]), flush=True)
    sys.exit(0)
print('CHILD_INIT %s -> %r' % (name, ok), flush=True)
if not ok:
    print('MARK_ERR %s init refused in a fresh process with a single sensor' % name, flush=True)
    sys.exit(0)
time.sleep(warm)
try:
    # once=True means "hand me the latest frame, do not wait", so a stream that has not
    # produced anything yet answers with an empty dict. And requesting JPEG from a driver
    # that only offers native rgb8 can also come back empty. Try every format variant in
    # order and report which one worked, instead of concluding "no stream" from one call.
    def call(fmt, once):
        if kind == 'depth':
            return r.get_depth_data(val)
        if kind == 'ir':
            return r.get_ir_data(val)
        if fmt is None:
            return r.get_rgb_data(val)
        return r.get_rgb_data(val, fmt, once)

    variants = [('native', None, True)]
    if kind == 'rgb':
        variants = [
            ('jpeg/once', JPEG, True),
            ('jpeg/wait', JPEG, False),
            ('rgb8', getattr(RgbOutputFormat, 'RGB8', None), True),
            ('bgr8', getattr(RgbOutputFormat, 'BGR8', None), True),
            ('default-format', None, True),
        ]

    d = None
    used = 'none'
    tries = 0
    for vname, fmt, once in variants:
        if fmt is None and vname in ('jpeg/once', 'jpeg/wait', 'rgb8', 'bgr8'):
            continue                                  # that enum member does not exist here
        for _ in range(3):
            tries += 1
            try:
                d = call(fmt, once)
            except Exception:
                d = None
            if d and d.get('data'):
                used = vname
                break
            time.sleep(0.4)
        if d and d.get('data'):
            break
    print('CHILD_TRIES %s %d variant=%s' % (name, tries, used), flush=True)
    if not d or not d.get('data'):
        print('MARK_ERR %s no frame in any format variant (%d tries)' % (name, tries), flush=True)
    else:
        print('RAW_IMG %s %s %s %d %d %s' % (
            name, kind, str(d.get('format', '')).lower(),
            int(d.get('width') or 0), int(d.get('height') or 0),
            base64.b64encode(d['data']).decode('ascii')), flush=True)
        try:
            intr = r.get_camera_intrinsic(val)
            if intr:
                print('MARK_INT %s %s' % (
                    name,
                    base64.b64encode(json.dumps(str(intr)).encode('utf-8')).decode('ascii')), flush=True)
        except Exception:
            pass
except Exception as e:
    print('MARK_ERR %s %s' % (name, str(e).replace(chr(10), ' ')[:200]), flush=True)
finally:
    try:
        r.request_shutdown()
        r.wait_for_shutdown()
        r.destroy()
    except Exception:
        pass
"""

def run_split(warm):
    try:
        from galbot_sdk.g1 import SensorType
    except Exception as e:
        emit('MARK_ERR import ' + repr(e).replace('\n', ' '))
        emit('MARK_END ok=0 fail=0')
        return 2
    cams = [(n, v, kind_of(n)) for n, v in enum_items(SensorType) if kind_of(n)]
    if ONLY:
        want = set(x.upper() for x in ONLY)
        cams = [c for c in cams if c[0].upper() in want]
    if not cams:
        emit('MARK_ERR enum no camera matched in SensorType')
        emit('MARK_END ok=0 fail=0')
        return 3
    for n, _, k in cams:
        emit('MARK_SENSOR %s %s' % (n, k))

    import subprocess
    child_warm = min(float(warm), 3.0)
    ok = 0
    fail = 0
    for n, _, k in cams:
        emit('MARK_WARN --- %s (fresh process)' % n)
        try:
            p = subprocess.Popen(
                [sys.executable, '-B', '-c', CHILD_GRAB, n, k, str(child_warm)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            for bline in p.stdout:
                line = bline.decode('utf-8', 'replace').rstrip('\r\n')
                if not line.strip():
                    continue
                if line.startswith('RAW_IMG '):
                    parts = line[len('RAW_IMG '):].split(' ', 5)
                    if len(parts) != 6:
                        emit('MARK_ERR %s malformed raw header' % n)
                        fail += 1
                        continue
                    cname, ckind, cfmt, cw, ch, cb64 = parts
                    try:
                        data = base64.b64decode(cb64)
                        blob, ext = encode(ckind, cfmt, int(cw), int(ch), data)
                        emit('MARK_IMG %s %s %s %d %d %s %d %s'
                             % (cname, ckind, cfmt, int(cw), int(ch), ext, len(blob), b64(blob)))
                        ok += 1
                    except Exception as e:
                        emit('MARK_ERR %s %s' % (cname, str(e).replace('\n', ' ')[:200]))
                        fail += 1
                elif line.startswith('MARK_ERR '):
                    emit(line)
                    fail += 1
                else:
                    emit(line)
            p.wait()
        except Exception as e:
            emit('MARK_ERR %s orchestrator %s' % (n, str(e).replace('\n', ' ')[:200]))
            fail += 1
    emit('MARK_END ok=%d fail=%d' % (ok, fail))
    return 0

# ---------------------------------------------------------------- diag
# init() returns a bool and does NOT raise when it refuses, so a bare "ok=False" is
# useless for diagnosing. This mode isolates three variables - default init, a single
# camera, the full set - and rebuilds the client between attempts, so a poisoned
# session cannot mask the real cause. It also dumps the client's public API surface,
# which is the only way to find out what introspection the SDK actually offers.
def run_diag():
    try:
        import galbot_sdk
        from galbot_sdk.g1 import GalbotRobot, SensorType
    except Exception as e:
        emit('MARK_ERR import ' + repr(e).replace('\n', ' '))
        emit('MARK_END ok=0 fail=0')
        return 2

    emit('MARK_WARN sdk file=%s' % getattr(galbot_sdk, '__file__', '?'))
    emit('MARK_WARN sdk version=%s' % getattr(galbot_sdk, '__version__', 'n/a'))

    cams = [(n, v, kind_of(n)) for n, v in enum_items(SensorType) if kind_of(n)]
    head = None
    for n, v, k in cams:
        if n.upper() == 'HEAD_LEFT_CAMERA':
            head = (n, v, k)
            break
    if head is None and cams:
        head = cams[0]

    # ------------------------------------------------------------ introspection
    # No init() here: the client is a SINGLETON and the docs state that repeated init()
    # calls without destroy() do not error but ONLY THE FIRST ONE TAKES EFFECT. So every
    # init probe below runs in its own child process; a single process would make probe
    # N a silent no-op reporting the state left by probe N-1.
    emit('MARK_WARN --- init signature introspection')
    r0 = None
    try:
        r0 = GalbotRobot()
        fn = getattr(r0, 'init', None)
        doc = ' '.join((getattr(fn, '__doc__', None) or '(none)').split())
        emit('MARK_WARN init doc: %s' % doc[:400])
        try:
            import inspect
            emit('MARK_WARN init signature: %s' % str(inspect.signature(fn)))
        except Exception as e:
            emit('MARK_WARN signature unavailable: %s' % str(e).replace('\n', ' ')[:150])
        if head is not None:
            emit('MARK_WARN sensor value=%r type=%s'
                 % (head[1], type(head[1]).__name__))
    except Exception as e:
        emit('MARK_WARN introspection raised %s: %s'
             % (type(e).__name__, str(e).replace('\n', ' ')[:150]))
    finally:
        if r0 is not None:
            try:
                r0.destroy()
            except Exception:
                pass

    # ------------------------------------------------------------ per-process probes
    import subprocess

    CHILD = """\
import sys
from galbot_sdk.g1 import GalbotRobot, SensorType
mode = sys.argv[1]
names = [x for x in (sys.argv[2] if len(sys.argv) > 2 else '').split(',') if x]
try:
    r = GalbotRobot()
except Exception as e:
    print('CLIENT_RAISED %s: %s' % (type(e).__name__, str(e).replace(chr(10), ' ')[:300]))
    sys.exit(0)
try:
    if mode == 'default':
        res = r.init()
    elif mode == 'kw':
        res = r.init(enable_sensor_set=set(getattr(SensorType, n) for n in names))
    elif mode == 'list':
        res = r.init([getattr(SensorType, n) for n in names])
    else:
        res = r.init(set(getattr(SensorType, n) for n in names))
    print('INIT_RESULT %r' % res)
except Exception as e:
    print('INIT_RAISED %s: %s' % (type(e).__name__, str(e).replace(chr(10), ' ')[:300]))
finally:
    try:
        r.destroy()
    except Exception:
        pass
"""

    def probe(label, mode, names):
        emit('MARK_WARN --- %s [fresh process]' % label)
        try:
            p = subprocess.run([sys.executable, '-B', '-c', CHILD, mode, ','.join(names)],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=150)
            out = p.stdout.decode('utf-8', 'replace').strip()
            for line in (out.splitlines() or ['(no output)'])[-8:]:
                emit('MARK_WARN   ' + line)
        except Exception as e:
            emit('MARK_WARN   probe failed: %s' % str(e).replace('\n', ' ')[:200])

    probes = [('A1 init() no args', 'default', [])]
    if head is not None:
        probes.append(('A2 init({%s})' % head[0], 'set', [head[0]]))
    probes.append(('A3 init(all %d cameras)' % len(cams), 'set', [n for n, _, _ in cams]))
    if head is not None:
        probes.append(('B1 kw enable_sensor_set={%s}' % head[0], 'kw', [head[0]]))
        probes.append(('B2 official pair LEFT_ARM_CAMERA + LEFT_ARM_DEPTH_CAMERA',
                       'set', ['LEFT_ARM_CAMERA', 'LEFT_ARM_DEPTH_CAMERA']))

    probed = 0
    for label, mode, names in probes:
        probe(label, mode, names)
        probed += 1
        time.sleep(1)

    # ------------------------------------------------ phase C: who owns the cameras
    # Read-only shell probes. If a perception/navigation process already owns the camera
    # service, that explains "session accepted, sensor set refused" without any bug in
    # this script - and no amount of retrying here would change it.
    emit('MARK_WARN === phase C: robot processes ===')
    import subprocess

    def sh(cmd):
        try:
            out = subprocess.check_output(cmd, shell=True, stderr=subprocess.STDOUT, timeout=10)
            txt = out.decode('utf-8', 'replace').strip()
            return ' | '.join(txt.splitlines())[:600] or '(empty)'
        except Exception as e:
            return 'err: %s' % str(e).replace('\n', ' ')[:200]

    emit('MARK_WARN ps: ' + sh("ps -eo pid,user,etimes,comm --sort=-etimes | grep -i -E 'galbot|percept|camera|media|ros' | grep -v grep | head -12"))
    emit('MARK_WARN who: ' + sh("who 2>/dev/null | head -6"))
    emit('MARK_WARN uptime: ' + sh("uptime"))
    emit('MARK_WARN listen: ' + sh("ss -ltn 2>/dev/null | grep -v -E '127.0.0.1|::1' | head -12"))

    emit('MARK_END ok=%d fail=0' % probed)
    return 0

# ---------------------------------------------------------------- selftest
def run_selftest():
    w, h = 160, 120
    g8 = bytes(((x * 255) // (w - 1)) for y in range(h) for x in range(w))
    a16 = array.array('H', [((x * 65535) // (w - 1)) for y in range(h) for x in range(w)])
    if sys.byteorder == 'big':
        a16.byteswap()                               # hand png_gray16 little-endian input
    rgb = bytearray()
    for y in range(h):
        for x in range(w):
            rgb += bytes(((x * 255) // (w - 1), (y * 255) // (h - 1), 128))
    items = [
        ('selftest_gray8',  png_gray8(w, h, g8)),
        ('selftest_gray16', png_gray16(w, h, a16.tobytes())),
        ('selftest_rgb8',   png_rgb8(w, h, bytes(rgb), 'rgb')),
    ]
    for n, b in items:
        emit('MARK_IMG %s selftest png %d %d png %d %s' % (n, w, h, len(b), b64(b)))
    emit('MARK_END ok=%d fail=0' % len(items))
    return 0

if os.environ.get('CAM_SELFTEST') == '1':
    sys.exit(run_selftest())
if MODE == 'list':
    sys.exit(run_list())
if MODE == 'diag':
    sys.exit(run_diag())
if MODE == 'grabsafe':
    sys.exit(run_grab(default_init=True))
if MODE == 'split':
    sys.exit(run_split(WARM))
sys.exit(run_grab())
'@

$mode = if ($List) { 'list' } elseif ($Diag) { 'diag' } elseif ($Split) { 'split' } elseif ($DefaultInit) { 'grabsafe' } else { 'grab' }
$onlyJson = '[]'
# [string[]] plus a whitespace-or-comma split, because PowerShell turns an unquoted
# "-Only A,B" into a two-element array; binding that to [string] would join it with a
# space and no camera would match.
$onlyTxt = (@($Only) -join ',')
if ($onlyTxt.Trim()) {
    $parts = @($onlyTxt -split '[,\s]+' | ForEach-Object { $_.Trim().ToUpper() } | Where-Object { $_ })
    $onlyJson = '[' + (($parts | ForEach-Object { "'" + $_ + "'" }) -join ', ') + ']'
}
$py = $pyTemplate.Replace('__MODE__', $mode).Replace('__ONLY__', $onlyJson).Replace('__WARM__', $WarmupSec).Replace('__LIBDIR__', $LibDir)

$pyB64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($py))
# The snippet must contain no double quote, so the remote shell can be quoted exactly once.
$code = "import base64;exec(base64.b64decode('$pyB64'))"

# ---------------------------------------------------------------- output directory
$stamp = (Get-Date).ToString('yyyyMMdd-HHmmss')
if (-not $OutDir) {
    $root = Split-Path -Parent $MyInvocation.MyCommand.Path
    if (-not $root) { $root = (Get-Location).Path }
    $OutDir = Join-Path $root ("cams\" + $stamp)
}
if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }

# ---------------------------------------------------------------- run
# The SDK needs PYTHONPATH/LD_LIBRARY_PATH because galbot_sdk is not installed, only
# shipped in $LibDir. -B keeps the robot free of __pycache__ writes.
$remoteCmd = "env PYTHONPATH=$LibDir LD_LIBRARY_PATH=$LibDir python3 -B -c `"$code`""

$exe = 'ssh.exe'
if ($Selftest) {
    # Truly local: the same python program, the same MARK protocol, the same parser and
    # file writer - only the transport changes. Proves the encoders before a robot is in
    # the loop, and re-proves them later on the robot's own python.
    Step 1 "Encoder selftest (local python, no robot)"
    $exe = 'python'
    $env:CAM_SELFTEST = '1'
    $argStr = '-B -c "' + ($code.Replace('"', '\"')) + '"'
} else {
    Step 1 ("{0} on {1}" -f $mode.ToUpper(), $RobotIp)
    # ssh receives one argv per element here: a pre-joined string would be parsed as options.
    $sshArgList = @('-T', '-o', 'ConnectTimeout=10')
    if ($KeyFile) { $sshArgList += @('-i', $KeyFile) }
    $sshArgList += @("$User@$RobotIp", $remoteCmd)

    Write-Host ("     argv: ssh -T {0}{1}@{2} ""<remote program, {3:N0} chars>""" -f
        $(if ($KeyFile) { "-i $KeyFile " } else { '' }), $User, $RobotIp, $code.Length) -ForegroundColor DarkGray
    if (-not $KeyFile) {
        Write-Host "     Enter the robot password when prompted (it will not echo)." -ForegroundColor Yellow
    }

    $argStr = "-T -o ConnectTimeout=10"
    if ($KeyFile) { $argStr += ' -i "' + $KeyFile + '"' }
    $argStr += ' ' + $User + '@' + $RobotIp
    $argStr += ' "' + ($remoteCmd.Replace('"', '\"')) + '"'
}

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $exe
$psi.Arguments = $argStr
$psi.UseShellExecute = $false
$psi.RedirectStandardInput = $false    # leave stdin alone: the password prompt needs it
$psi.RedirectStandardOutput = $true    # frames arrive here as base64 lines
$psi.RedirectStandardError = $false    # and stderr stays visible for the same reason
$psi.CreateNoWindow = $false

$proc = [System.Diagnostics.Process]::Start($psi)
$sr = $proc.StandardOutput

$results = New-Object System.Collections.ArrayList
$errs = New-Object System.Collections.ArrayList
$sensors = New-Object System.Collections.ArrayList
$endLine = ''
$sawAny = $false
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$timedOut = $false

# EXACTLY ONE outstanding ReadLineAsync at a time.
# StreamReader allows a single pending async read per stream. Calling ReadLineAsync()
# again while the previous task is still unread throws InvalidOperationException
# "The stream is currently in use by a previous operation on the stream"
# (zh: "流正在由其上的前一操作使用"). The old code did exactly that: when Wait(1000)
# expired it abandoned the task and started a second read on the same reader. Any pause
# longer than one second - typing the ssh password, or the SDK warmup - killed the run.
$pending = $null
while ($true) {
    if ($sw.Elapsed.TotalSeconds -gt $TimeoutSec) { $timedOut = $true; break }
    if ($null -eq $pending) { $pending = $sr.ReadLineAsync() }
    if (-not $pending.Wait(200)) { continue }          # poll the SAME task, never a new one
    if ($pending.IsFaulted) {
        Write-Host ("  stream error: " + $pending.Exception.GetBaseException().Message) -ForegroundColor Red
        break
    }
    $line = $pending.Result
    $pending = $null
    if ($null -eq $line) { break }

    if ($line -match '^MARK_IMG (\S+) (\S+) (\S+) (\d+) (\d+) (\w+) (\d+) (\S+)\s*$') {
        $sawAny = $true
        $name = $Matches[1]; $kind = $Matches[2]; $fmt = $Matches[3]
        $w = [int]$Matches[4]; $h = [int]$Matches[5]; $ext = $Matches[6]
        $decl = [int]$Matches[7]
        try {
            $bytes = [Convert]::FromBase64String($Matches[8])
        } catch {
            Write-Host ("  BAD  {0}: base64 decode failed ({1} chars)" -f $name, $Matches[8].Length) -ForegroundColor Red
            $null = $errs.Add("$name base64 decode failed")
            continue
        }
        if ($bytes.Length -ne $decl) {
            Write-Host ("  WARN {0}: declared {1} B, decoded {2} B" -f $name, $decl, $bytes.Length) -ForegroundColor Yellow
        }
        $file = Join-Path $OutDir ($name + '.' + $ext)
        [System.IO.File]::WriteAllBytes($file, $bytes)
        Write-Host ("  OK   {0,-30} {1,-6} {2,-7} {3}x{4,-5} {5,8:N0} B  -> {6}" -f
            $name, $kind, $fmt, $w, $h, $bytes.Length, (Split-Path -Leaf $file)) -ForegroundColor Green
        $null = $results.Add([pscustomobject]@{
            camera = $name; kind = $kind; format = $fmt; width = $w; height = $h
            bytes = $bytes.Length; file = (Split-Path -Leaf $file)
        })
        continue
    }
    if ($line -match '^MARK_INT (\S+) (\S+)\s*$') {
        $intrTxt = ''
        try { $intrTxt = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Matches[2])) } catch { }
        Write-Host ("       intrinsic {0}: {1}" -f $Matches[1], $intrTxt) -ForegroundColor DarkGray
        continue
    }
    if ($line -match '^MARK_SENSOR (\S+) (\S+)\s*$') {
        $null = $sensors.Add([pscustomobject]@{ camera = $Matches[1]; kind = $Matches[2] })
        Write-Host ("       sensor   {0} ({1})" -f $Matches[1], $Matches[2]) -ForegroundColor DarkGray
        continue
    }
    if ($line -match '^MARK_ENUM (\S+) (\S+)\s*$') {
        $sawAny = $true
        if ($Matches[2] -eq '-') {
            Write-Host ("       {0,-34} non-camera sensor, skipped" -f $Matches[1]) -ForegroundColor DarkGray
        } else {
            Write-Host ("       {0,-34} {1}" -f $Matches[1], $Matches[2]) -ForegroundColor Gray
        }
        continue
    }
    if ($line -match '^MARK_ERR (\S*) ?(.*)$') {
        $sawAny = $true
        Write-Host ("  FAIL {0} {1}" -f $Matches[1], $Matches[2]) -ForegroundColor Red
        $null = $errs.Add(($Matches[1] + ' ' + $Matches[2]).Trim())
        continue
    }
    if ($line -match '^MARK_WARN (.*)$') {
        $sawAny = $true
        Write-Host ("  note {0}" -f $Matches[1]) -ForegroundColor DarkYellow
        continue
    }
    if ($line -match '^MARK_INIT (.*)$') {
        $sawAny = $true
        Write-Host ("  init: {0}" -f $Matches[1]) -ForegroundColor Cyan
        continue
    }
    if ($line -match '^MARK_END (.*)$') {
        $sawAny = $true
        $endLine = $Matches[1]
        continue
    }
    if ($line.Trim()) { Write-Host ("       | " + $line) -ForegroundColor DarkGray }   # tracebacks
}

# Never leave the child behind: a surviving ssh keeps the console and re-prints its
# "password:" prompt after this script has already died, which is a confusing dead end.
if (-not $proc.WaitForExit(2000)) { try { $proc.Kill(); $null = $proc.WaitForExit(2000) } catch { } }
$sshExit = $proc.ExitCode
try { $sr.Dispose() } catch { }

# ---------------------------------------------------------------- summary
Write-Host ""
if ($timedOut) {
    Write-Host ("TIMEOUT after {0}s. The process was killed; partial results are still saved." -f $TimeoutSec) -ForegroundColor Red
}
Write-Host ("{0} exit code: {1}" -f $(if ($Selftest) { 'python' } else { 'ssh' }), $sshExit)
if ($endLine) { Write-Host ("remote summary: " + $endLine) }

if ($List -and $endLine) {
    Write-Host ""
    Write-Host "Listed every camera sensor this robot reports. Nothing was captured (-List)." -ForegroundColor Green
    Write-Host "Next: .\robot-cam.ps1 -Grab"
    exit 0
}

if ($Diag) {
    Write-Host ""
    Write-Host "Diagnostic finished. Each probe ran in its OWN python process" -ForegroundColor Cyan
    Write-Host "(the client is a singleton: only the first init() per process counts)." -ForegroundColor Cyan
    Write-Host "  A1 True, A2/A3 False  -> only the default set is accepted; the sensor-set API is refused"
    Write-Host "  A2 True               -> a single camera works: use -Only <camera> and grab in batches"
    Write-Host "  A3 True               -> the full set works now; the earlier refusal was transient"
    Write-Host "  A1 False              -> the robot refuses every init right now (session or mode issue)"
    Write-Host "  phase C 'ps:' line    -> shows whether a perception/navigation process owns the cameras"
    exit 0
}

if ($results.Count -gt 0) {
    $index = [pscustomobject]@{
        robot = $RobotIp; time = $stamp; sshExit = $sshExit
        discovered = @($sensors); captured = @($results); errors = @($errs)
    }
    $indexPath = Join-Path $OutDir 'index.json'
    ($index | ConvertTo-Json -Depth 5) | Set-Content -Path $indexPath -Encoding UTF8
    Write-Host ""
    Write-Host ("Captured {0} image(s) into {1}" -f $results.Count, $OutDir) -ForegroundColor Green
    Write-Host ("Index: " + $indexPath)
    exit 0
}

Write-Host ""
if (-not $sawAny) {
    Write-Host "Nothing came back at all. That is UNKNOWN, not success." -ForegroundColor Red
    Write-Host "  No password prompt appeared -> add -KeyFile after installing the public key." -ForegroundColor Red
    Write-Host "  Password prompt appeared and failed -> wrong password, ssh exit $sshExit." -ForegroundColor Red
} elseif ($endLine) {
    Write-Host "The remote program ran but produced no image. Read the FAIL lines above:" -ForegroundColor Yellow
    Write-Host "  'empty frame'        -> in order of likelihood: ANOTHER process holds that stream," -ForegroundColor Yellow
    Write-Host "                          the camera is disabled in the robot config, it is not fitted," -ForegroundColor Yellow
    Write-Host "                          or ir_enabled=false. Confirmed on 2026-11-07: a teammate's" -ForegroundColor Yellow
    Write-Host "                          process held LEFT/RIGHT_ARM_CAMERA, and init() STILL returned" -ForegroundColor Yellow
    Write-Host "                          True - an empty frame does NOT prove init succeeded." -ForegroundColor Yellow
    Write-Host "  'import ...'         -> wrong PYTHONPATH; rerun with -LibDir <dir with galbot_sdk>" -ForegroundColor Yellow
    Write-Host "  'init ...'           -> SDK refused: someone else holds the session, tell the lead" -ForegroundColor Yellow
} else {
    Write-Host "The remote program died before its final line; see the | lines above." -ForegroundColor Yellow
}
exit 1
