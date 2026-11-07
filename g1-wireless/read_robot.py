import json
import os
import socket
import time
import galbot_sdk
from galbot_sdk import GalbotRobot, MachineType

def send(data):
    print("G1_STATE " + json.dumps(data, allow_nan=False), flush=True)

host = socket.gethostname()
version = str(galbot_sdk.__version__)
shm = os.statvfs("/dev/shm")
identity = dict(host=host, sdk=version, shm_free_bytes=shm.f_bavail * shm.f_frsize,
                shm_total_bytes=shm.f_blocks * shm.f_frsize)
send(dict(identity, status="initializing"))
if host != "G1_HOST_PLACEHOLDER" or version != "1.10.0" or not identity["shm_free_bytes"]:
    send(dict(identity, status="identity, SDK version or shared memory check failed"))
    raise SystemExit(1)
robot = GalbotRobot.get_instance(MachineType.G1)
if not robot.init():
    send(dict(identity, status="SDK initialization failed"))
    raise SystemExit(1)
try:
    names = robot.get_joint_names(False)
    while True:
        joints = robot.get_joint_positions(joint_names=names) if names else []
        odom = robot.get_odom()
        if isinstance(odom, dict):
            odom = {key: odom.get(key) for key in
                    ("position", "orientation", "linear_velocity", "angular_velocity", "timestamp_ns")}
        elif odom is not None:
            odom = {key: getattr(odom, key, None) for key in
                    ("position", "orientation", "linear_velocity", "angular_velocity", "timestamp_ns")}
        good_joints = bool(names) and len(names) == len(joints)
        send(dict(identity, status="sample", joints=dict(zip(names, joints)) if good_joints else None,
                  odom=odom, sampled_at=time.time()))
        time.sleep(0.5)
finally:
    robot.destroy()

