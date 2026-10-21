#!/usr/bin/env python3
"""生成「开机自启 + 崩溃重启」的服务文件（macOS launchd / Linux systemd 用户服务），只打印，不自动安装。

看板自己每天按 schedule.daily_at 拉数；服务文件只负责让进程一直在。
用法（在看板目录下）：python3 tools/install_service.py [--name my-dashboard]
"""
import argparse
import os
import platform
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default=os.path.basename(ROOT).lower().replace(" ", "-") or "dashboard")
    a = ap.parse_args()
    start = os.path.join(ROOT, "start.sh")
    log = os.path.join(ROOT, "data", "server.log")
    if platform.system() == "Darwin":
        label = "local.%s" % a.name
        path = os.path.expanduser("~/Library/LaunchAgents/%s.plist" % label)
        print("""# 保存为 {path}，然后：
#   launchctl load -w {path}
#   停用：launchctl unload -w {path}
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key><array><string>/bin/sh</string><string>{start}</string></array>
  <key>WorkingDirectory</key><string>{root}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict></plist>""".format(path=path, label=label, start=start, root=ROOT, log=log))
    else:
        path = os.path.expanduser("~/.config/systemd/user/%s.service" % a.name)
        print("""# 保存为 {path}，然后：
#   systemctl --user daemon-reload && systemctl --user enable --now {name}
#   看日志：journalctl --user -u {name} -f
[Unit]
Description={name} dashboard

[Service]
WorkingDirectory={root}
ExecStart=/bin/sh {start}
Restart=on-failure

[Install]
WantedBy=default.target""".format(path=path, name=a.name, root=ROOT, start=start))
    return 0


if __name__ == "__main__":
    sys.exit(main())
