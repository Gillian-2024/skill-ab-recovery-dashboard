#!/bin/bash
# 启动看板：http://127.0.0.1:5050
cd "$(dirname "$0")"
export DEVELOPER_DIR=/Library/Developer/CommandLineTools
exec .venv/bin/python -m dashboard.app
