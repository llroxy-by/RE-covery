#!/bin/sh
# G1 Joint Lab 一键启动
#   1. 自动在已连线网口上找机器人（IPv6 链路本地 + 主机密钥指纹校验，IP 变了也能找到）
#   2. 建立 SSH 主连接（只输一次密码）
#   3. 起控制台 → 开浏览器
#
# 用法：./start.sh                      自动发现
#       ROBOT_HOST=x.x.x.x ./start.sh   指定地址（跳过发现）
#       DRY=1 ./start.sh                只做发现和校验，不连不断言
set -u
cd "$(dirname "$0")"

SOCK=/tmp/kitt-console-live.sock
URL=http://127.0.0.1:8765
USER_NAME=galbot
# 机器人 SSH 主机密钥指纹（公开信息，用于确认"连的是同一台机器"）
FP=1MH5iIhYyb2gK5Q02SOYqO0Ynil5Xqgh14oqNz/5s6k

fp_of() { # $1=地址 $2=网口
  ssh-keyscan -6 -T 3 -t ed25519 "$1%$2" 2>/dev/null \
    | awk 'NF>=3{print $2" "$3}' | ssh-keygen -lf - 2>/dev/null | awk '{print $2}' | sed 's/^SHA256://'
}

own_macs() { # 本机各网口 MAC，用来排除自己
  for i in $(ifconfig -l); do
    m=$(ifconfig "$i" 2>/dev/null | awk '/ether/{print $2; exit}')
    [ -n "$m" ] && echo "$i $m"
  done
}
OWNS=$(own_macs)

TARGET=""

if [ -n "${ROBOT_HOST:-}" ]; then
  TARGET="$USER_NAME@$ROBOT_HOST"
  echo "· 使用指定地址 $ROBOT_HOST"
else
  echo "· 在已连线的网口上寻找机器人…"
  # 主动触发一次邻居发现，填充缓存
  for i in $(ifconfig -l); do
    case "$i" in en*) ;; *) continue ;; esac
    ifconfig "$i" 2>/dev/null | grep -q "status: active" || continue
    ping6 -c 2 -i 1 "ff02::1%$i" >/dev/null 2>&1
  done
  sleep 1

  # 收集候选：链路本地 IPv6 邻居，排除本机
  CANDS=""
  for entry in $(ndp -an 2>/dev/null | awk '$1 ~ /^fe80/{print $1}'); do
    addr=$(printf '%s' "$entry" | cut -d% -f1)
    ifc=$(printf '%s' "$entry" | cut -d% -f2)
    mac=$(ndp -an 2>/dev/null | awk -v a="$entry" '$1==a{print $2}')
    case "$ifc" in en*) ;; *) continue ;; esac
    case "$OWNS" in *"$ifc $mac"*) continue ;; esac
    CANDS="$CANDS $addr%$ifc"
  done

  if [ -z "$CANDS" ]; then
    echo "✗ 网口上没有发现设备"
    echo "  · 确认网线两端都插好（本机网口灯、机器人网口灯）"
    echo "  · 若机器人是静态 IPv4：ROBOT_HOST=192.168.x.x ./start.sh"
    exit 1
  fi

  for c in $CANDS; do
    addr=$(printf '%s' "$c" | cut -d% -f1)
    ifc=$(printf '%s' "$c" | cut -d% -f2)
    f=$(fp_of "$addr" "$ifc")
    if [ "$f" = "$FP" ]; then
      echo "✓ 找到机器人：$addr%$ifc"
      TARGET="$USER_NAME@$c"
      break
    else
      echo "  跳过 ${addr}%${ifc}（指纹 ${f:-读取失败}，不是这台机器人）"
    fi
  done

  if [ -z "$TARGET" ]; then
    echo "✗ 没有指纹匹配的机器人"
    echo "  · 可能是机器人开机但 SSH 未就绪，或网口在别的子网"
    echo "  · 试：ROBOT_HOST=<地址> ./start.sh"
    exit 1
  fi
fi

[ -n "${DRY:-}" ] && { echo "DRY：目标 ${TARGET}，到此为止"; exit 0; }

if ! ssh -S "$SOCK" -O check "$TARGET" 2>/dev/null; then
  rm -f "$SOCK"
  echo "建立 SSH 主连接（请输入机器人密码，仅此一次）…"
  ssh -M -S "$SOCK" -o ControlPersist=yes -o ServerAliveInterval=5 -o ConnectTimeout=8 -f -N "$TARGET" || {
    echo "✗ SSH 主连接建立失败（密码错 / 机器人 SSH 未开）"; exit 1; }
fi
echo "✓ SSH 主连接就绪（${SOCK}）"

if lsof -nP -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "! 8765 已有进程在监听，直接打开页面"
  open "$URL"; exit 0
fi

ROBOT_HOST="${TARGET#*@}"
export ROBOT_HOST
( sleep 2; open "$URL" ) >/dev/null 2>&1 &
exec python3 server.py
