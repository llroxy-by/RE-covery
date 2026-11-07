"""Scene recognition through the user's configured vision endpoint. No robot control."""
import argparse
import base64
import json
import os
from pathlib import Path
import shlex
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', type=Path, required=True)
    parser.add_argument('--env-file', type=Path, default=Path(__file__).resolve().parent.parent / 'vision/config/.env.local')
    parser.add_argument('--base-url', default='https://api.llroxy.top/v1')
    parser.add_argument('--model', default='cn:kimi-k2.5')
    parser.add_argument('--prompt', default='')
    args = parser.parse_args()
    key = os.environ.get('VISION_API_KEY', '')
    if not key and args.env_file.exists():
        for raw in args.env_file.read_text().splitlines():
            line = raw.strip().removeprefix('export ')
            if line.startswith('VISION_API_KEY='):
                values = shlex.split(line.split('=', 1)[1], comments=True)
                key = values[0] if values else ''
    if not key:
        parser.error('请在本机环境变量或未提交的 .env.local 中设置 VISION_API_KEY')
    data = base64.b64encode(args.image.read_bytes()).decode()
    mime = 'image/png' if args.image.suffix.lower() == '.png' else 'image/jpeg'
    prompt = ('识别桌面上的红色球、白色指尖魔方、游标卡尺、水瓶和开放盒子。'
              '仅输出JSON，含图像宽高以及每件目标的名称、像素框[x0,y0,x1,y1]、'
              '置信度、遮挡情况。不确定就标记unknown。不要推测三维坐标或输出机器人指令。')
    if args.prompt:
        prompt = args.prompt
    body = {'model': args.model, 'messages': [{'role': 'user', 'content': [
        {'type': 'text', 'text': prompt},
        {'type': 'image_url', 'image_url': {'url': 'data:' + mime + ';base64,' + data}}]}],
        'max_tokens': 1600}
    req = urllib.request.Request(args.base_url.rstrip('/') + '/chat/completions',
        data=json.dumps(body).encode(), headers={'Authorization': 'Bearer ' + key,
                                               'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.load(response)
        print(json.dumps({'model': args.model, 'content': result['choices'][0]['message']['content'],
                          'usage': result.get('usage')}, ensure_ascii=False))
    except urllib.error.HTTPError as exc:
        raise SystemExit('视觉接口 HTTP %s；未切换到 GPT' % exc.code)
    except (urllib.error.URLError, TimeoutError):
        raise SystemExit('视觉接口连接失败或超时；未切换到 GPT')


if __name__ == '__main__':
    main()
