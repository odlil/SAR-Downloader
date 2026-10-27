from copy import deepcopy
from functools import partial
import io
import json
from pathlib import Path
import socket
import sys
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import build_opener, ProxyHandler, Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from asf_api import prepare_query, normalize_scene, search_asf, SearchError, _request
from server import Handler, ThreadingHTTPServer


def polygon(w=0, s=0, e=4, n=4):
    return {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}


def payload(**changes):
    return {"dataset": "SENTINEL-1", "product": "SLC", "start": "2024-01-01", "end": "2024-01-31",
            "frameScheme": "asf", "aoi": polygon(), "limit": 100, **changes}


def feature(identifier="scene-1", geometry=None, **properties):
    return {"type": "Feature", "geometry": geometry or polygon(), "properties": {
        "fileID": identifier, "sceneName": identifier, "platform": "Sentinel-1A", "pathNumber": 114,
        "frameNumber": 136, "processingLevel": "SLC", "startTime": "2024-01-30T12:10:07Z",
        "stopTime": "2024-01-30T12:10:34Z", "flightDirection": "ASCENDING", "polarization": "VV+VH",
        "bytes": "1000", "url": "https://datapool.asf.alaska.edu/example.zip", "fileName": "example.zip", **properties}}


class AdapterTests(unittest.TestCase):
    def test_alos_options_are_valid_for_the_reported_region(self):
        area = polygon(80.39279, 42.11068, 80.74108, 42.29353)
        for dataset, products in (("ALOS", ("L1.0", "L1.1", "L1.5", "RTC_HI_RES", "RTC_LOW_RES")),
                                  ("ALOS-2", ("L1.1",))):
            for product in products:
                with self.subTest(dataset=dataset, product=product):
                    query, _, _ = prepare_query(payload(dataset=dataset, product=product, aoi=area))
                    self.assertEqual(query['processingLevel'], product)

    def test_dataset_product_frame_and_utc_mappings(self):
        query, _, _ = prepare_query(payload(path="114,115-117", frame="136", polarization="VV+VH"))
        self.assertEqual(query["asfframe"], "136")
        self.assertEqual(query["relativeOrbit"], "114,115-117")
        self.assertEqual(query["end"], "2024-01-31T23:59:59.999Z")
        self.assertEqual(query["polarization"], "VV+VH")
        esa, _, _ = prepare_query(payload(frameScheme="esa", frame="136"))
        self.assertIn("frame", esa)
        self.assertNotIn("asfframe", esa)
        nisar, _, _ = prepare_query(payload(dataset="NISAR", product="RSLC", frameScheme="nisar", frame="20", polarization="HH+HV"))
        self.assertEqual(nisar["frame"], "20")
        self.assertEqual(nisar["mainBandPolarization"], "HH+HV")
        alos, _, _ = prepare_query(payload(dataset="ALOS", product="RTC"))
        self.assertEqual(alos["dataset"], "ALOS PALSAR")
        self.assertEqual(alos["processingLevel"], "RTC_HI_RES,RTC_LOW_RES")
        grd, _, _ = prepare_query(payload(product="GRD"))
        self.assertIn("GRD_HD", grd["processingLevel"])

    def test_burst_id_polarization_and_no_frame(self):
        query, _, _ = prepare_query(payload(dataset="SLC-BURST", product="BURST", burstID="114_242969_IW3", polarization="VV"))
        self.assertEqual(query["dataset"], "SLC-BURST")
        self.assertEqual(query["fullBurstID"], "114_242969_IW3")
        for changes in ({"frame": "10"}, {"polarization": "VV+VH"}, {"burstID": "bad"}):
            with self.assertRaises(SearchError):
                prepare_query(payload(dataset="SLC-BURST", product="BURST", **changes))

    def test_invalid_input_fails_before_network(self):
        for changes in ({"dataset": []}, {"product": {}}, {"start": "2024-02-30"}, {"start": "2025-01-01"},
                        {"path": "176"}, {"frame": "4-1"}, {"limit": True}, {"limit": 1001}, {"aoi": polygon(400000, 3000000, 500000, 4000000)}):
            with self.subTest(changes=changes), patch("asf_api._request") as request:
                with self.assertRaises(SearchError):
                    search_asf(payload(**changes))
                request.assert_not_called()

    def test_real_metadata_burst_xml_and_nisar_primary_size(self):
        burst = feature(processingLevel="BURST", url="https://sentinel1-burst.asf.alaska.edu/source/IW2/VV/1.tiff",
                        frameNumber=None, burst={"fullBurstID": "114_242969_IW2", "subswath": "IW2"},
                        additionalUrls=["https://sentinel1-burst.asf.alaska.edu/source/IW2/VV/1.xml", "javascript:alert(1)"])
        scene, _ = normalize_scene(burst, "SLC-BURST")
        self.assertEqual(scene["platform"], "S1A")
        self.assertEqual(len(scene["xmlUrls"]), 1)
        self.assertEqual(scene["fullBurstID"], "114_242969_IW2")
        self.assertIsNone(scene["asfFrame"])
        self.assertFalse(scene["demo"])
        self.assertIsNone(scene["esaFrame"])
        scene, _ = normalize_scene(feature(fileName="main.h5", bytes={"main.h5": {"bytes": 12000}, "preview.png": {"bytes": 9000}},
                                           mainBandPolarization=["HH", "HV"], frameNumber=20), "NISAR")
        self.assertEqual(scene["size"], 12000)
        self.assertEqual(scene["polarization"], "HH+HV")
        self.assertEqual(scene["frame"], 20)

    def test_multipart_and_holes_are_exactly_filtered_without_fake_rectangles(self):
        area = polygon()
        area["coordinates"].append(polygon(1, 1, 3, 3)["coordinates"][0])
        aoi = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": area}, {"type": "Feature", "geometry": polygon(10, 10, 11, 11)}]}
        accepted = feature("edge", polygon(0.1, 0.1, 0.5, 0.5))
        candidates = [accepted, deepcopy(accepted), feature("hole", polygon(1.5, 1.5, 2, 2)),
                      feature("gap", polygon(7, 7, 8, 8)), feature("second", polygon(10, 10, 11, 11))]
        with patch("asf_api._request", return_value=candidates):
            result = search_asf(payload(aoi=aoi))
        self.assertEqual({scene["id"] for scene in result["scenes"]}, {"edge", "second"})
        actual = next(scene for scene in result["scenes"] if scene["id"] == "edge")
        self.assertEqual(actual["geometry"], accepted["geometry"])

    def test_limits_and_bad_records_are_reported(self):
        candidates = [feature("a"), feature("b"), feature("bad", startTime="invalid")]
        with patch("asf_api._request", return_value=candidates):
            result = search_asf(payload(limit=1))
        self.assertEqual(result["count"], 1)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["warnings"]), 2)

    def test_upstream_failure_is_an_error_instead_of_empty_or_demo_results(self):
        errors = [socket.timeout(), URLError("offline"), HTTPError("https://asf/", 429, "limited", {}, io.BytesIO(b'{}'))]
        for error in errors:
            with self.subTest(error=error), patch("asf_api._open_catalog", side_effect=error):
                with self.assertRaises(SearchError):
                    _request({})


class SearchEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(ROOT / "web")))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.opener = build_opener(ProxyHandler({}))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(timeout=3)

    def test_local_search_result_and_upstream_error_status(self):
        for result, status in (({"provider": "ASF", "scenes": []}, 200), (SearchError("timeout", 504), 504)):
            with patch("server.search_asf", **({"side_effect": result} if isinstance(result, Exception) else {"return_value": result})):
                request = Request(self.base + "/api/search", data=json.dumps(payload()).encode(), headers={"Content-Type": "application/json", "Origin": self.base})
                try:
                    response = self.opener.open(request, timeout=3)
                except HTTPError as error:
                    response = error
                with response:
                    self.assertEqual(response.status, status)
                    self.assertIn("error" if status == 504 else "scenes", json.load(response))

    def test_foreign_origin_is_rejected_without_calling_asf(self):
        request = Request(self.base + "/api/search", data=b'{}', headers={"Content-Type": "application/json", "Origin": "https://example.org"})
        with patch("server.search_asf") as search:
            with self.assertRaises(HTTPError) as caught:
                self.opener.open(request, timeout=3)
            self.assertEqual(caught.exception.code, 403)
            search.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
