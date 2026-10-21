#!/usr/bin/env python3
"""一键生成看板：把 template/ 复制到目标目录、放好 dashboard.yaml、建 venv 装依赖、校验配置。

用法：
  python3 scripts/generate.py <目标目录> --config <访谈产出的 dashboard.yaml>
  python3 scripts/generate.py <目标目录> --demo          # 用合成数据 + 样例配置先跑起来看效果
可选：--port 5050  --no-venv（不建 venv，自己装依赖）  --probe（生成后连数据源做一次自检）

目标目录必须不存在或为空；不会覆盖任何已有文件。
"""
import argparse
import os
import shutil
import subprocess
import sys

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(SKILL_ROOT, "template")
SKIP = {"data", ".venv", "__pycache__", "dashboard.yaml", ".env", ".DS_Store"}


def run(cmd, cwd):
    print("$ " + " ".join(cmd))
    return subprocess.run(cmd, cwd=cwd).returncode


def set_port(path, port):
    import re
    s = open(path, encoding="utf-8").read()
    s2, n = re.subn(r"(?m)^(\s*port:\s*)\d+", r"\g<1>%d" % port, s, count=1)
    if not n:
        s2 = s + "\nserver:\n  host: 127.0.0.1\n  port: %d\n" % port
    open(path, "w", encoding="utf-8").write(s2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--config", help="写好的 dashboard.yaml")
    g.add_argument("--demo", action="store_true", help="生成合成数据，用样例配置")
    ap.add_argument("--port", type=int)
    ap.add_argument("--no-venv", action="store_true")
    ap.add_argument("--probe", action="store_true")
    a = ap.parse_args()

    dst = os.path.abspath(os.path.expanduser(a.target))
    if os.path.exists(dst) and os.listdir(dst):
        sys.exit("目标目录 %s 已存在且不为空；换一个目录，或先自行确认后清空" % dst)
    if a.config and not os.path.exists(a.config):
        sys.exit("找不到配置文件 %s" % a.config)
    shutil.copytree(TEMPLATE, dst, ignore=lambda d, names: [n for n in names if n in SKIP], dirs_exist_ok=True)
    cfg = os.path.join(dst, "dashboard.yaml")
    shutil.copyfile(a.config if a.config else os.path.join(dst, "dashboard.example.yaml"), cfg)
    if a.port:
        set_port(cfg, a.port)
    print("✓ 已生成看板目录：%s" % dst)

    py = sys.executable
    if not a.no_venv:
        if run([sys.executable, "-m", "venv", ".venv"], dst) or run([".venv/bin/pip", "install", "-q", "-r", "requirements.txt"], dst):
            sys.exit("✗ 建 venv / 装依赖失败；可加 --no-venv 后自行安装 requirements.txt")
        py = os.path.join(dst, ".venv", "bin", "python")
    if a.demo:
        os.makedirs(os.path.join(dst, "data"), exist_ok=True)
        if run([py, "tools/demo_data.py", "data/demo.duckdb"], dst):
            sys.exit("✗ 演示数据生成失败")
    chk = [py, "tools/check_config.py"] + (["--probe"] if a.probe or a.demo else [])
    rc = run(chk, dst)
    if rc:
        print("⚠ 配置自检未通过（退出码 %d），按上面的提示改 %s 后重跑：%s" % (rc, cfg, " ".join(chk)))
    port = a.port or 5050
    print("\n下一步：\n  cd %s && ./start.sh\n  浏览器打开 http://127.0.0.1:%d ，日常漏斗页点「从数据源刷新」拉第一批数。" % (dst, port))
    if a.demo:
        print("  演示数据里埋了一个实验：分组 G100（对照）/ G101（实验），开窗日见上面 demo_data 的输出。")
    return rc


if __name__ == "__main__":
    sys.exit(main())
