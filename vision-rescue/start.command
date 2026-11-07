#!/bin/zsh
# 双击运行：RE:BENCH 实时视觉（Mac 本机摄像头，画面在浏览器本地渲染）
#
# key 从 vision/config/.env.local 读，用来让视觉模型兜底认字母。
# 键不存在也能跑，只是只剩本地模板比对。
cd "${0:A:h}"

ENV_FILE="vision/config/.env.local"
if [[ -f "$ENV_FILE" ]]; then
  source "$ENV_FILE"
fi

PORT="${PORT:-8400}"
echo "启动中… 浏览器会自动打开 http://127.0.0.1:$PORT/"
echo "第一次会问摄像头权限，点「允许」。关掉这个窗口就停止服务。"
echo
exec ../.venv-vision/bin/python -m live.server \
  --config vision/config/mac.json --port "$PORT" "$@"
