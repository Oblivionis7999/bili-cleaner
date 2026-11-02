"""命令行入口：启动本地服务并打开浏览器。"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import webbrowser

from .const import APP_NAME, DEFAULT_PORT, VERSION
from .store import Store


def _find_free_port(host: str, start: int, tries: int = 20) -> int:
    for offset in range(tries):
        port = start + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise SystemExit(f"端口 {start}-{start + tries} 都被占用了，用 --port 指定一个空闲端口")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bilicleaner",
        description=f"{APP_NAME} {VERSION} — 清理你自己的 B 站账号数据（关注 / 动态 / 收藏夹 / 稍后再看）",
    )
    p.add_argument("--host", default="127.0.0.1", help="监听地址，默认只监听本机")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"端口，默认 {DEFAULT_PORT}（被占用时自动顺延）")
    p.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    p.add_argument("--mock", action="store_true", help="用内置模拟数据运行，不连接 B 站（开发/演示用）")
    p.add_argument("--data-dir", default=None, help="自定义数据目录（登录态/设置/备份）")
    p.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    store = Store(args.data_dir)

    # 延迟导入，避免 --version 之类的小操作也要加载 FastAPI
    from .api.app import create_app

    app = create_app(store=store, mock=args.mock)
    port = _find_free_port(args.host, args.port)
    url = f"http://{args.host}:{port}/"

    banner = (
        f"\n  {APP_NAME} {VERSION} 已启动"
        f"\n  地址: {url}"
        f"\n  数据目录: {store.data_dir}"
        f"\n  模式: {'模拟数据' if args.mock else '真实账号'}"
        f"\n  关闭窗口或按 Ctrl+C 退出\n"
    )
    print(banner, flush=True)

    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    import uvicorn

    try:
        uvicorn.run(app, host=args.host, port=port, log_level="warning")
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
