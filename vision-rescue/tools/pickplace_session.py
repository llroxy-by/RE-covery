"""Local persistent grasp session. Explicit --execute enables motion.

JSONL actions: move, capture, batch, call, quit. A batch stops at the first
failed step; every move still gets a fresh one-use plan and collision check.
No source files are written to the robot.
"""
import argparse
import base64
import json
from pathlib import Path
import sys
from exec_transport import connect, call, close


class Session:
    def __init__(self, proc):
        self.proc = proc
        self.log = []

    def request(self, req):
        _, result = call(self.proc, req)
        self.log.append({'request': req, 'response': {k: v for k, v in result.items() if k != 'b64'}})
        return result

    def perform(self, obj):
        obj = dict(obj)
        action = obj.pop('action', 'call')
        if action == 'batch':
            results = []
            for step in obj['steps']:
                result = self.perform(step)
                results.append(result)
                print('STEP', json.dumps(brief(result), ensure_ascii=False), flush=True)
                if not result.get('ok'):
                    return {'ok': False, 'error': '本批次已停止，后续步骤未执行', 'results': results}
            return {'ok': True, 'results': results}
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
            plan = self.request({'cmd': 'ikpath', 'x': x, 'y': y, 'z': z, 'steps': 40})
            if (not plan.get('ok') or plan['max_delta_deg'] > obj.get('max_delta', 40)
                    or plan['max_dev_cm'] > obj.get('max_dev', 5)
                    or plan['min_tip_z'] < obj['min_tip']):
                return {'ok': False, 'error': '路径不通过', 'plan': plan}
            result = self.request({'cmd': 'execute_plan', 'plan_id': plan['plan_id'],
                                   'speed': obj.get('speed', .05), 'table_z': obj['table_z']})
            result['plan'] = plan
            if result.get('err_mm') is None or result['err_mm'] > 3:
                result['ok'] = False
            return result
        return self.request(obj)


def brief(result):
    result = dict(result)
    result.pop('joints', None)
    if 'head' in result:
        result['head'] = {'ok': result['head'].get('ok')}
    if 'results' in result:
        result['results'] = [brief(x) for x in result['results']]
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    proc, boot = connect(args.execute)
    session = Session(proc)
    print('CONNECTED', boot, flush=True)
    try:
        for line in sys.stdin:
            try:
                obj = json.loads(line)
                if obj.get('action') == 'quit':
                    break
                result = session.perform(obj)
                print('RESULT', json.dumps(brief(result), ensure_ascii=False), flush=True)
            except Exception as exc:
                print('ERROR', type(exc).__name__, str(exc), flush=True)
            Path('/tmp/pickplace-batch-log.json').write_text(json.dumps(session.log, ensure_ascii=False, indent=2))
    finally:
        close(proc)


if __name__ == '__main__':
    main()
