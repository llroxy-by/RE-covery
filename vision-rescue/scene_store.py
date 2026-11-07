"""Shared immutable scene snapshot; no robot commands or model calls."""
import json, os, tempfile, time, uuid
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from pathlib import Path

def location():
    return Path(os.environ.get('ROBOT_SCENE_FILE', '/tmp/galbot-scene.json'))

def publish(result, captured_at, source='robot_camera'):
    if not result.get('ok') or not isinstance(result.get('tracks', result.get('objects')), list):
        raise ValueError('Invalid detection result')
    if not 0 <= time.time() - captured_at < 60:
        raise ValueError('Invalid capture time')
    data = dict(result, schema_version=1, scene_id=uuid.uuid4().hex,
                captured_at=captured_at, published_at=time.time(), source=source)
    if os.environ.get('ROBOT_SCENE_URL'):
        return remote('POST', data)
    return save(data)

def save(data):
    target = location(); target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=target.parent, prefix='.scene-')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, allow_nan=False)
        os.replace(name, target)
    finally:
        if os.path.exists(name): os.unlink(name)
    return data

def read(expected_scene_id=None, max_age=5):
    if os.environ.get('ROBOT_SCENE_URL'):
        try:
            return remote('GET', query={'scene_id': expected_scene_id} if expected_scene_id else {})
        except Exception:
            return {'ok': False, 'stale': True, 'error': '远程识图服务不可用'}
    try:
        data = json.loads(location().read_text())
    except (OSError, ValueError):
        return {'ok': False, 'stale': True, 'error': '尚无可用识图数据'}
    age = time.time() - data['captured_at']
    stale = not 0 <= age <= max_age
    match = expected_scene_id is None or expected_scene_id == data['scene_id']
    return dict(data, ok=bool(data.get('ok') and not stale and match), stale=stale,
                age_ms=round(age * 1000), version_matches=match)


def remote(method, data=None, query=None):
    url = os.environ['ROBOT_SCENE_URL'].rstrip('/')
    if query: url += '?' + urlencode(query)
    headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + os.environ.get('ROBOT_SCENE_TOKEN', '')}
    request = Request(url, data=json.dumps(data, allow_nan=False).encode() if data is not None else None, headers=headers, method=method)
    with urlopen(request, timeout=4) as response:
        return json.load(response)
