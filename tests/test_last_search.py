import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from last_search import read_location,remember_location


class LastSearchTests(unittest.TestCase):
    def test_last_success_survives_restart_without_storing_results_or_credentials(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'tests') as folder:
            payload={'aoi':{'type':'Polygon','coordinates':[[[86,43],[87,43],[87,44],[86,44],[86,43]]]},'password':'fixture-secret'}
            result={'scenes':[{'bounds':[85,42,88,45],'url':'https://example.org/private'}]}
            self.assertTrue(remember_location(folder,payload,result,100))
            record=read_location(Path(folder))
            self.assertEqual(record,{'bounds':[85,42,88,45],'aoiBounds':[86,43,87,44],'updatedAt':100})
            text=(Path(folder)/'last-search.json').read_text()
            self.assertNotIn('fixture-secret',text);self.assertNotIn('private',text)
            self.assertFalse(remember_location(folder,payload,{},200))
            self.assertEqual(read_location(folder),record)
            self.assertFalse(remember_location(folder,payload,{'scenes':[]},99))
            self.assertEqual(read_location(folder),record)

    def test_empty_result_remembers_query_area_and_corrupt_files_fall_back(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'tests') as folder:
            payload={'aoi':{'type':'Polygon','coordinates':[[[1,2],[3,2],[3,4],[1,4],[1,2]]]}}
            self.assertTrue(remember_location(folder,payload,{'scenes':[]},1))
            self.assertEqual(read_location(folder)['bounds'],[1,2,3,4])
            file=Path(folder)/'last-search.json'
            for value in ('broken','x'*5000,json.dumps({'bounds':[1,2,0,4]})):
                file.write_text(value);self.assertIsNone(read_location(folder))


if __name__=='__main__':unittest.main()
