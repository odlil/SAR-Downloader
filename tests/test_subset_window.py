"""Window bounds against analytical affine geometry, including large interiors."""
from pathlib import Path
import sys
import gc
import io
import threading
import unittest
import numpy as np
import h5py
from shapely.geometry import Polygon
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from nisar_subset_engine import BASE, Paused, radar_window, remote_hdf5


class GridSource(dict):
    pass


class WindowTests(unittest.TestCase):
    def test_remote_hdf5_restores_gc_after_overlapping_files_and_error(self):
        memory=io.BytesIO()
        with h5py.File(memory,'w') as file:
            file['value']=[1,2,3]
        content=memory.getvalue()
        previous=gc.isenabled()
        try:
            for enabled in (True,False):
                gc.enable() if enabled else gc.disable()
                with self.assertRaisesRegex(RuntimeError,'test exit'):
                    with remote_hdf5(io.BytesIO(content)) as first:
                        self.assertFalse(gc.isenabled())
                        with remote_hdf5(io.BytesIO(content)) as second:
                            np.testing.assert_array_equal(first['value'][:],second['value'][:])
                        self.assertFalse(gc.isenabled())
                        raise RuntimeError('test exit')
                self.assertEqual(gc.isenabled(),enabled)
        finally:
            gc.enable() if previous else gc.disable()

    def setup_grid(self, levels=3):
        col,row=np.meshgrid(np.arange(45),np.arange(51))
        self.matrix=np.array([[.01,.002],[.003,.01]])
        self.offset=np.array([86.,43.])
        self.heights=np.arange(levels)*1000.
        self.arrays=(np.stack([86+.01*col+.002*row+h*.000001 for h in self.heights]),
                     np.stack([43+.003*col+.01*row for _ in self.heights]),self.heights,
                     np.arange(51)*2.,800000+np.arange(45)*50.)
        self.az=np.linspace(0,100,5001)
        source=GridSource({f'{BASE}/swaths/zeroDopplerTime':self.az})
        return source

    def polygon(self):
        native=np.array([[11.13,12.23],[31.31,14.37],[30.43,38.47],[9.59,35.61]])
        return Polygon(native@self.matrix.T+self.offset)

    def test_large_rotated_window_matches_analytic_bounds_at_all_heights(self):
        source=self.setup_grid()
        area=self.polygon()
        native=[]
        for height in self.heights:
            native.extend(np.linalg.solve(self.matrix,(np.asarray(area.exterior.coords)[:-1]-self.offset-[height*.000001,0]).T).T)
        native=np.asarray(native)
        result=radar_window(source,area,padding=0,arrays=self.arrays)
        self.assertEqual(result[:2],(np.searchsorted(self.az,2*native[:,1].min()),np.searchsorted(self.az,2*native[:,1].max(),side='right')))
        np.testing.assert_allclose(result[2:],[800000+50*native[:,0].min(),800000+50*native[:,0].max()],rtol=0,atol=1e-6)

    def test_both_polygon_windings_give_same_window(self):
        source=self.setup_grid(levels=1)
        area=self.polygon()
        a=radar_window(source,area,arrays=self.arrays)
        b=radar_window(source,Polygon(list(area.exterior.coords)[::-1]),arrays=self.arrays)
        np.testing.assert_allclose(a,b)

    def test_pause_interrupts_cpu_window_preparation(self):
        source=self.setup_grid()
        stop=threading.Event()
        def progress(**values):
            if values.get('prep_current') == 1:
                stop.set()
        def check():
            if stop.is_set():
                raise Paused()
        source._subset_progress=progress
        source._subset_check=check
        with self.assertRaises(Paused):
            radar_window(source,self.polygon(),arrays=self.arrays)


if __name__=='__main__':unittest.main(verbosity=2)
