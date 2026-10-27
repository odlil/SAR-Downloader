"""Generate local AOI fixtures with pyshp in the existing gdal environment."""
from pathlib import Path
import json
import zipfile
import shapefile

output = Path(__file__).resolve().parent / "fixtures"
output.mkdir(exist_ok=True)
stem = output / "aoi"
with shapefile.Writer(str(stem), shapeType=shapefile.POLYGON) as writer:
    writer.field("name", "C")
    writer.poly([[[102.7, 29.5], [102.7, 31.5], [104.5, 31.5], [104.5, 29.5], [102.7, 29.5]]])
    writer.record("local-test")
stem.with_suffix(".prj").write_text('GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]', encoding="ascii")
for name, extensions in [("aoi-valid.zip", [".shp", ".shx", ".dbf", ".prj"]), ("aoi-missing-crs.zip", [".shp", ".shx", ".dbf"])]:
    with zipfile.ZipFile(output / name, "w", zipfile.ZIP_DEFLATED) as archive:
        for extension in extensions:
            item = stem.with_suffix(extension)
            archive.write(item, item.name)
(output / "aoi.geojson").write_text(json.dumps({"type":"Polygon","coordinates":[[[102.7,29.5],[104.5,29.5],[104.5,31.5],[102.7,31.5],[102.7,29.5]]]}),encoding="utf-8")
kml = '<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><name>AOI</name><Polygon><outerBoundaryIs><LinearRing><coordinates>102.7,29.5,0 104.5,29.5,0 104.5,31.5,0 102.7,31.5,0 102.7,29.5,0</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark></kml>'
(output / "aoi.kml").write_text(kml, encoding="utf-8")
with zipfile.ZipFile(output / "aoi.kmz", "w", zipfile.ZIP_DEFLATED) as archive:
    archive.writestr("doc.kml", kml)
print("Local fixtures generated:", output)
