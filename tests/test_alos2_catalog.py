"""ALOS-2 CMR routing and metadata contracts, without external requests."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from alos2_catalog import cmr_query, normalize_granule, matches_filters
from asf_api import prepare_query, search_asf, normalize_scene, SearchError


def granule():
    return {'meta': {'provider-id': 'ASF'}, 'umm': {
        'CollectionReference': {'ShortName': 'ALOS2_L1_PSR2'}, 'Platforms': [{'ShortName': 'ALOS-2'}],
        'GranuleUR': 'ALOS2592361150-250512-WBDR1.1__A',
        'TemporalExtent': {'RangeDateTime': {'BeginningDateTime': '2025-05-12T10:12:01Z', 'EndingDateTime': '2025-05-12T10:12:53Z'}},
        'AdditionalAttributes': [{'Name': key, 'Values': values} for key, values in {
            'PATH_NUMBER': ['86'], 'FRAME_NUMBER': ['1150'], 'POLARIZATION': ['HH', 'HV'],
            'PROCESSING_LEVEL': ['1.1'], 'ASCENDING_DESCENDING': ['ASCENDING'], 'BEAM_MODE': ['WBD']}.items()],
        'SpatialExtent': {'HorizontalSpatialDomain': {'Geometry': {'GPolygons': [{'Boundary': {'Points': [
            {'Longitude': x, 'Latitude': y} for x, y in [[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]]
        ]}}]}}},
        'RelatedUrls': [{'Type': 'GET DATA', 'URL': 'https://cumulus.asf.earthdatacloud.nasa.gov/L1.1/ALOS2/scene.zip'},
                        {'Type': 'VIEW RELATED INFORMATION', 'URL': 'https://cumulus.asf.earthdatacloud.nasa.gov/s3credentials'}],
        'DataGranule': {'ArchiveAndDistributionInformation': [{'Name': 'scene.zip', 'SizeInBytes': 61239643845}]},
    }}


def payload(**changes):
    return {'dataset': 'ALOS-2', 'product': 'L1.1', 'frameScheme': 'asf', 'limit': 2,
            'aoi': {'type': 'Polygon', 'coordinates': [[[0, 0], [3, 0], [3, 3], [0, 3], [0, 0]]]}, **changes}


class ALOS2Tests(unittest.TestCase):
    def test_only_supported_products_and_path_scheme_are_accepted(self):
        for changes in ({'product': 'L2.2'}, {'path': '208'}, {'frameScheme': 'esa'}):
            with self.subTest(changes=changes), self.assertRaises(SearchError):
                prepare_query(payload(**changes))

    def test_explicit_collection_and_conservative_number_ranges(self):
        params, area, _ = prepare_query(payload(path='85,87-89', frame='1150', polarization='HH+HV', direction='ASCENDING'))
        query = cmr_query(params, area)
        self.assertIn(('short_name', 'ALOS2_L1_PSR2'), query)
        self.assertIn(('attribute[]', 'int,PATH_NUMBER,85,89'), query)
        self.assertIn(('attribute[]', 'int,FRAME_NUMBER,1150'), query)
        self.assertIn(('attribute[]', 'string,POLARIZATION,HH'), query)

    def test_realistic_large_zip_size_polarizations_and_frame(self):
        scene, _ = normalize_scene(normalize_granule(granule()), 'ALOS-2')
        self.assertEqual(scene['size'], 61239643845)
        self.assertEqual(scene['polarization'], 'HH+HV')
        self.assertEqual(scene['asfFrame'], 1150)
        self.assertEqual(scene['path'], 86)
        self.assertEqual(scene['product'], 'L1.1')
        self.assertTrue(matches_filters(scene, payload(path='86', polarization='HV+HH')))
        self.assertFalse(matches_filters(scene, payload(path='85,87-89')))

    def test_foreign_collection_platform_and_untrusted_downloads_are_rejected(self):
        for field in ('collection', 'platform', 'url'):
            item = granule()
            if field == 'collection': item['umm']['CollectionReference']['ShortName'] = 'NISAR_L1_RSLC_V1'
            elif field == 'platform': item['umm']['Platforms'][0]['ShortName'] = 'NISAR'
            else: item['umm']['RelatedUrls'][0]['URL'] = 'https://example.org/scene.zip'
            with self.subTest(field=field), self.assertRaises(ValueError):
                normalize_granule(item)

    def test_search_routes_to_cmr_and_keeps_exact_filters_and_aoi(self):
        with patch('asf_api._request', return_value=[granule()]) as request:
            result = search_asf(payload(path='86', frame='1150'))
        self.assertEqual(result['count'], 1)
        self.assertTrue(request.call_args.kwargs['items'])
        self.assertEqual(request.call_args.kwargs['endpoint'], 'https://cmr.earthdata.nasa.gov/search/granules.umm_json')
        with patch('asf_api._request', return_value=[granule()]):
            self.assertEqual(search_asf(payload(path='85,87-89'))['count'], 0)
        hole = deepcopy(payload()['aoi'])
        hole['coordinates'].append([[.5,.5],[.5,2.5],[2.5,2.5],[2.5,.5],[.5,.5]])
        self.assertEqual(prepare_query(payload(aoi=hole))[1].area, 5)

    def test_foreign_catalog_response_is_never_relabelled_as_alos2(self):
        item = granule(); item['umm']['CollectionReference']['ShortName'] = 'NISAR_L1_RSLC_V1'
        with patch('asf_api._request', return_value=[item]):
            result = search_asf(payload())
        self.assertEqual(result['count'], 0)
        self.assertTrue(result['warnings'])


if __name__ == '__main__': unittest.main()
