"""Connect the existing camera JPEG WebSocket to the existing letter reader."""
import argparse
import asyncio
import json
import os
import time
from scene_store import publish
from pathlib import Path

from live.server import Bench


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


async def run(args):
    import websockets
    bench = Bench(args.config)
    if not bench.reader.vlm.available and not args.template_only:
        raise RuntimeError('视觉模型不可用：' + bench.reader.vlm.why_unavailable())
    latest = asyncio.Queue(maxsize=1)

    async def receive(ws):
        while True:
            jpeg = await asyncio.wait_for(ws.recv(), timeout=args.timeout)
            if not isinstance(jpeg, bytes) or not jpeg.startswith(b'\xff\xd8'):
                raise RuntimeError('相机消息不是 JPEG')
            if latest.full():
                latest.get_nowait()
            latest.put_nowait((jpeg,time.time()))

    async with websockets.connect(args.camera, max_size=16 * 1024 * 1024) as ws:
        receiver = asyncio.create_task(receive(ws))
        try:
            for index in range(args.frames):
                get = asyncio.create_task(latest.get())
                done, _ = await asyncio.wait([get, receiver], return_when=asyncio.FIRST_COMPLETED)
                if receiver in done:
                    get.cancel()
                    await receiver
                jpeg, captured_at = await get
                result = await asyncio.get_running_loop().run_in_executor(None, bench.detect, jpeg, 0, 0)
                if receiver.done():
                    await receiver
                if not result.get('ok'):
                    raise RuntimeError(result.get('error', '识别失败'))
                result['source'] = 'robot_camera'
                result['sequence'] = index + 1
                # Other-camera zone coordinates must not be presented as calibrated.
                for track in result.get('tracks', []):
                    track['zone'] = None
                result['zones_calibrated'] = False
                result = publish(result, captured_at)
                emit(result)
                if bench.reader.vlm.given_up and not args.template_only:
                    raise RuntimeError('视觉模型连续失败，停止识别')
        finally:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)


def main():
    os.chdir(Path(__file__).resolve().parent)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--camera', default='ws://127.0.0.1:8781')
    parser.add_argument('--config', default='vision/config/mac.json')
    parser.add_argument('--frames', type=int, default=300)
    parser.add_argument('--timeout', type=float, default=5)
    parser.add_argument('--template-only', action='store_true', help='允许没有模型配置时使用模板')
    args = parser.parse_args()
    if args.frames <= 0 or args.timeout <= 0:
        parser.error('frames 和 timeout 必须大于零')
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        emit({'ok': False, 'error': str(exc)})
        raise SystemExit(1)


if __name__ == '__main__':
    main()
