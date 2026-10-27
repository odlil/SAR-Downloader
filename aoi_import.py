"""Read local AOI files. Heavy GIS imports are deferred until SHP is selected."""
from __future__ import annotations

from contextlib import ExitStack
import io
import json
import math
from pathlib import Path
import struct
import xml.etree.ElementTree as ET
import zipfile

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_VERTICES = 25000
MAX_FEATURES = 2000
SUPPORTED = {".shp", ".kml", ".kmz", ".geojson", ".json"}


class AOIError(ValueError):
    pass


def _check_size(path: Path):
    if path.stat().st_size > MAX_FILE_BYTES:
        raise AOIError("范围文件超过 10 MB，请先简化矢量。")


def _sidecar(path: Path, extension: str):
    name = path.with_suffix(extension).name.casefold()
    return next((item for item in path.parent.iterdir() if item.is_file() and item.name.casefold() == name), None)


def _map_coordinates(coordinates, transform):
    if not isinstance(coordinates, (list, tuple)) or not coordinates:
        raise AOIError("范围坐标为空或格式无效。")
    if isinstance(coordinates[0], (float, int)):
        if len(coordinates) < 2:
            raise AOIError("范围坐标缺少经度或纬度。")
        x, y = transform(float(coordinates[0]), float(coordinates[1]))
        return [x, y]
    return [_map_coordinates(item, transform) for item in coordinates]


