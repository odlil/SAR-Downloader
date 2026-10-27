"""ASF public catalog adapter; only metadata is requested, never image assets."""
from __future__ import annotations

from datetime import date, datetime, timezone
import json
import math
import re
import socket
import ssl
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, unquote
from urllib.request import Request, urlopen

from aoi_import import AOIError, geometry_bounds
from https_runtime import https_context

ENDPOINT = "https://api.daac.asf.alaska.edu/services/search/param"
MAX_RESPONSE = 32 * 1024 * 1024
DATASETS = {
    "SENTINEL-1": ("SENTINEL-1", {"SLC": "SLC", "GRD": "GRD_HD,GRD_HS,GRD_MD,GRD_MS,GRD_FD"}),
    "SLC-BURST": ("SLC-BURST", {"BURST": "BURST"}),
    "NISAR": ("NISAR", {key: key for key in ("RSLC", "GSLC", "GCOV", "GUNW")}),
    "ALOS": ("ALOS PALSAR", {"L1.1": "L1.1", "L1.0": "L1.0", "L1.5": "L1.5", "RTC": "RTC_HI_RES,RTC_LOW_RES",
                             "RTC_HI_RES": "RTC_HI_RES", "RTC_LOW_RES": "RTC_LOW_RES"}),
    "ALOS-2": ("ALOS-2", {"L1.1": "L1.1"}),
}


class SearchError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def number_filter(value, minimum=0, maximum=99999):
    value = str(value or "").strip().replace("，", ",").replace("–", "-").replace("—", "-")
    value = re.sub(r"\s+", "", value)
    if not value:
        return ""
    if len(value) > 160 or not re.fullmatch(r"\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*", value):
        raise SearchError("Path / Frame 请输入整数、范围或逗号列表。")
    for item in value.split(","):
        numbers = list(map(int, item.split("-")))
        if not minimum <= numbers[0] <= numbers[-1] <= maximum:
            raise SearchError("Path / Frame 编号超出范围或起止顺序错误。")
    return value


def _day(value, end=False):
    if value in (None, ""):
        return None
    try:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError()
        day = date.fromisoformat(value)
    except ValueError as error:
        raise SearchError("检索日期无效，请使用 YYYY-MM-DD。") from error
    return day.isoformat() + ("T23:59:59.999Z" if end else "T00:00:00Z")


def prepare_query(payload):
    if not isinstance(payload, dict):
        raise SearchError("检索参数需要 JSON 对象。")
    dataset = payload.get("dataset")
    if not isinstance(dataset, str) or dataset not in DATASETS:
        raise SearchError("不支持的数据集。")
    api_dataset, products = DATASETS[dataset]
    product = payload.get("product")
    if not isinstance(product, str) or product not in products:
        raise SearchError("该数据集不支持所选产品类型。")
    limit = payload.get("limit", 200)
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise SearchError("结果上限应为 1–1000 的整数。")
    start, end = _day(payload.get("start")), _day(payload.get("end"), True)
    if start and end and start > end:
        raise SearchError("开始日期不能晚于结束日期。")
    params = {"dataset": api_dataset, "processingLevel": products[product],
              "output": "geojson", "maxResults": limit + 1}
    if start:
        params["start"] = start
    if end:
        params["end"] = end
    path = number_filter(payload.get("path"), 1, 207 if dataset == "ALOS-2" else 175 if dataset.startswith("S") else 99999)
    if path:
        params["relativeOrbit"] = path
    scheme = payload.get("frameScheme", "nisar" if dataset == "NISAR" else "asf")
    allowed_schemes = ("nisar",) if dataset == "NISAR" else ("asf",) if dataset.startswith("ALOS") else ("asf", "esa")
    if scheme not in allowed_schemes:
        raise SearchError("帧号体系与数据集不匹配。")
    frame = number_filter(payload.get("frame"))
    if frame:
        if dataset == "SLC-BURST":
            raise SearchError("S1 Bursts 使用 Full Burst ID，不使用 Frame 筛选。")
        params["frame" if scheme in ("esa", "nisar") else "asfframe"] = frame
    direction = payload.get("direction")
    if direction:
        if direction not in ("ASCENDING", "DESCENDING"):
            raise SearchError("轨道方向无效。")
        params["flightDirection"] = direction
    polarization = payload.get("polarization")
    if polarization:
        options = {"VV", "VH", "HH", "HV", "VV+VH", "HH+HV", "HH+VV", "HH+HV+VH+VV", "QUADRATURE"}
        if polarization not in options or (dataset == "SLC-BURST" and polarization not in {"VV", "VH", "HH", "HV"}):
            raise SearchError("极化类型与数据集不匹配。")
        params["mainBandPolarization" if dataset == "NISAR" else "polarization"] = polarization
    burst_id = payload.get("burstID") or ""
    if not isinstance(burst_id, str):
        raise SearchError("Full Burst ID 格式无效。")
    burst_id = burst_id.strip().upper().replace("，", ",").replace(" ", "")
    if burst_id:
        if dataset != "SLC-BURST" or len(burst_id) > 1024 or not all(
                re.fullmatch(r"\d{3}_\d{1,8}_(?:IW[1-3]|EW[1-5])", item) for item in burst_id.split(",")):
            raise SearchError("Full Burst ID 例如 114_242969_IW3；多个编号用逗号分隔。")
        params["fullBurstID"] = burst_id
    try:
        geometry_bounds(payload.get("aoi"))
        from shapely.geometry import shape
        from shapely.errors import ShapelyError
        from shapely.ops import unary_union
        aoi = payload["aoi"]
        if aoi["type"] == "FeatureCollection":
            area = unary_union([shape(feature["geometry"]) for feature in aoi["features"]])
        else:
            area = shape(aoi["geometry"] if aoi["type"] == "Feature" else aoi)
        if area.is_empty or not area.is_valid or area.geom_type not in ("Polygon", "MultiPolygon"):
            raise SearchError("研究区面几何无效，请先在 GIS 软件中修复几何。")
        # ASF accepts a single outer polygon and ignores holes. Query a covering
        # hull, then intersect every returned footprint with the complete AOI.
        covering = area.convex_hull
        if len(covering.exterior.coords) > 400:
            covering = area.envelope
        params["intersectsWith"] = covering.wkt
    except ImportError as error:
        raise SearchError("真实检索需要 Shapely，请运行首次配置.cmd 安装依赖。", 503) from error
    except (AOIError, KeyError, TypeError, AttributeError, ValueError) as error:
        if isinstance(error, SearchError):
            raise
        raise SearchError(f"研究区无效：{error}") from error
    except ShapelyError as error:
        raise SearchError("研究区几何无法合并，请先在 GIS 软件中修复几何。") from error
    return params, area, limit


