#!/bin/zsh
# 双击运行：用 Mac 本机摄像头跑实时视觉检测（不需要手机、不需要 HTTPS 证书）
#
# key 从 vision/config/.env.local 读（已配好，双击即用，不用再输）。
# 想换 key：改那个文件里的一行即可。文件不存在时才会让你手输。
cd "${0:A:h}"

ENV_FILE="vision/config/.env.local"
if [[ -f "$ENV_FILE" ]]; then
  source "$ENV_FILE"
fi

if [[ -z "$VISION_API_KEY" ]]; then
  read -s 'VISION_API_KEY?没找到 .env.local，请输入 API token（不显示，直接回车则只用本地模板库）：'
  echo
  export VISION_API_KEY
fi

echo "启动中… 浏览器会自动打开 http://127.0.0.1:8300/"
echo "第一次会问摄像头权限，点「允许」。关掉这个窗口就停止服务。"
echo
exec ../.venv-vision/bin/python -m vision.tools.mac_live \
  --config vision/config/mac.json "$@"