def _shp_xy_source(path: Path):
    """Project PolygonZ/M records to XY in memory; never rewrite the user's files.

    pyshp 3.0.2 raises UnboundLocalError for a valid PolygonZ without optional M.
    AOIs only need XY. Retain the original ring/part structure and let pyshp do
    the polygon/hole grouping, while bypassing Z/M decoding for these types.
    """
    with path.open("rb") as source:
        header = source.read(100)
        if len(header) < 100:
            raise AOIError("SHP 文件头不完整，请检查文件是否被截断。")
        shape_type = struct.unpack_from("<i", header, 32)[0]
        if shape_type not in (15, 25):
            return None
        data = header + source.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise AOIError("范围文件超过 10 MB，请先简化矢量。")
    if (struct.unpack_from(">i", header)[0] != 9994
            or struct.unpack_from("<i", header, 28)[0] != 1000
            or struct.unpack_from(">i", header, 24)[0] * 2 != len(data)):
        raise AOIError("SHP 文件头或声明长度与实际内容不符，请检查文件是否完整。")
    output = bytearray(header)
    struct.pack_into("<i", output, 32, 5)
    output[68:100] = bytes(32)  # Z/M bounds are unused in a 2D Polygon header.
    offset, vertices, features = 100, 0, 0
    while offset < len(data):
        if len(data) - offset < 8:
            raise AOIError("SHP 记录头不完整，请检查文件是否被截断。")
        record, words = struct.unpack_from(">ii", data, offset)
        end = offset + 8 + words * 2
        if words < 2 or end > len(data):
            raise AOIError("SHP 几何记录不完整，请检查文件是否被截断。")
        content = data[offset + 8:end]
        kind = struct.unpack_from("<i", content)[0]
        if kind == 0:
            xy = content[:4]
        else:
            if kind != shape_type or len(content) < 44:
                raise AOIError("SHP 面几何类型或记录结构无效。")
            parts, points = struct.unpack_from("<ii", content, 36)
            vertices += points
            features += 1
            if vertices > MAX_VERTICES or features > MAX_FEATURES:
                raise AOIError("范围过于复杂，请简化到 25,000 个顶点、2,000 个面以内。")
            xy_end = 44 + 4 * parts + 16 * points
            required_end = xy_end + (16 + 8 * points if kind == 15 else 0)
            if parts < 1 or points < 4 or parts > points or len(content) < required_end:
                raise AOIError("SHP 面顶点或高程记录不完整，请检查文件是否被截断。")
            starts = struct.unpack_from(f"<{parts}i", content, 44)
            if starts[0] != 0 or any(a >= b for a, b in zip(starts, starts[1:])) or starts[-1] >= points:
                raise AOIError("SHP 面环索引无效，请检查几何数据。")
            xy = struct.pack("<i", 5) + content[4:xy_end]
        output.extend(struct.pack(">ii", record, len(xy) // 2))
        output.extend(xy)
        offset = end
    struct.pack_into(">i", output, 24, len(output) // 2)
    return io.BytesIO(output)


def _read_shp(path: Path):
    projection = _sidecar(path, ".prj")
    if projection is None:
        raise AOIError(f"缺少同目录的 {path.stem}.prj，无法确认坐标系。请将同名投影文件放到 SHP 旁边，无需打包。")
    if projection.stat().st_size > 1024 * 1024:
        raise AOIError(".prj 文件过大，请检查投影文件。")
    try:
        import shapefile
        from pyproj import CRS, Transformer
        from pyproj.exceptions import ProjError
    except ImportError as error:
        raise AOIError("读取 SHP 需要 pyshp 和 pyproj，请运行首次配置.cmd 安装依赖。") from error
    try:
        wkt = projection.read_text(encoding="utf-8-sig")
        source_crs = CRS.from_wkt(wkt)
        transformer = Transformer.from_crs(source_crs, CRS.from_epsg(4326), always_xy=True)
    except Exception as error:
        raise AOIError("无法识别 .prj 投影信息，请检查投影文件。") from error
    features = []
    # Only shapes are needed for the AOI; DBF attributes need not be loaded.
    try:
        with ExitStack() as stack:
            xy_source = _shp_xy_source(path)
            options = {"shp": stack.enter_context(xy_source) if xy_source is not None else str(path)}
            # Normalized records have different offsets; the original SHX cannot be reused.
            if xy_source is None and (index := _sidecar(path, ".shx")):
                options["shx"] = str(index)
            reader = stack.enter_context(shapefile.Reader(**options))
            count = 0
            for shape in reader.iterShapes():
                if shape.shapeType == 0:
                    continue
                if shape.shapeType not in (5, 15, 25):
                    raise AOIError("研究区需要面要素，当前 SHP 包含点或线。")
                count += len(shape.points)
                if count > MAX_VERTICES or len(features) >= MAX_FEATURES:
                    raise AOIError("范围过于复杂，请简化到 25,000 个顶点、2,000 个面以内。")
                geometry = shape.__geo_interface__
                geometry["coordinates"] = _map_coordinates(
                    geometry["coordinates"], lambda x, y: transformer.transform(x, y, errcheck=True)
                )
                features.append({"type": "Feature", "properties": {}, "geometry": geometry})
    except AOIError:
        raise
    except ProjError as error:
        raise AOIError("SHP 坐标转换失败：坐标值可能超出 .prj 声明的坐标系范围，请核对坐标系。") from error
    except Exception as error:
        raise AOIError(f"SHP 面几何读取失败（{type(error).__name__}），请检查 SHP / SHX 文件是否完整。") from error
    return {"type": "FeatureCollection", "features": features}


def _tag(element):
    return element.tag.rsplit("}", 1)[-1] if isinstance(element.tag, str) else ""


class _NoDoctype(ET.TreeBuilder):
    def doctype(self, name, pubid, system):
        raise AOIError("KML 不支持 DTD 或外部实体，请使用普通 KML 面文件。")


def _kml_ring(boundary):
    coordinates = next((element for element in boundary.iter() if _tag(element) == "coordinates"), None)
    if coordinates is None or not coordinates.text:
        raise AOIError("KML 面缺少坐标。")
    ring = []
    try:
        for item in coordinates.text.split():
            values = item.split(",")
            if len(values) < 2:
                raise ValueError("missing coordinate")
            ring.append([float(values[0]), float(values[1])])
            if len(ring) > MAX_VERTICES:
                raise AOIError("KML 顶点超过 25,000，请先简化。")
    except AOIError:
        raise
    except (ValueError, IndexError) as error:
        raise AOIError("KML 坐标格式无效，应为 经度,纬度[,高程]。") from error
    if ring and ring[0] != ring[-1]:
        ring.append(ring[0].copy())
    if len(ring) < 4 or len({tuple(point) for point in ring}) < 3:
        raise AOIError("KML 面至少需要三个不同的顶点。")
    return ring


def _read_kml(data: bytes):
    if len(data) > MAX_FILE_BYTES:
        raise AOIError("KML 超过 10 MB，请先简化。")
    try:
        root = ET.fromstring(data, parser=ET.XMLParser(target=_NoDoctype()))
    except ET.ParseError as error:
        raise AOIError("KML XML 格式无效，请检查文件。") from error
    features = []
    vertex_count = 0
    for polygon in root.iter():
        if _tag(polygon) != "Polygon":
            continue
        outer = next((item for item in polygon if _tag(item) == "outerBoundaryIs"), None)
        if outer is None:
            raise AOIError("KML 面缺少外边界 outerBoundaryIs。")
        rings = [_kml_ring(outer)]
        rings.extend(_kml_ring(item) for item in polygon if _tag(item) == "innerBoundaryIs")
        vertex_count += sum(len(ring) for ring in rings)
        if vertex_count > MAX_VERTICES:
            raise AOIError("KML 顶点超过 25,000，请先简化。")
        features.append({"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": rings}})
        if len(features) > MAX_FEATURES:
            raise AOIError("KML 面要素超过 2,000，请先简化。")
    return features


def _read_kmz(path: Path):
    try:
        with zipfile.ZipFile(path) as archive:
            items = archive.infolist()
            if len(items) > 200 or sum(item.file_size for item in items) > MAX_ARCHIVE_BYTES:
                raise AOIError("KMZ 内容过大，请简化到 200 个文件、解压后 64 MB 以内。")
            documents = [item for item in items if not item.is_dir() and item.filename.lower().endswith(".kml")]
            if not documents:
                raise AOIError("KMZ 中没有 KML 文件。")
            features = []
            for item in documents:
                if item.flag_bits & 1:
                    raise AOIError("不支持加密的 KMZ。")
                if item.file_size > MAX_FILE_BYTES:
                    raise AOIError("KMZ 中的 KML 超过 10 MB，请先简化。")
                # Read in memory. Archive paths are never extracted to disk.
                with archive.open(item) as stream:
                    data = stream.read(MAX_FILE_BYTES + 1)
                features.extend(_read_kml(data))
                if len(features) > MAX_FEATURES:
                    raise AOIError("KMZ 面要素超过 2,000，请先简化。")
                if features:
                    geometry_bounds({"type": "FeatureCollection", "features": features})
            return {"type": "FeatureCollection", "features": features}
    except (zipfile.BadZipFile, RuntimeError) as error:
        raise AOIError("KMZ 无法读取，请检查文件是否完整。") from error


def geometry_bounds(geometry):
    bounds = [math.inf, math.inf, -math.inf, -math.inf]
    vertices = 0
    features = 0

    def visit_coordinates(coordinates):
        nonlocal vertices
        if not isinstance(coordinates, (list, tuple)) or not coordinates:
            raise AOIError("面几何坐标为空或格式无效。")
        if isinstance(coordinates[0], (int, float)):
            if len(coordinates) < 2:
                raise AOIError("面几何缺少经度或纬度。")
            x, y = coordinates[:2]
            if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in (x, y)):
                raise AOIError("坐标包含无效数值。")
            if not (-180 <= x <= 180 and -85 <= y <= 85):
                raise AOIError("坐标不在支持的经纬度范围内，请确认投影。当前地图支持纬度 ±85°。")
            vertices += 1
            if vertices > MAX_VERTICES:
                raise AOIError("范围顶点超过 25,000，请先简化。")
            bounds[0] = min(bounds[0], x)
            bounds[1] = min(bounds[1], y)
            bounds[2] = max(bounds[2], x)
            bounds[3] = max(bounds[3], y)
        else:
            for item in coordinates:
                visit_coordinates(item)

    def visit(item):
        nonlocal features
        if not isinstance(item, dict):
            raise AOIError("范围几何格式无效。")
        kind = item.get("type")
        if kind == "FeatureCollection":
            children = item.get("features")
            if not isinstance(children, list):
                raise AOIError("面要素列表无效。")
            for child in children:
                visit(child)
        elif kind == "Feature":
            visit(item.get("geometry"))
        elif kind in ("Polygon", "MultiPolygon"):
            features += 1
            if features > MAX_FEATURES:
                raise AOIError("范围面要素超过 2,000，请先简化。")
            visit_coordinates(item.get("coordinates"))
        else:
            raise AOIError("研究区仅支持面要素 Polygon / MultiPolygon。")

    visit(geometry)
    if not vertices or bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
        raise AOIError("未发现有效面范围。KML/KMZ 中需包含面，网络链接不会自动下载。")
    return bounds, features


def read_aoi(filename):
    path = Path(filename)
    extension = path.suffix.lower()
    if extension not in SUPPORTED:
        raise AOIError("请选择 .shp、.kml、.kmz 或 GeoJSON 文件；不再导入 SHP ZIP。")
    if not path.is_file():
        raise AOIError("所选范围文件不存在或无法访问。")
    _check_size(path)
    if extension == ".shp":
        geometry = _read_shp(path)
    elif extension == ".kml":
        geometry = {"type": "FeatureCollection", "features": _read_kml(path.read_bytes())}
    elif extension == ".kmz":
        geometry = _read_kmz(path)
    else:
        try:
            geometry = json.loads(path.read_text(encoding="utf-8-sig"))
        except (ValueError, UnicodeError) as error:
            raise AOIError("GeoJSON 格式无效，请使用 UTF-8 编码。") from error
        if not isinstance(geometry, dict):
            raise AOIError("GeoJSON 顶层需要几何对象或面要素集合。")
        if geometry.get("crs") and not any(value in str(geometry["crs"]).upper() for value in ("4326", "CRS84")):
            raise AOIError("GeoJSON 请先转换为 WGS84 / EPSG:4326。")
    bounds, count = geometry_bounds(geometry)
    return {"filename": path.name, "format": extension[1:].upper(), "geometry": geometry,
            "bounds": bounds, "featureCount": count, "crs": "EPSG:4326"}