def _request(params, *, endpoint=ENDPOINT, items=False):
    request = Request(endpoint, data=urlencode(params, doseq=True).encode("utf-8"), headers={
        "User-Agent": "SAR-Workbench/0.2 (local catalog search)", "Accept": "application/geo+json, application/json"})
    try:
        with _open_catalog(request) as response:
            raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise SearchError("ASF 返回内容过大，请降低结果上限或缩小检索条件。", 502)
        result = json.loads(raw)
        key = "items" if items else "features"
        if not isinstance(result, dict) or (not items and result.get("type") != "FeatureCollection") or not isinstance(result.get(key), list):
            raise SearchError("ASF 返回格式异常，请稍后重试。", 502)
        return result[key]
    except HTTPError as error:
        if error.code in (500, 502, 503, 504):
            error.close()
            raise SearchError(f"ASF 检索服务暂时不可用（上游 HTTP {error.code}），请稍后重试。",
                              504 if error.code == 504 else 502) from error
        try:
            detail = json.loads(error.read(4096))
            report = detail.get("error", {})
            report = report.get("report", "") if isinstance(report, dict) else str(report)
            if isinstance(detail.get('errors'), list):
                report = '；'.join(map(str, detail['errors']))
            report = str(report or "")
        except (ValueError, AttributeError):
            report = ""
        finally:
            error.close()
        nested_status = re.search(r'\bHTTP\s+(5\d{2})\b', report)
        if error.code == 400 and nested_status:
            upstream = int(nested_status.group(1))
            raise SearchError(f"ASF 上游目录服务异常（ASF HTTP 400，上游 HTTP {upstream}），请稍后重试。",
                              504 if upstream == 504 else 502) from error
        if error.code == 429:
            raise SearchError("ASF 请求频率受限，请稍后再检索。", 429) from error
        raise SearchError(f"ASF 检索失败（HTTP {error.code}）" + (f"：{report[:300]}" if report else "，请稍后重试。"), 502) from error
    except (TimeoutError, socket.timeout) as error:
        raise SearchError("连接 ASF 超时，请检查网络后重试，或缩小研究区及日期范围。", 504) from error
    except URLError as error:
        if isinstance(error.reason, ssl.SSLCertVerificationError):
            raise SearchError("ASF HTTPS 证书验证失败，请检查系统时间；使用代理自签证书时，请配置受信任的 CA 证书。", 502) from error
        if isinstance(error.reason, TimeoutError):
            raise SearchError("连接 ASF 超时，请稍后重试，或检查网络及代理是否可用。", 504) from error
        raise SearchError("无法建立 ASF 连接，请检查网络或当前代理设置。", 502) from error
    except OSError as error:
        raise SearchError("无法加载 HTTPS CA 证书，请完整解压程序；自定义证书路径需要指向有效的 PEM 文件。", 502) from error
    except (ValueError, UnicodeError) as error:
        if isinstance(error, SearchError):
            raise
        raise SearchError("ASF 返回的内容无法解析，请稍后重试。", 502) from error


