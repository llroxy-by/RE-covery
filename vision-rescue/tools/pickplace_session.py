"""Local persistent grasp session. Explicit --execute enables motion.

JSONL actions: move, capture, batch, call, quit. A batch stops at the first
failed step; every move still gets a fresh one-use plan and collision check.
No source files are written to the robot.
"""
import argparse
import base64
import json
import time
from pathlib import Path
import sys
from exec_transport import connect, call, close

PROFILES = {
    'precise': {'transport': .06, 'approach': .03, 'gripper': .04},
    'balanced': {'transport': .08, 'approach': .04, 'gripper': .05},
    'fast': {'transport': .12, 'approach': .06, 'gripper': .07},
}


class Session:
    def __init__(self, proc, profile='balanced'):
        self.proc = proc
        self.log = []
        self.profile = PROFILES[profile]

    def request(self, req):
        started = time.time()
        try:
            wall_ms, result = call(self.proc, req)
        except Exception as exc:
            self.log.append({'started_at': started, 'request': req,
                             'error': type(exc).__name__ + ': ' + str(exc),
                             'rpc_ms': (time.time() - started) * 1000})
            raise
        self.log.append({'started_at': started, 'rpc_ms': wall_ms, 'request': req,
                         'response': {k: v for k, v in result.items() if k != 'b64'}})
        return result

    def perform(self, obj):
        obj = dict(obj)
        action = obj.pop('action', 'call')
        if action == 'batch':
            results = []
            timing = []
            batch_start = time.monotonic()
            previous_end = batch_start
            for step in obj['steps']:
                start = time.monotonic()
                result = self.perform(step)
                end = time.monotonic()
                timing.append({'action': step.get('action', step.get('cmd')),
                               'duration_ms': round((end - start) * 1000, 2),
                               'gap_ms': round((start - previous_end) * 1000, 2)})
                previous_end = end
                results.append(result)
                print('STEP', json.dumps(brief(result), ensure_ascii=False), flush=True)
                if not result.get('ok'):
                    return {'ok': False, 'error': '本批次已停止，后续步骤未执行', 'results': results, 'timing': timing}
            return {'ok': True, 'results': results, 'timing': timing,
                    'duration_ms': round((time.monotonic() - batch_start) * 1000, 2)}
        if action == 'capture':
            state = self.request({'cmd': 'state'})
            head = self.request({'cmd': 'headjpeg'})
            tag = obj.get('tag', 'scene')
            if not tag.replace('-', '').replace('_', '').isalnum():
                raise ValueError('快照名称无效')
            if head.get('b64'):
                Path('/tmp/' + tag + '.jpg').write_bytes(base64.b64decode(head.pop('b64')))
            result = {'ok': bool(state.get('ok') and head.get('ok')), 'state': state, 'head': head}
            if obj.get('probe'):
                result['probe'] = self.request({'cmd': 'probe', 'radius': obj.get('radius', .18), 'frames': 4, 'peaks': 12})
                result['ok'] = bool(result['ok'] and result['probe'].get('ok'))
            Path('/tmp/' + tag + '.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
            if 'probe' in result:
                result['probe'].pop('grid', None)
            return result
        if action == 'expect_grip':
            state = self.request({'cmd': 'state'})
            grip = state.get('grip', {})
            valid = state.get('ok') and not grip.get('moving', True) and obj['min'] <= grip.get('width', -1) <= obj['max']
            return {'ok': bool(valid), 'grip': grip}
        if action == 'move':
            x, y, z = obj['xyz']
            phase = obj.get('phase', 'approach')
            if phase not in ('transport', 'approach'):
                return {'ok': False, 'error': '未知运动阶段'}
            result = self.request({'cmd': 'move_checked', 'x': x, 'y': y, 'z': z,
                                   'steps': 40, 'min_tip': obj['min_tip'],
                                   'max_delta': obj.get('max_delta', 40), 'max_dev': obj.get('max_dev', 5),
                                   'speed': obj.get('speed', self.profile[phase]), 'table_z': obj['table_z']})
            if result.get('err_mm') is None or result['err_mm'] > 3:
                result['ok'] = False
            return result
        if obj.get('cmd') == 'grip':
            obj.setdefault('wait_until_stopped', True)
            obj.setdefault('v', self.profile['gripper'])
        return self.request(obj)


def brief(result):
    result = dict(result)
    result.pop('joints', None)
    if result.get('ok'):
        result.pop('plan', None)
    if 'state' in result:
        result['state'] = brief(result['state'])
    if 'head' in result:
        result['head'] = {'ok': result['head'].get('ok')}
    if 'results' in result:
        steps = result.pop('results')
        result['completed'] = sum(bool(x.get('ok')) for x in steps)
        result['last'] = brief(steps[-1]) if steps else None
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--profile', choices=PROFILES, default='balanced')
    parser.add_argument('--verbose', action='store_true')
    parser.add_argument('--plan', type=Path, help='Read a complete JSON plan from a local file, avoiding terminal line limits')
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text()) if args.plan else None
    proc, boot = connect(args.execute)
    session = Session(proc, args.profile)
    print('CONNECTED', boot, flush=True)
    try:
        if plan is not None:
            result = session.perform(plan)
            print('RESULT', json.dumps(result if args.verbose else brief(result), ensure_ascii=False), flush=True)
            Path('/tmp/pickplace-batch-log.json').write_text(json.dumps(session.log, ensure_ascii=False, indent=2))
            return 0 if result.get('ok') else 1
        for line in sys.stdin:
            try:
                obj = json.loads(line)
                if obj.get('action') == 'quit':
                    break
                result = session.perform(obj)
                print('RESULT', json.dumps(result if args.verbose else brief(result), ensure_ascii=False), flush=True)
            except Exception as exc:
                print('ERROR', type(exc).__name__, str(exc), flush=True)
            Path('/tmp/pickplace-batch-log.json').write_text(json.dumps(session.log, ensure_ascii=False, indent=2))
    finally:
        try:
            Path('/tmp/pickplace-batch-log.json').write_text(json.dumps(session.log, ensure_ascii=False, indent=2))
        finally:
            close(proc)


if __name__ == '__main__':
    sys.exit(main())
