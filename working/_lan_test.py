import subprocess, sys, time, urllib.request, os
os.environ["NO_PROXY"] = "*"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PY = r"C:/Users/lenovo/Desktop/WorkBuddy/resources/vendor/python/python.exe"
p = subprocess.Popen([PY, "-m", "vision.tools.phone_capture", "--host", "0.0.0.0",
                      "--port", "8300", "--output", "vision/records/_lantest"],
                     cwd=r"C:/Users/lenovo/WorkBuddy/2026-11-06-15-58-54",
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
try:
    time.sleep(3.0)
    for host in ("127.0.0.1", "10.0.110.161"):
        try:
            with opener.open(f"http://{host}:8300/", timeout=6) as r:
                body = r.read().decode("utf-8", "replace")
                print(f"{host}:8300  ->  HTTP {r.status}, {len(body)} 字节, 含按钮={'建立 S0' in body}")
        except Exception as e:
            print(f"{host}:8300  ->  失败 {type(e).__name__}: {e}")
finally:
    p.terminate()
    try: p.communicate(timeout=6)
    except Exception: p.kill()