def _open_catalog(request):
    """Keep the original connection behavior; only frozen Linux needs a CA fix."""
    options = {'timeout': 45}
    if sys.platform == 'linux' and getattr(sys, 'frozen', False):
        options['context'] = https_context()
    return urlopen(request, **options)


def safe_url(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password:
            return value
    except ValueError:
        pass
    return None


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, TypeError):
        return None


def normalize_scene(feature, dataset):
    p = feature.get("properties") or {}
    identifier = p.get("fileID") or p.get("sceneName")
    geometry = feature.get("geometry")
    if not identifier or not isinstance(geometry, dict) or geometry.get("type") not in ("Polygon", "MultiPolygon"):
        raise ValueError("missing product identifier/footprint")
    from shapely.geometry import shape
    footprint = shape(geometry)
    if footprint.is_empty or not footprint.is_valid:
        raise ValueError("invalid footprint")
    start = str(p.get("startTime") or "")
    datetime.fromisoformat(start.replace("Z", "+00:00"))
    url = safe_url(p.get("url"))
    filename = p.get("fileName")
    if not filename and url:
        filename = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    size = p.get("bytes")
    if isinstance(size, dict):
        # NISAR lists sizes for the HDF5 plus all supplementary files; report
        # the primary asset only, not the sum of previews and QA products.
        entry = size.get(filename, {})
        size = entry.get("bytes") if isinstance(entry, dict) else entry
    polarization = p.get("mainBandPolarization") if dataset == "NISAR" else p.get("polarization")
    if isinstance(polarization, list):
        polarization = "+".join(map(str, polarization))
    burst = p.get("burst") or {}
    if not isinstance(burst, dict):
        burst = {}
    additional = p.get("additionalUrls") or []
    if not isinstance(additional, list):
        additional = []
    xml_urls = [value for value in additional if safe_url(value) and urlsplit(value).path.lower().endswith(".xml")]
    platform = re.sub(r"SENTINEL[- ]?1", "S1", str(p.get("platform") or dataset).upper()).replace("-", "")
    frame = _number(p.get("frameNumber"))
    frame = int(frame) if frame is not None else None
    scene = {"id": str(identifier), "demo": False, "dataset": dataset, "platform": platform,
             "name": str(p.get("sceneName") or identifier), "date": start[:10], "time": start[11:19],
             "startTime": start, "stopTime": p.get("stopTime"), "product": p.get("processingLevel"),
             "polarization": polarization or "—", "direction": p.get("flightDirection"),
             "path": _number(p.get("pathNumber")), "asfFrame": frame if dataset not in ("NISAR", "SLC-BURST") else None,
             "esaFrame": None, "frame": frame, "size": _number(size), "url": url,
             "filename": filename, "xmlUrls": xml_urls, "geometry": geometry, "bounds": list(footprint.bounds),
             "fullBurstID": burst.get("fullBurstID"), "subswath": burst.get("subswath"),
             "relativeBurstID": burst.get("relativeBurstID"), "absoluteBurstID": burst.get("absoluteBurstID"),
             "orbit": p.get("orbit"), "beamMode": p.get("beamModeType"), "source": "ASF"}
    return scene, footprint


def search_asf(payload):
    params, area, limit = prepare_query(payload)
    from shapely.errors import ShapelyError
    if payload['dataset'] == 'ALOS-2':
        from alos2_catalog import cmr_query, normalize_granule, matches_filters
        entries = _request(cmr_query(params, area), endpoint='https://cmr.earthdata.nasa.gov/search/granules.umm_json', items=True)
        features = []
        for entry in entries:
            try:
                features.append(normalize_granule(entry))
            except (ValueError, KeyError, TypeError, IndexError, AttributeError):
                features.append({})  # Count malformed/foreign records using the shared warning path.
    else:
        features = _request(params)
    scenes, seen, skipped = [], set(), 0
    for feature in features:
        try:
            scene, footprint = normalize_scene(feature, payload["dataset"])
            if payload['dataset'] == 'ALOS-2' and not matches_filters(scene, payload):
                continue
            if scene["id"] not in seen and footprint.intersects(area):
                seen.add(scene["id"])
                scenes.append(scene)
        except (ValueError, TypeError, KeyError, AttributeError, ShapelyError):
            skipped += 1
    scenes.sort(key=lambda scene: (scene["startTime"], scene["id"]), reverse=True)
    warnings = []
    truncated = len(features) >= limit + 1 or len(scenes) > limit
    if truncated:
        warnings.append("候选结果达到上限，当前列表可能不完整；请提高上限，或缩小研究区及日期范围。")
    if skipped:
        warnings.append(f"ASF 返回的 {skipped} 条记录缺少有效时间或边界，已跳过。")
    return {"provider": "ASF", "scenes": scenes[:limit], "count": min(len(scenes), limit), "limit": limit,
            "truncated": truncated, "warnings": warnings,
            "queriedAt": datetime.now(timezone.utc).isoformat(), "candidateCount": len(features)}
