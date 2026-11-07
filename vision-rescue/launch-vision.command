#!/bin/zsh
cd "${0:A:h}"
read -s 'VISION_API_KEY?请输入 API token（不显示）：'
echo
export VISION_API_KEY
../.venv-vision/bin/python -m vision.tools.phone_live
