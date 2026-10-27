"""Run the native picker in a short-lived main-thread process."""
import json
import sys

from aoi_import import AOIError, read_aoi
from native_dialog import enable_dpi_awareness, large_native_dialog


def pick_and_read():
    import tkinter as tk
    from tkinter import filedialog

    enable_dpi_awareness()
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        with large_native_dialog("导入研究区 · SHP / KML / KMZ"):
            filename = filedialog.askopenfilename(
                parent=root,
                title="导入研究区 · SHP / KML / KMZ",
                filetypes=[
                    ("研究区文件", "*.shp *.kml *.kmz *.geojson *.json"),
                    ("ESRI Shapefile", "*.shp"),
                    ("Google Earth KML", "*.kml"),
                    ("Google Earth KMZ", "*.kmz"),
                    ("GeoJSON", "*.geojson *.json"),
                ],
            )
    finally:
        root.destroy()
    return read_aoi(filename) if filename else {"cancelled": True}


def pick_folder(title="选择 SAR 影像目录"):
    import tkinter as tk
    from tkinter import filedialog
    enable_dpi_awareness()
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        with large_native_dialog(title):
            folder = filedialog.askdirectory(parent=root, title=title, mustexist=True)
    finally:
        root.destroy()
    return {"output": folder} if folder else {"cancelled": True}


def main(arguments=None):
    arguments = sys.argv[1:] if arguments is None else arguments
    try:
        if arguments == ["--check"]:
            from check_environment import check_environment
            result = check_environment(native=True)
        elif arguments in ([], ["--folder"], ["--orbit-folder"]):
            result = pick_folder("选择 Sentinel-1 影像文件夹") if arguments == ["--orbit-folder"] else pick_folder() if arguments else pick_and_read()
        else:
            result = {"error": "文件选择器参数无效。"}
    except AOIError as error:
        result = {"error": str(error)}
    except Exception:
        result = {"error": "无法打开文件选择器，请确认图形会话可用，并使用本机启动入口运行。"}
    # ASCII transport avoids Windows console code-page differences.
    sys.stdout.write(json.dumps(result, ensure_ascii=True, allow_nan=False))
    sys.stdout.flush()
    return 1 if result.get("error") or result.get("ok") is False else 0


if __name__ == "__main__":
    raise SystemExit(main())
