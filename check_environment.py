"""Offline checks shared by first-time setup and packaged-release smoke tests."""
import argparse
import json
import math
from pathlib import Path
import sys


def check_environment(*, native=False, aoi_files=(), picker_child=False):
    from app_runtime import APP_ROOT, VERSION, WEB_ROOT
    errors, versions = [], {}
    https = {}
    try:
        from https_runtime import ca_bundle, https_context
        https = {'caBundle': ca_bundle(), 'rootCertificates': https_context().cert_store_stats()['x509_ca']}
    except (OSError, ImportError) as error:
        errors.append('Cannot load trusted HTTPS root certificates: ' + str(error))
    if sys.platform != 'win32':
        try:
            import cryptography
            versions['cryptography'] = cryptography.__version__
        except ImportError:
            errors.append('Missing cryptography for encrypted local settings.')
    if not (3, 11) <= sys.version_info[:2] <= (3, 12):
        errors.append("Use Python 3.11 (recommended) or 3.12, 64-bit.")
    if sys.maxsize <= 2 ** 32:
        errors.append("A 64-bit Python installation is required.")
    try:
        import requests
        versions["requests"] = requests.__version__
        if not Path(requests.certs.where()).is_file():
            errors.append("The HTTPS CA certificate bundle is missing.")
        if getattr(sys, "frozen", False) and not Path(requests.certs.where()).resolve().is_relative_to(APP_ROOT):
            errors.append("The HTTPS CA bundle is outside the portable app.")
    except ImportError:
        errors.append("Missing requests.")
    try:
        import h5py
        import numpy
        versions["h5py"] = h5py.__version__
        versions["numpy"] = numpy.__version__
    except ImportError:
        errors.append("Missing h5py/numpy for NISAR RSLC subsets. Run first-time setup.")
    try:
        import shapefile
        versions["pyshp"] = shapefile.__version__
    except ImportError:
        errors.append("Missing pyshp (the shapefile module).")
    try:
        import pyproj
        versions["pyproj"] = pyproj.__version__
        point = pyproj.Transformer.from_crs(4326, 3857, always_xy=True).transform(86.8, 43.1)
        assert all(math.isfinite(value) for value in point)
        assert pyproj.CRS.from_epsg(4326).to_epsg() == 4326
        if getattr(sys, "frozen", False) and not Path(pyproj.datadir.get_data_dir()).resolve().is_relative_to(APP_ROOT):
            errors.append("The PROJ database is outside the portable app.")
    except Exception:
        errors.append("pyproj or its PROJ database cannot be loaded.")
    try:
        import shapely
        from shapely.geometry import Polygon
        versions["shapely"] = shapely.__version__
        assert Polygon([(0, 0), (2, 0), (2, 2), (0, 2)]).intersection(Polygon([(1, 1), (3, 1), (3, 3), (1, 3)])).area == 1
    except Exception:
        errors.append("shapely or its GEOS library cannot be loaded.")
    try:
        import tkinter
        from tkinter import filedialog
        versions["tkinter"] = str(tkinter.TkVersion)
        if native:
            root = tkinter.Tk()
            root.withdraw()
            try:
                versions["tkinter"] = str(root.tk.call("info", "patchlevel"))
                if getattr(sys, "frozen", False) and not Path(root.tk.eval("set tcl_library")).resolve().is_relative_to(APP_ROOT):
                    errors.append("The Tcl library is outside the portable app.")
            finally:
                root.destroy()
    except Exception:
        errors.append("tkinter/Tcl/Tk is missing or cannot initialize. Install Python with Tcl/Tk support.")
    for resource in ("index.html", "app.js", "vendor/leaflet.js", "vendor/leaflet.css"):
        if not (WEB_ROOT / resource).is_file():
            errors.append("Missing web resource: " + resource)
    imported = []
    for filename in aoi_files:
        try:
            from aoi_import import read_aoi
            result = read_aoi(filename)
            imported.append({"filename": Path(filename).name, "bounds": result["bounds"], "featureCount": result["featureCount"]})
        except Exception:
            errors.append("AOI import failed: " + Path(filename).name)
    child_ok = None
    if picker_child:
        try:
            from server import choose_aoi
            child = choose_aoi(check=True)
            child_ok = child.get("ok") is True and child.get("frozen") == bool(getattr(sys, "frozen", False))
            if not child_ok:
                errors.append("The native picker child process failed its runtime check.")
        except Exception:
            child_ok = False
            errors.append("The native picker child process could not be started.")
    build_info = None
    if getattr(sys, 'frozen', False):
        try:
            build_info = json.loads((APP_ROOT / 'build-info.json').read_text(encoding='utf-8'))
            if build_info['version'] != VERSION or len(build_info['sourceTreeSha256']) != 64:
                raise ValueError()
        except (OSError, ValueError, KeyError, TypeError):
            errors.append('Missing or invalid source build identity.')
    return {"ok": not errors, "version": VERSION, "build": build_info, "python": sys.version.split()[0],
            "frozen": bool(getattr(sys, "frozen", False)), "dependencies": versions, "aoi": imported,
            "pickerChild": child_ok, "https": https, "errors": errors}


def main(arguments=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--native", action="store_true", help="Initialize and close an invisible Tk window")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--aoi", action="append", default=[], help="Offline import check using a supplied AOI file")
    parser.add_argument("--picker-child", action="store_true", help="Check the actual picker subprocess without opening a dialog")
    args = parser.parse_args(arguments)
    result = check_environment(native=args.native, aoi_files=args.aoi, picker_child=args.picker_child)
    if not args.quiet or not result["ok"]:
        print(json.dumps(result, ensure_ascii=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
