import os
import math
import signal
import socket
import sys
import time
import galbot_sdk
from galbot_sdk import ControlStatus, GalbotRobot, MachineType

def pulse(robot, success, sleep=time.sleep):
    try:
        status = robot.set_base_velocity([-0.01, 0.0, 0.0], [0.0, 0.0, 0.0], 0.0)
        if status != success:
            raise RuntimeError("Velocity command failed: " + str(status))
        sleep(0.3)
    finally:
        status = robot.stop_base()
        print("STOP RESULT:", status, flush=True)
        if status != success:
            raise RuntimeError("STOP NOT CONFIRMED: use on-site physical emergency stop")

def odom_sample(robot):
    data = robot.get_odom()
    def field(name):
        return data[name] if isinstance(data, dict) else getattr(data, name)
    x, y, _ = field("position")
    qx, qy, qz, qw = field("orientation")
    stamp = field("timestamp_ns")
    if not all(map(math.isfinite, (x, y, qx, qy, qz, qw))) or stamp <= 0:
        raise RuntimeError("Invalid odometry")
    if not 0.9 <= math.hypot(qx, qy, qz, qw) <= 1.1:
        raise RuntimeError("Invalid odometry orientation")
    yaw = math.atan2(2 * (qw*qz + qx*qy), 1 - 2 * (qy*qy + qz*qz))
    return x, y, yaw, stamp

def reverse_5cm(robot, success, clock=time.monotonic, sleep=time.sleep):
    x, y, yaw, first_stamp = odom_sample(robot)
    sleep(0.12)
    x, y, yaw, stamp = odom_sample(robot)
    if stamp <= first_stamp:
        raise RuntimeError("Odometry is not updating")
    started = updated = clock()
    reason = "time limit"
    try:
        while clock() - started < 7:
            px, py, heading, new_stamp = odom_sample(robot)
            now = clock()
            if new_stamp < stamp or (new_stamp == stamp and now - updated > 0.25):
                reason = "odometry interrupted"
                break
            if new_stamp > stamp:
                stamp, updated = new_stamp, now
            dx, dy = px - x, py - y
            rear = -(dx * math.cos(yaw) + dy * math.sin(yaw))
            sideways = -dx * math.sin(yaw) + dy * math.cos(yaw)
            turn = math.remainder(heading - yaw, 2 * math.pi)
            if rear < -0.015 or abs(sideways) > 0.015 or abs(turn) > 0.12:
                reason = "unexpected path"
                break
            if rear >= 0.05:
                reason = "target reached"
                break
            if robot.set_base_velocity([-0.01, 0., 0.], [0., 0., 0.], 0.) != success:
                reason = "velocity command rejected"
                break
            sleep(0.1)
    finally:
        status = robot.stop_base()
        print("STOP RESULT:", status, flush=True)
        if status != success:
            raise RuntimeError("STOP NOT CONFIRMED: use on-site physical emergency stop")
    sleep(0.3)
    px, py, _, final_stamp = odom_sample(robot)
    if final_stamp <= stamp:
        raise RuntimeError(f"{reason}: final odometry is stale")
    rear = -((px - x) * math.cos(yaw) + (py - y) * math.sin(yaw))
    print(f"{reason} | measured rear displacement: {rear * 100:.2f} cm", flush=True)

def interrupted(signum, _frame):
    raise SystemExit(f"Interrupted by signal {signum}")

def read_height(robot, after=0):
    for _ in range(10):
        transform, timestamp = robot.get_transform("base_link", "head_base_link")
        if len(transform) == 7 and timestamp > after and math.isfinite(transform[2]):
            print(f"Current height: {transform[2]:.4f} m | TF timestamp: {timestamp}", flush=True)
            return transform[2], timestamp
        time.sleep(0.1)
    raise RuntimeError("Height feedback unavailable")

def raise_5cm(robot):
    _, first_stamp = read_height(robot)
    start, stamp = read_height(robot, after=first_stamp)
    target = start + 0.05
    if not 0.96 <= start <= target <= 1.54:
        raise RuntimeError("Start or target height outside SDK range")
    print(f"Lift target: {target:.4f} m | requested duration: 6 s", flush=True)
    status = robot.set_leg_height(target, duration=6.0, is_blocking=True, timeout_s=15.0)
    print("LIFT RESULT:", status, flush=True)
    if status != ControlStatus.SUCCESS:
        raise RuntimeError("Lift completion unconfirmed. Check on site before retrying.")
    end, _ = read_height(robot, after=stamp)
    print(f"Measured rise: {(end - start) * 100:.2f} cm", flush=True)

def main():
    host = socket.gethostname()
    version = str(galbot_sdk.__version__)
    shm = os.statvfs("/dev/shm")
    available = shm.f_bavail * shm.f_frsize
    print(f"{host} | SDK {version} | /dev/shm {available // 1048576} MiB free", flush=True)
    if host != "G1_HOST_PLACEHOLDER" or version != "1.10.0" or not available:
        raise SystemExit("Preflight failed")
    robot = GalbotRobot.get_instance(MachineType.G1)
    if not callable(robot.set_base_velocity) or not callable(robot.stop_base):
        raise SystemExit("Required SDK methods unavailable")
    action = sys.stdin.readline()
    if action not in ("REVERSE ONCE\n", "REVERSE 5CM\n", "READ HEIGHT\n", "RAISE 5CM\n"):
        print("Preflight passed. Motion disabled.", flush=True)
        return
    if action != "REVERSE ONCE\n" and available < 2 * 1024**3:
        raise SystemExit("SDK initialization skipped: /dev/shm has less than 2 GiB free")
    for sig in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(sig, interrupted)
    if not robot.init():
        raise SystemExit("SDK init failed")
    try:
        if action == "READ HEIGHT\n":
            read_height(robot)
            return
        if action == "RAISE 5CM\n":
            raise_5cm(robot)
            return
        if not robot.get_odom():
            raise SystemExit("Odometry unavailable")
        if action == "REVERSE 5CM\n":
            reverse_5cm(robot, ControlStatus.SUCCESS)
        else:
            pulse(robot, ControlStatus.SUCCESS)
        print("Stop command acknowledged. Verify the robot is stationary on site.", flush=True)
    finally:
        robot.destroy()

if __name__ == "__main__":
    main()

