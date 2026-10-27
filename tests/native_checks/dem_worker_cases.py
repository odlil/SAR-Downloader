"""Exercise real sardem/GDAL on tiny adjacent rasters, without network."""
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import dem_worker
try:
    GDAL, OSR, _, _ = dem_worker.engine()
except (RuntimeError, ImportError):
    GDAL=OSR=None


@unittest.skipIf(GDAL is None,'DEM engine not configured')
class RasterTests(unittest.TestCase):
    def test_mosaic_crop_zero_height_and_relocatable_isce_companions(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'tests') as folder:
            root=Path(folder);step=1/3600;files=[]
            for index,height in enumerate((0.,100.)):
                file=root/f'tile{index}.tif';files.append(file)
                raster=GDAL.GetDriverByName('GTiff').Create(str(file),10,10,1,GDAL.GDT_Float32)
                raster.SetGeoTransform((86+index*10*step,step,0,43.01,0,-step))
                srs=OSR.SpatialReference();srs.ImportFromEPSG(4326);raster.SetProjection(srs.ExportToWkt())
                raster.GetRasterBand(1).WriteRaster(0,0,10,10,struct.pack('100f',*([height]*100)))
                raster.FlushCache();raster=None
            plan={'paddedBounds':[86+2*step,43.01-8*step,86+18*step,43.01-2*step], 'height':'egm2008'}
            for format in ('GTiff','ISCE'):
                stage=root/format/'result'
                output,_=dem_worker.create_product({**plan,'format':format},files,stage)
                moved=root/('published-'+format);stage.rename(moved)
                ds=GDAL.Open(str(moved/output.name));band=ds.GetRasterBand(1)
                samples=struct.unpack(f'{ds.RasterXSize*ds.RasterYSize}f',band.ReadRaster(buf_type=GDAL.GDT_Float32))
                self.assertIn(0.,samples);self.assertIn(100.,samples)
                self.assertLess(ds.RasterXSize,20);self.assertEqual(band.GetNoDataValue(),-32767)
                self.assertEqual(ds.GetMetadataItem('VERTICAL_DATUM'),'EGM2008');ds=None
                if format=='ISCE':
                    self.assertTrue((moved/'elevation.dem.xml').is_file())
                    self.assertEqual(ET.parse(moved/'elevation.dem.xml').find(".//property[@name='REFERENCE']/value").text,'EGM2008')
                    vrt=GDAL.Open(str(moved/'elevation.dem.vrt'))
                    actual=GDAL.Open(str(moved/output.name))
                    self.assertEqual(vrt.GetRasterBand(1).Checksum(),actual.GetRasterBand(1).Checksum())
                    vrt=actual=None
            with self.assertRaisesRegex(RuntimeError,'EGM2008'):
                dem_worker.create_product({**plan,'format':'GTiff','height':'ellipsoid'},files,root/'bad')


if __name__=='__main__':unittest.main()
