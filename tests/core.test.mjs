import test from 'node:test';
import assert from 'node:assert/strict';
import { parseNumberFilter,matchesNumber,filterScenes,geometryBounds,validateBounds,demTiles,scanScenePaths,uniqueTasks,buildSearchRequest,sceneDownloadUrls,escapeHtml,DATASETS } from '../web/core.js';
import { sensorForDataset } from '../web/core.js';

test('ALOS and ALOS-2 use a separate module with supported products and a JAXA frame scheme', () => {
  assert.equal(sensorForDataset('ALOS'), 'alos');
  assert.equal(sensorForDataset('ALOS-2'), 'alos');
  assert.equal(sensorForDataset('NISAR'), 'nisar');
  assert.equal(sensorForDataset('SLC-BURST'), 'sentinel');
  assert.deepEqual(DATASETS['ALOS-2'].products, ['L1.1']);
  assert.ok(DATASETS.ALOS.products.includes('RTC_HI_RES'));
  assert.deepEqual(DATASETS['ALOS-2'].frameSchemes.map(item => item.value), ['asf']);
});

test('Path / Frame supports single values, lists, ranges, Chinese comma, and zero',()=>{
  const ranges=parseNumberFilter('0， 135, 137–139');
  assert.deepEqual(ranges,[[0,0],[135,135],[137,139]]);
  assert.equal(matchesNumber(0,ranges),true);
  assert.equal(matchesNumber(138,ranges),true);
  assert.equal(matchesNumber(136,ranges),false);
  assert.equal(matchesNumber(null,ranges),false);
  for(const input of ['140-130','1,,2','abc','1-2-3','-1','1,'])assert.throws(()=>parseNumberFilter(input));
  assert.throws(()=>parseNumberFilter('0',{min:1}));
});

const base={dataset:'SENTINEL-1',product:'SLC',date:'2026-09-24',path:135,asfFrame:486,esaFrame:3120,polarization:'VV+VH',direction:'ASCENDING',bounds:[102,29,105,33]};
const filters={dataset:'SENTINEL-1',path:'135-137',frame:'486',frameScheme:'asf',product:'SLC',start:'2026-07-01',end:'2026-09-30',bounds:[102.5,29,106,32.5]};
test('Path, Frame, date, product and AOI combine as AND filters',()=>{
  const scenes=[{...base,id:'yes'},{...base,id:'wrong-path',path:62},{...base,id:'wrong-frame',asfFrame:487},{...base,id:'wrong-date',date:'2025-09-01'},{...base,id:'wrong-region',bounds:[120,20,121,21]},{...base,id:'wrong-product',product:'GRD'}];
  assert.deepEqual(filterScenes(scenes,filters).map(s=>s.id),['yes']);
});

test('ASF Frame and ESA Frame are separate numbering systems',()=>{
  assert.equal(filterScenes([base],{...filters,frame:'3120',frameScheme:'esa'}).length,1);
  assert.equal(filterScenes([base],{...filters,frame:'3120',frameScheme:'asf'}).length,0);
  assert.equal(filterScenes([{...base,asfFrame:0}],{...filters,frame:'0'}).length,1);
});

test('NISAR uses Track and its own Frame value',()=>{
  const scene={...base,dataset:'NISAR',product:'RSLC',path:165,frame:100};
  const query={...filters,dataset:'NISAR',product:'RSLC',path:'165',frame:'100',frameScheme:'nisar'};
  assert.equal(filterScenes([scene],query).length,1);
  assert.equal(buildSearchRequest(query).frameScheme,'nisar');
});

test('Bounds and imported geometries reject inverted or projected coordinates',()=>{
  assert.throws(()=>validateBounds([106,29,102,33]));
  assert.throws(()=>validateBounds([102,29,106,NaN]));
  assert.throws(()=>geometryBounds({type:'LineString',coordinates:[[102,29],[103,30]]}));
  assert.throws(()=>geometryBounds({type:'Polygon',coordinates:[[[400000,3500000],[400001,3500000],[400001,3500001],[400000,3500000]]]}));
  const shape={type:'FeatureCollection',features:[{type:'Feature',properties:{},geometry:{type:'Polygon',coordinates:[[[102,29],[106,29],[106,33],[102,33],[102,29]]]}}]};
  assert.deepEqual(geometryBounds(shape),[102,29,106,33]);
});

test('DEM tile planning respects exact boundaries and negative coordinates',()=>{
  assert.deepEqual(demTiles([10,20,11,21],0).tiles.map(t=>t.id),['N20_E010']);
  assert.deepEqual(demTiles([-2,-3,-1,-2],0).tiles.map(t=>t.id),['S03_W002']);
  assert.throws(()=>demTiles([-170,-60,170,60],0),/500/);
});

test('ZIP and SAFE duplicates resolve to one Sentinel scene without reading imagery',()=>{
  const name='S1A_IW_SLC__1SDV_20260924T110236_20260924T110303_066123_081234_ABCD';
  const scenes=scanScenePaths([`raw/${name}.zip`,`raw/${name}.SAFE/manifest.safe`,`raw/${name}.SAFE/measurement/a.tiff`,'raw/other.zip']);
  assert.equal(scenes.length,1);
  assert.equal(scenes[0].platform,'S1A');
  assert.equal(scenes[0].start,'20260924T110236');
  assert.equal(scenes[0].end,'20260924T110303');
});

test('Queue identity prevents duplicates within one batch and existing tasks',()=>{
  assert.deepEqual(uniqueTasks([{id:'a'}],[{id:'a'},{id:'b'},{id:'b'}]),[{id:'a'},{id:'b'}]);
});

test('Search contract preserves numeric filters and the imported polygon',()=>{
  const geometry={type:'Polygon',coordinates:[[[102,29],[106,29],[104,33],[102,29]]]};
  const request=buildSearchRequest({...filters,path:'135,137-139',frame:'485-488'},geometry);
  assert.equal(request.path,'135,137-139');assert.equal(request.frame,'485-488');assert.deepEqual(request.aoi,geometry);
  assert.throws(()=>filterScenes([],{...filters,start:'2026-10-01'}),/开始日期/);
});

test('Burst requests preserve ID, individual polarization and result limit',()=>{
  assert.ok(DATASETS['SLC-BURST']);
  const request=buildSearchRequest({...filters,dataset:'SLC-BURST',product:'BURST',frame:'',burstID:'114_242969_IW3',polarization:'VV',limit:500});
  assert.equal(request.burstID,'114_242969_IW3');assert.equal(request.limit,500);assert.equal(request.polarization,'VV');
  assert.throws(()=>buildSearchRequest({...filters,dataset:'SLC-BURST',product:'BURST'}),/Full Burst ID/);
});

test('Download link copying deduplicates optional Burst XML and rejects unsafe protocols',()=>{
  const scene={url:'https://example.org/1.tiff',xmlUrls:['https://example.org/1.xml','https://example.org/1.xml','javascript:alert(1)']};
  assert.deepEqual(sceneDownloadUrls(scene),['https://example.org/1.tiff']);
  assert.deepEqual(sceneDownloadUrls(scene,true),['https://example.org/1.tiff','https://example.org/1.xml']);
});

test('HTML escaping preserves ordinary values and protects text and attributes', () => {
  assert.equal(escapeHtml(null), '');
  assert.equal(escapeHtml(0), '0');
  assert.equal(escapeHtml('Sentinel-1 中文'), 'Sentinel-1 中文');
  assert.equal(escapeHtml(`<img title="a&b" data-x='c'>`), '&lt;img title=&quot;a&amp;b&quot; data-x=&#39;c&#39;&gt;');
});
