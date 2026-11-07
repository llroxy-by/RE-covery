import asyncio
import signal
import threading
import time
import websockets
import server

async def stream(ws, *args):
    last = 0
    try:
        while True:
            with server.lock:
                data, stamp = server.frame, server.stamp
            if data and stamp != last and time.monotonic() - stamp < 3:
                await asyncio.wait_for(ws.send(data), timeout=3)
                last = stamp
            await asyncio.sleep(.04)
    except (websockets.exceptions.ConnectionClosed, asyncio.TimeoutError):
        pass

async def main():
    done = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, done.set)
    worker = threading.Thread(target=server.capture, daemon=True)
    worker.start()
    try:
        async with websockets.serve(stream, '127.0.0.1', 8781, max_size=1024, ping_interval=20):
            print('Camera WebSocket ready on 127.0.0.1:8781', flush=True)
            await done.wait()
    finally:
        server.stop.set()
        worker.join(timeout=10)

if __name__ == '__main__':
    asyncio.run(main())
