"""Offline regressions: these tests never connect to a robot."""
import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import time
import types
import unittest
from unittest.mock import patch

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sdk = types.ModuleType('galbot_sdk')
sdk.SensorType = types.SimpleNamespace()
sdk.MotionStatus = types.SimpleNamespace(SUCCESS='SUCCESS')
sdk.ControlStatus = types.SimpleNamespace(SUCCESS='SUCCESS')
gm = types.ModuleType('galbot_sdk.g1')
gm.RobotStates = type('RobotStates', (), {})
gm.Parameter = type('Parameter', (), {'set_timeout': lambda self, t: None})


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


with patch.dict(sys.modules, {'galbot_sdk': sdk, 'galbot_sdk.g1': gm}):
    executor = load('grasp_executor_test', HERE.parent / 'robot_exec.py')
lowprobe = load('lowprobe_test', HERE / 'lowprobe_grasp.py')


def joints():
    return {'leg': [0.] * 5, 'head': [0.] * 2,
            'left_arm': [0.] * 7, 'right_arm': [0.] * 7}


class Motion:
    def __init__(self):
        self.js = joints()
        self.ik_calls = 0
        self.ik_status = 'SUCCESS'
        self.collision_status = 'SUCCESS'

    def get_chain_joint_state(self):
        return self.js

    def inverse_kinematics(self, *args):
        self.ik_calls += 1
        return self.ik_status, {'left_arm': [.01] * 7}

    def forward_kinematics(self, link, frame, js):
        assert set(js) == set(joints())
        return 'SUCCESS', [.7, .1, 1.] + executor.R2q(executor.tool_orientation(0.))

    def check_collision_detail(self, *args):
        return self.collision_status, []


