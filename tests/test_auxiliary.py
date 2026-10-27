"""Catalog semantics and anonymous assets; all upstream indexes are fixtures."""
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from auxiliary_api import (AuxiliaryError, OrbitLinks, dem_plan, match_orbits,
                           parse_scene, preview_dem, scan_scene_folder)
from dem_jobs import new_dem_job
from earthdata_auth import TransferError, validate_url
from public_assets import GEOID_URL, public_asset_kind

SCENE = 'S1A_IW_SLC__1SDV_20240130T121007_20240130T121034_052336_065405_7131'
EOF = 'S1A_OPER_AUX_POEORB_OPOD_20240219T070659_V20240129T225942_20240131T005942.EOF'
ORBIT_URL = 'https://s1qc.asf.alaska.edu/aux_poeorb/' + EOF
TILE = 'Copernicus_DSM_COG_10_N43_00_E086_00_DEM'
TILE_URL = f'https://copernicus-dem-30m.s3.amazonaws.com/{TILE}/{TILE}.tif'


class AuxiliaryTests(unittest.TestCase):
    def record(self, name=EOF):
        parser = OrbitLinks('POEORB');parser.feed(f'<a href="{name}">orbit</a>')
        return list(parser.records.values())[0]

    def test_latest_orbit_must_cover_entire_acquisition_and_deduplicates(self):
        original = self.record()
        newer = {**original, 'created': '20240220T070659', 'filename': 'new.EOF', 'url': ORBIT_URL + 'new'}
        partial = {**newer, 'created': '20240221T070659', 'end': '20240130T121020'}
        wrong_sat = {**newer, 'platform': 'S1B', 'created': '20240222T070659'}
        with patch('auxiliary_api.orbit_catalog', return_value=[original, newer, partial, wrong_sat]) as catalog:
            result = match_orbits({'scenes': [SCENE, SCENE + '.zip'], 'strategy': 'auto'})
        self.assertEqual(result['matched'], 1);self.assertEqual(result['files'], 1)
        self.assertEqual(result['scenes'][0]['orbit'], newer)
        catalog.assert_called_once_with('POEORB')

    def test_auto_fallback_and_precise_only_missing_are_explicit(self):
        restored = {**self.record(), 'type': 'RESORB'}
        with patch('auxiliary_api.orbit_catalog', side_effect=[[], [restored]]):
            self.assertEqual(match_orbits({'scenes': [SCENE]})['matched'], 1)
        with patch('auxiliary_api.orbit_catalog', return_value=[]) as catalog:
            result = match_orbits({'scenes': [SCENE], 'strategy': 'precise'})
        self.assertEqual(result['missing'], 1);self.assertEqual(result['assets'], [])
        catalog.assert_called_once_with('POEORB')

    def test_names_and_index_links_never_accept_paths_or_remote_urls(self):
        for name in ('../' + SCENE, SCENE.replace('20240130', '20241330'), 'random.zip'):
            with self.assertRaises(AuxiliaryError):parse_scene(name)
        parser = OrbitLinks('POEORB')
        parser.feed(f'<a href="https://example.org/{EOF}">x</a><a href="../{EOF}">x</a>')
        self.assertFalse(parser.records)

    def test_safe_folder_is_not_walked_and_zip_safe_duplicates_collapse(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'tests') as folder:
            root = Path(folder);safe=root/(SCENE+'.SAFE');safe.mkdir()
            (safe/'manifest.safe').write_text('not read')
            (root/(SCENE+'.zip')).write_bytes(b'not opened')
            self.assertEqual(len(scan_scene_folder(root)['scenes']), 1)
            self.assertEqual(scan_scene_folder(safe)['scenes'][0]['id'], SCENE)

    def test_dem_coverage_uses_floor_for_west_south_and_keeps_missing_nodata(self):
        with patch('auxiliary_api.copdem_catalog', return_value={(-1, -1): TILE}):
            result = preview_dem({'bounds': [-.1,-.1,.1,.1], 'format': 'tiles'})
        self.assertEqual(result['available'],1);self.assertEqual(len(result['tiles']),4)
        self.assertEqual(result['height'],'egm2008');self.assertEqual(len(result['missing']),3)
        self.assertTrue(all('url' not in t for t in result['tiles'] if not t['available']))
        self.assertIn('NoData', result['warnings'][0])

    def test_dem_limits_and_untrusted_derived_fields(self):
        for payload in ({'bounds':[0,0,math.nan,1]}, {'bounds':[170,0,-170,1]},
                        {'bounds':[0,0,1,1], 'buffer':True}, {'bounds':[0,0,20,20]},
                        {'bounds':[0,0,10,10], 'format':'GTiff'}):
            with self.assertRaises(AuxiliaryError):dem_plan(payload)
        plan={'bounds':[86,43,86.01,43.01], 'height':'egm2008', 'format':'ISCE', 'target':'outside', 'paddedBounds':[0,0,90,80]}
        job = new_dem_job({'plan':plan, 'kind':'dem-process'})
        self.assertNotIn('target',job['plan']);self.assertNotIn('paddedBounds',job['plan'])
        self.assertEqual(job['id'],new_dem_job({'plan':{**plan,'target':'different'}})['id'])

    def test_public_assets_are_narrow_and_validate_without_credentials(self):
        for url,kind in ((ORBIT_URL,'orbit'),(TILE_URL,'dem'),(GEOID_URL,'geoid')):
            self.assertEqual(public_asset_kind(url),kind);self.assertEqual(validate_url(url,asset=True),url)
        for url in (ORBIT_URL.replace('https:', 'http:'), TILE_URL+'?token=x',
                    GEOID_URL.replace('cdn.proj.org','cdn.proj.org.evil.test'),
                    'https://copernicus-dem-30m.s3.amazonaws.com/secrets.txt',
                    'https://s1qc.asf.alaska.edu/aux_poeorb/../secret.EOF'):
            self.assertIsNone(public_asset_kind(url))
            with self.assertRaises(TransferError):validate_url(url,asset=True)


if __name__=='__main__':unittest.main()
