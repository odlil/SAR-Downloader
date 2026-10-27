from __future__ import annotations

from functools import partial
import json
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aoi_import import AOIError, read_aoi
from server import Handler, ThreadingHTTPServer

KML = b'''<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark><name>AOI</name>
<MultiGeometry><Polygon><outerBoundaryIs><LinearRing><coordinates>
102.7,29.5,0 104.5,29.5,0 104.5,31.5,0 102.7,31.5,0 102.7,29.5,0
</coordinates></LinearRing></outerBoundaryIs>
<innerBoundaryIs><LinearRing><coordinates>
103,30 103,30.5 103.5,30.5 103.5,30 103,30
</coordinates></LinearRing></innerBoundaryIs></Polygon></MultiGeometry>
</Placemark></Document></kml>'''


class AOIImportTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="范围 导入 ", dir=ROOT / "tests")).resolve()
        self.assertTrue(self.folder.is_relative_to(ROOT.resolve()))

    def tearDown(self):
        shutil.rmtree(self.folder)

    def test_single_shp_reads_adjacent_projection_without_zip_or_dbf(self):
        for extension in (".shp", ".prj"):
            shutil.copyfile(ROOT / "tests" / "fixtures" / ("aoi" + extension), self.folder / ("glacier" + extension))
        result = read_aoi(self.folder / "glacier.shp")
        self.assertEqual(result["format"], "SHP")
        self.assertEqual(result["filename"], "glacier.shp")
        self.assertEqual(result["featureCount"], 1)
        for actual, expected in zip(result["bounds"], [102.7, 29.5, 104.5, 31.5]):
            self.assertAlmostEqual(actual, expected, places=6)

    def test_projected_shp_is_converted_to_wgs84(self):
        import shapefile
        from pyproj import CRS, Transformer

        transform = Transformer.from_crs(4326, 32648, always_xy=True)
        points = [[102.7, 29.5], [102.7, 31.5], [104.5, 31.5], [104.5, 29.5], [102.7, 29.5]]
        target = self.folder / "投影范围.shp"
        with shapefile.Writer(str(target)) as writer:
            writer.field("name", "C")
            writer.poly([[list(transform.transform(x, y)) for x, y in points]])
            writer.record("test")
        target.with_suffix(".PRJ").write_text(CRS.from_epsg(32648).to_wkt(), encoding="utf-8")
        for actual, expected in zip(read_aoi(target)["bounds"], [102.7, 29.5, 104.5, 31.5]):
            self.assertAlmostEqual(actual, expected, places=6)

    def dimensional_shp(self, kind=15, include_m=True, projected=False):
        """Create independent, synthetic PolygonZ/M fixtures (no user data)."""
        import shapefile
        from pyproj import CRS, Transformer

        outer = [[102.7, 29.5], [102.7, 31.5], [104.5, 31.5], [104.5, 29.5], [102.7, 29.5]]
        hole = [[103, 30], [103.5, 30], [103.5, 30.5], [103, 30.5], [103, 30]]
        island = [[105, 30], [105, 31], [105.5, 31], [105.5, 30], [105, 30]]
        crs = CRS.from_epsg(32648 if projected else 4326)
        transform = Transformer.from_crs(4326, crs, always_xy=True)
        target = self.folder / "三维范围.shp"
        with shapefile.Writer(str(target), shapeType=kind) as writer:
            writer.field("name", "C")
            for rings in ([outer, hole, island], [outer]):
                xy = [[list(transform.transform(*point)) for point in ring] for ring in rings]
                if kind == 15:
                    writer.polyz([[[*point, 100, 200] for point in ring] for ring in xy])
                else:
                    writer.polym([[[*point, 200] for point in ring] for ring in xy])
                writer.record("synthetic")
        target.with_suffix(".prj").write_text(crs.to_wkt(), encoding="utf-8")
        if not include_m:
            # PolygonZ's M range/array are optional in the ESRI specification.
            source = target.read_bytes()
            shp, shx = bytearray(source[:100]), bytearray(target.with_suffix(".shx").read_bytes()[:100])
            offset = 100
            while offset < len(source):
                record, words = struct.unpack_from(">ii", source, offset)
                content = source[offset + 8:offset + 8 + words * 2]
                parts, points = struct.unpack_from("<ii", content, 36)
                content = content[:44 + 4 * parts + 24 * points + 16]
                shx.extend(struct.pack(">ii", len(shp) // 2, len(content) // 2))
                shp.extend(struct.pack(">ii", record, len(content) // 2) + content)
                offset += 8 + words * 2
            struct.pack_into(">i", shp, 24, len(shp) // 2)
            struct.pack_into(">i", shx, 24, len(shx) // 2)
            target.write_bytes(shp)
            target.with_suffix(".shx").write_bytes(shx)
        return target

    def test_polygonz_without_optional_m_keeps_holes_parts_records_and_originals(self):
        target = self.dimensional_shp(include_m=False)
        originals = {path: path.read_bytes() for path in self.folder.iterdir()}
        for has_index in (True, False):
            with self.subTest(has_index=has_index):
                if not has_index:
                    target.with_suffix(".shx").unlink()
                result = read_aoi(target)
                self.assertEqual(result["featureCount"], 2)
                self.assertEqual(result["bounds"], [102.7, 29.5, 105.5, 31.5])
                first = result["geometry"]["features"][0]["geometry"]
                self.assertEqual(first["type"], "MultiPolygon")
                self.assertEqual(len(first["coordinates"]), 2)
                self.assertEqual(sorted(len(polygon) for polygon in first["coordinates"]), [1, 2])
                self.assertEqual(sum(len(ring) for polygon in first["coordinates"] for ring in polygon), 15)
                self.assertTrue(all(len(point) == 2 for polygon in first["coordinates"] for ring in polygon for point in ring))
                for path, data in originals.items():
                    if path.exists():
                        self.assertEqual(path.read_bytes(), data)

    def test_projected_polygonz_without_m_and_full_zm_or_m_are_supported(self):
        for kind, include_m, projected in ((15, False, True), (15, True, False), (25, True, False)):
            with self.subTest(kind=kind, include_m=include_m, projected=projected):
                target = self.dimensional_shp(kind=kind, include_m=include_m, projected=projected)
                result = read_aoi(target)
                self.assertEqual(result["featureCount"], 2)
                for actual, expected in zip(result["bounds"], [102.7, 29.5, 105.5, 31.5]):
                    self.assertAlmostEqual(actual, expected, places=6)

    def test_truncated_polygonz_and_invalid_ring_indices_are_not_silently_accepted(self):
        target = self.dimensional_shp(include_m=False)
        original = target.read_bytes()
        target.write_bytes(original[:-8])
        with self.assertRaisesRegex(AOIError, "长度|完整"):
            read_aoi(target)
        corrupt = bytearray(original)
        struct.pack_into("<i", corrupt, 100 + 8 + 44, 2)  # First part must start at vertex zero.
        target.write_bytes(corrupt)
        with self.assertRaisesRegex(AOIError, "索引"):
            read_aoi(target)

    def test_shp_without_projection_has_actionable_error(self):
        target = self.folder / "glacier.shp"
        shutil.copyfile(ROOT / "tests" / "fixtures" / "aoi.shp", target)
        with self.assertRaisesRegex(AOIError, r"glacier\.prj"):
            read_aoi(target)

    def test_kml_keeps_polygon_holes_and_ignores_altitude(self):
        target = self.folder / "范围.kml"
        target.write_bytes(KML)
        result = read_aoi(target)
        self.assertEqual(result["format"], "KML")
        self.assertEqual(result["bounds"], [102.7, 29.5, 104.5, 31.5])
        rings = result["geometry"]["features"][0]["geometry"]["coordinates"]
        self.assertEqual(len(rings), 2)
        self.assertEqual(len(rings[0][0]), 2)

    def test_kmz_reads_nested_kml_without_extracting_files(self):
        target = self.folder / "范围.kmz"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("folder/doc.kml", KML)
            archive.writestr("images/preview.txt", "ignored")
        result = read_aoi(target)
        self.assertEqual(result["format"], "KMZ")
        self.assertEqual(result["featureCount"], 1)
        self.assertEqual(result["bounds"], [102.7, 29.5, 104.5, 31.5])
        self.assertFalse((self.folder / "folder").exists())

    def test_non_polygon_kml_malformed_xml_and_dtd_are_rejected(self):
        target = self.folder / "bad.kml"
        for data in (b'<kml><Point><coordinates>103,30</coordinates></Point></kml>', b'<kml><Polygon>', b'<!DOCTYPE kml [<!ENTITY a "abc">]><kml/>'):
            target.write_bytes(data)
            with self.assertRaises(AOIError):
                read_aoi(target)

    def test_shp_zip_is_no_longer_an_input_format(self):
        with self.assertRaisesRegex(AOIError, r"\.shp.*\.kml.*\.kmz"):
            read_aoi(ROOT / "tests" / "fixtures" / "aoi-valid.zip")

    def test_geojson_remains_supported(self):
        self.assertEqual(read_aoi(ROOT / "tests" / "fixtures" / "aoi.geojson")["bounds"], [102.7, 29.5, 104.5, 31.5])


class AOIEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(ROOT / "web")))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.opener = build_opener(ProxyHandler({}))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def request(self, body=b'{}', origin=None):
        return Request(self.base + "/api/aoi/open", data=body, headers={"Content-Type": "application/json", "Origin": origin or self.base}, method="POST")

    def test_picker_result_and_cancel_are_returned(self):
        for result in (read_aoi(ROOT / "tests" / "fixtures" / "aoi.shp"), {"cancelled": True}):
            with patch("server.choose_aoi", return_value=result) as picker:
                with self.opener.open(self.request(), timeout=5) as response:
                    self.assertEqual(json.load(response), result)
                picker.assert_called_once_with()

    def test_foreign_origin_or_direct_path_is_rejected_before_picker(self):
        for request, status in ((self.request(origin="https://example.org"), 403), (self.request(body=b'{"path":"C:/arbitrary.shp"}'), 400)):
            with patch("server.choose_aoi") as picker:
                with self.assertRaises(HTTPError) as caught:
                    self.opener.open(request, timeout=5)
                self.assertEqual(caught.exception.code, status)
                picker.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
