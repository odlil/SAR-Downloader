"""Common source/frozen entry point. Picker subprocesses never start a server."""
import sys


def main():
    from https_runtime import configure_https
    configure_https()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if sys.argv[1:2] == ["--native-dialog"]:
        from aoi_dialog import main as picker_main
        return picker_main(sys.argv[2:])
    if sys.argv[1:2] == ["--self-check"]:
        from check_environment import main as check_main
        return check_main(sys.argv[2:])
    from server import main as server_main
    result = server_main()
    if result and sys.platform == "win32" and getattr(sys, "frozen", False) and "--no-browser" not in sys.argv:
        # A double-clicked console would otherwise disappear before its error
        # can be read. Automated/no-browser invocations remain non-interactive.
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, "SAR Downloader 启动失败。请先关闭旧启动窗口，检查端口或用户目录权限后重试。", "SAR Downloader", 0x10)
    return result


if __name__ == "__main__":
    raise SystemExit(main() or 0)