class SafetyTests(unittest.TestCase):
    def test_rectified_image_is_not_treated_as_raw_fisheye(self):
        info = {'K': [400., 0., 640., 0., 400., 480., 0., 0., 1.],
                'D': [0.] * 5, 'distortion_model': 'plumb_bob',
                'camera_type': 'fisheye'}
        np.testing.assert_allclose(executor.camera_ray(640., 720., info), [0., .6, 1.])

    def test_unknown_camera_model_rejected(self):
        with self.assertRaises(ValueError):
            executor.camera_ray(640., 720., {'K': [400.,0.,640.,0.,400.,480.,0.,0.,1.]})

    def setUp(self):
        self.ex = executor.Exec.__new__(executor.Exec)
        self.ex.m = Motion()
        self.ex._plans = {}
        self.sent = []
        self.ex.r = types.SimpleNamespace(set_joint_positions=self.move)
        self.ex.state = lambda: {'ok': True, 'mount': [.7, .1, 1.]}

    def move(self, q, **kwargs):
        self.sent.append(q)
        return 'SUCCESS'

    def plan(self):
        return self.ex.cmd_ikpath({'x': .7, 'y': .1, 'z': 1.})

    def execute(self, p):
        with patch.dict(sys.modules, {'galbot_sdk': sdk, 'galbot_sdk.g1': gm}):
            return self.ex.cmd_execute_plan({'plan_id': p['plan_id'], 'table_z': .7, 'speed': .05})

    def test_fault_with_nonempty_ik_is_rejected(self):
        self.ex.m.ik_status = 'FAULT'
        self.assertFalse(self.plan()['ok'])
        self.assertFalse(self.sent)

    def test_fk_uses_full_body_and_tip_offset(self):
        self.assertEqual(self.plan()['min_tip_z'], .811)

    def test_execution_uses_same_ik_once_and_plan_cannot_replay(self):
        p = self.plan()
        self.assertTrue(self.execute(p)['ok'])
        self.assertEqual(self.sent, [[.01] * 7])
        self.assertEqual(self.ex.m.ik_calls, 1)
        self.assertFalse(self.execute(p)['ok'])

    def test_changed_other_arm_rejects_plan(self):
        p = self.plan()
        # Return fresh feedback rather than mutate the snapshot itself.
        self.ex.m.js = joints()
        self.ex.m.js['right_arm'][0] = .1
        self.assertFalse(self.execute(p)['ok'])
        self.assertFalse(self.sent)

    def test_collision_service_fault_never_moves(self):
        p = self.plan()
        self.ex.m.collision_status = 'FAULT'
        self.assertFalse(self.execute(p)['ok'])
        self.assertFalse(self.sent)

    def test_expired_plan_never_moves(self):
        p = self.plan()
        self.ex._plans[p['plan_id']]['created'] = time.monotonic() - 20
        self.assertFalse(self.execute(p)['ok'])
        self.assertFalse(self.sent)

    def test_dry_does_not_move_or_open_closed_gripper(self):
        commands = []
        def call(p, req):
            commands.append(req['cmd'])
            return 0, {'ok': True, 'mount': [.7, .1, 1.], 'grip': {'width': 0.}}
        with patch.object(sys, 'argv', ['lowprobe', '--dry']), \
             patch.object(lowprobe, 'connect', return_value=(None, {'depth_cam': True})), \
             patch.object(lowprobe, 'call', side_effect=call), \
             patch.object(lowprobe, 'probe', return_value={'ok': True}), \
             patch('atexit.register'), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(lowprobe.main(), 0)
        self.assertEqual(commands, ['state'])

    def test_failed_execution_not_retried(self):
        commands = []
        def call(p, req):
            commands.append(req['cmd'])
            if req['cmd'] == 'ikpath':
                return 0, {'ok': True, 'plan_id': 'one', 'max_delta_deg': 1.,
                           'min_tip_z': 1., 'max_dev_cm': 0.}
            return 0, {'ok': False, 'status': 'FAULT'}
        with patch.object(lowprobe, 'call', side_effect=call):
            self.assertFalse(lowprobe.move_cart(None, .7, .1, 1.)[0])
        self.assertEqual(commands, ['ikpath', 'execute_plan'])

    def test_fault_preflight_does_not_open_gripper_or_move(self):
        commands = []
        def call(p, req):
            commands.append(req['cmd'])
            if req['cmd'] == 'state':
                return 0, {'mount': [.7, .1, 1.], 'grip': {'width': 0.}}
            return 0, {'ok': False, 'status': 'FAULT'}
        with patch.object(sys, 'argv', ['lowprobe', '--execute']), \
             patch.object(lowprobe, 'connect', return_value=(None, {'depth_cam': True})), \
             patch.object(lowprobe, 'call', side_effect=call), \
             patch('atexit.register'), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(lowprobe.main(), 4)
        self.assertEqual(commands, ['state', 'collision'])

    def test_readonly_transport_rejects_every_motion_command(self):
        import exec_transport
        p = types.SimpleNamespace(_allow_motion=False)
        for cmd in ('base', 'cart', 'joints', 'grip', 'execute_plan'):
            self.assertFalse(exec_transport.call(p, {'cmd': cmd})[1]['ok'])

    def test_preview_get_cannot_trigger_motion(self):
        preview = load('preview_test', HERE / 'preview_server.py')
        responses = []
        handler = preview.Handler.__new__(preview.Handler)
        handler._json = lambda obj, code=200: responses.append((obj, code))
        with patch.object(preview.ROBOT, 'call', side_effect=AssertionError('must not contact robot')):
            for path in ('/api/up', '/api/hover'):
                handler.path = path
                handler.do_GET()
        self.assertEqual([code for _, code in responses], [405, 405])

    def test_persistent_batch_stops_before_later_motion_on_failure(self):
        import pickplace_session
        session = pickplace_session.Session(None)
        with patch.object(session, 'request', side_effect=[{'ok': True}, {'ok': False}]) as request, \
                contextlib.redirect_stdout(io.StringIO()):
            result = session.perform({'action': 'batch', 'steps': [
                {'cmd': 'state'}, {'cmd': 'grip'}, {'cmd': 'joints'}]})
        self.assertFalse(result['ok'])
        self.assertEqual(request.call_count, 2)

    def test_persistent_move_rejects_missing_position_error(self):
        import pickplace_session
        session = pickplace_session.Session(None)
        plan = {'ok': True, 'max_delta_deg': 1, 'max_dev_cm': 0,
                'min_tip_z': 1, 'plan_id': 'once'}
        with patch.object(session, 'request', side_effect=[plan, {'ok': True}]):
            result = session.perform({'action': 'move', 'xyz': [.7, .1, 1.],
                                      'table_z': .7, 'min_tip': .8})
        self.assertFalse(result['ok'])


if __name__ == '__main__':
    unittest.main()
