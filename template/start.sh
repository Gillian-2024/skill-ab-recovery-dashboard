#!/bin/sh
# 启动看板：首次运行自动建 venv 并装依赖；之后直接起服务。
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
exec .venv/bin/python -m dashboard.app
