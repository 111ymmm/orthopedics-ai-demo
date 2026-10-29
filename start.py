"""Validate the runtime, reuse a running local service, or launch the application."""
from pathlib import Path
import argparse
import json
import subprocess
import sys
import webbrowser
from urllib.request import ProxyHandler, build_opener


def service_running(url):
    try:
        with build_opener(ProxyHandler({})).open(url + "/api/health", timeout=1.5) as response:
            result = json.load(response)
        return result.get("status") == "ok" and result.get("mode") == "local_demo" and result.get("version") == "2.0.0"
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(description="启动骨科专科辅助规划工作台")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--check", action="store_true", help="只检查环境，不启动服务或浏览器")
    parser.add_argument("--no-browser", action="store_true", help="启动时不自动打开浏览器")
    args = parser.parse_args()
    if sys.version_info < (3, 10):
        raise SystemExit("请安装 Python 3.10 或更新版本。")
    root = Path(__file__).resolve().parent
    url = f"http://127.0.0.1:{args.port}"
    try:
        from PIL import Image
        pillow_available = True
    except ImportError:
        pillow_available = False
    if args.check:
        print(json.dumps({"python": sys.executable, "version": sys.version.split()[0],
                          "pillow_available": pillow_available, "service_running": service_running(url)}, ensure_ascii=False))
        return
    if service_running(url):
        print(f"服务已经运行：{url}")
        if not args.no_browser:
            webbrowser.open(url)
        return
    runtime = sys.executable
    if not pillow_available:
        print("正在创建项目独立环境并安装 Pillow，首次安装需要网络。")
        environment = root / ".venv"
        if not environment.exists():
            subprocess.check_call([sys.executable, "-m", "venv", str(environment)])
        runtime = str(environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python"))
        subprocess.check_call([runtime, "-m", "pip", "install", "-r", str(root / "requirements.txt")])
    print(f"本地页面：{url}\n请保持本窗口打开；关闭窗口或按 Ctrl+C 会停止服务。", flush=True)
    command = [runtime, "-u", str(root / "app.py"), "--port", str(args.port)]
    if not args.no_browser:
        command.append("--open")
    subprocess.run(command, cwd=root, check=True)


if __name__ == "__main__":
    main()
