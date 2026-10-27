"""Multi-source validation, credential boundaries and queue identity contracts."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from auxiliary_api import AuxiliaryError, dem_plan, preview_dem
from dem_jobs import new_dem_job
from dem_sources import protected_dem_url, srtm_tiles, needs_auth, nisar_vrt
from downloads import DownloadManager
from earthdata_auth import AuthError, EarthdataAuth, NoAuth, ScopedSession, TransferError, validate_url
from preflight import inspect, ensure_ready


class DemSourceTests(unittest.TestCase):
    def test_source_specific_heights_formats_and_buffered_srtm_coverage(self):
        for payload in ({'source':'unknown'}, {'source':[]}, {'source':'NISAR','height':'egm2008'},
                        {'source':'SRTM','height':'egm2008'}, {'source':'SRTM','format':'tiles'},
                        {'source':'NISAR','format':'tiles'}):
            with self.subTest(payload=payload), self.assertRaises(AuxiliaryError):
                dem_plan({'bounds':[86,43,86.01,43.01],**payload})
        with self.assertRaisesRegex(AuxiliaryError,'SRTM'):
            dem_plan({'bounds':[86,59.99,86.01,60],'source':'SRTM','buffer':5})
        self.assertEqual(dem_plan({'bounds':[86,43,86.01,43.01],'source':'SRTM','height':'egm96'})['height'],'egm96')

    def test_cop_identifiers_stay_compatible_and_sources_do_not_collide(self):
        plan={'bounds':[86,43,86.01,43.01],'buffer':0,'height':'ellipsoid','format':'GTiff'}
        original=hashlib.sha256(json.dumps(plan,sort_keys=True).encode()).hexdigest()[:24]
        jobs=[new_dem_job({'plan':{**plan,'source':source}}) for source in ('COP','NISAR','SRTM')]
        self.assertEqual(jobs[0]['id'],'dem:'+original)
        self.assertEqual(len({job['id'] for job in jobs}),3)
        self.assertFalse(needs_auth(jobs[0]));self.assertTrue(all(needs_auth(job) for job in jobs[1:]))
        for job in jobs:self.assertEqual(DownloadManager._new_job(job)['id'],job['id'])

    def test_preview_does_not_claim_verified_upstream_tiles_or_contact_copdem(self):
        with patch('auxiliary_api.copdem_catalog',side_effect=AssertionError('wrong catalog')):
            for source in ('SRTM','NISAR'):
                plan=preview_dem({'bounds':[-.1,-.1,.1,.1],'source':source})
                self.assertEqual(len(plan['tiles']),4)
                self.assertFalse(plan['coverageVerified'])
                self.assertIn('Earthdata',plan['warnings'][0])

    def test_new_srtm_url_is_canonical_and_narrow(self):
        tile=srtm_tiles([-.1,-.1,0,0])[0]
        self.assertEqual(tile['id'],'S01W001')
        self.assertTrue(protected_dem_url(tile['url']))
        self.assertEqual(validate_url(tile['url'],asset=True),tile['url'])
        for bad in (tile['url']+'?token=x',tile['url'].replace('/S01W001.SRTMGL1.hgt.zip','/N01E001.SRTMGL1.hgt.zip'),
                    tile['url'].replace('data.lpdaac.earthdatacloud.nasa.gov','evil.example'),
                    'https://data.lpdaac.earthdatacloud.nasa.gov/secrets.zip'):
            self.assertFalse(protected_dem_url(bad))
            with self.assertRaises(TransferError):validate_url(bad,asset=True)

    def test_lpdaac_signed_redirects_never_receive_bearer_or_basic_credentials(self):
        url=srtm_tiles([86,43,86.01,43.01])[0]['url']
        session=ScopedSession(token='fixture-token',basic=('fixture-user','fixture-password'))
        redirect=Mock(status_code=303,headers={'Location':'https://d123abc.cloudfront.net/tile.zip?Signature=fixture'})
        final=Mock(status_code=200)
        with patch.object(session.http,'request',side_effect=[redirect,final]) as request:
            self.assertIs(session.request('GET',url),final)
        first,second=request.call_args_list
        self.assertEqual(first.kwargs['headers']['Authorization'],'Bearer fixture-token')
        self.assertNotIn('Authorization',second.kwargs['headers'])
        self.assertIsInstance(second.kwargs['auth'],NoAuth)
        with self.assertRaises(TransferError):validate_url('https://d123abc.cloudfront.net/tile.zip')
        session.close()

    def test_worker_credentials_round_trip_without_netrc_or_persistence(self):
        auth=EarthdataAuth()
        with self.assertRaises(AuthError):auth.worker_state()
        auth.token='fixture-token'
        auth.cookies.set('asf-urs','fixture-cookie',domain='.asf.alaska.edu')
        state=json.loads(json.dumps(auth.worker_state()))
        child=EarthdataAuth();child._restore(state)
        self.assertEqual(child.token,auth.token)
        self.assertTrue(child.status()['burstReady'])
        self.assertNotIn('fixture-token',str(auth.status()))
        auth.close();child.close()

    def test_logged_out_mixed_dem_queue_only_runs_cop(self):
        root=Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory(dir=root) as folder:
            tasks=[new_dem_job({'plan':{'bounds':[86,43,86.01,43.01],'source':source}}) for source in ('COP','NISAR','SRTM')]
            with patch('preflight.dem_environment',return_value=True):
                report=inspect(tasks,{'output':str(Path(folder)/'output')},{'loggedIn':False})
            self.assertEqual(report['readyCount'],1)
            self.assertEqual(report['blockedCount'],2)
            with self.assertRaises(AuthError):
                with patch('preflight.dem_environment',return_value=True):
                    ensure_ready(inspect(tasks[1:],{'output':str(Path(folder)/'output')},{'loggedIn':False}))

    def test_nisar_index_prunes_unrelated_regions_and_rejects_remote_injection(self):
        xml=b'''<VRTDataset><GeoTransform>0,1,0,2,0,-1</GeoTransform><VRTRasterBand>
          <SimpleSource><SourceFilename>DEM.tif</SourceFilename><DstRect xOff="0" yOff="0" xSize="2" ySize="2"/></SimpleSource>
          <SimpleSource><SourceFilename>unused.tif</SourceFilename><DstRect xOff="100" yOff="100" xSize="1" ySize="1"/></SimpleSource>
        </VRTRasterBand></VRTDataset>'''
        index=Mock(status_code=200);index.iter_content.return_value=[xml]
        index.__enter__=Mock(return_value=index);index.__exit__=Mock(return_value=False)
        asset=Mock(status_code=200,url='https://d123abc.cloudfront.net/tile.tif?Signature=fixture')
        asset.__enter__=Mock(return_value=asset);asset.__exit__=Mock(return_value=False)
        session=Mock();session.request.side_effect=[index,asset]
        gdal=Mock()
        root,memory,assets=nisar_vrt(gdal,session,[.5,.5,1,1])
        self.assertTrue(root.startswith('/vsimem/'))
        self.assertEqual(len(memory),1);self.assertEqual(len(assets),1)
        saved=gdal.FileFromMemBuffer.call_args.args[1]
        self.assertNotIn(b'unused.tif',saved)
        self.assertIn(b'/vsicurl/https://d123abc.cloudfront.net/',saved)
        malicious=xml.replace(b'DEM.tif',b'https://evil.example/DEM.tif')
        index.iter_content.return_value=[malicious];session.request.side_effect=[index]
        with self.assertRaisesRegex(TransferError,'非官方'):nisar_vrt(gdal,session,[.5,.5,1,1])


if __name__=='__main__':unittest.main()
