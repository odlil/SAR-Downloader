import test from 'node:test';
import assert from 'node:assert/strict';
import { groupScenes, sceneGroupKey, nextGroupAtPoint, containsPoint } from '../web/scene-view.js';

const scene=(id,path,asfFrame,extra={})=>({id,dataset:'SENTINEL-1',path,asfFrame,...extra});
const scenes=[scene('latest',114,136),scene('older',114,136),scene('adjacent',114,135),scene('other-track',143,136)];

test('Frame browsing groups dates together and separates the same frame on different paths',()=>{
  const groups=groupScenes(scenes);
  assert.equal(groups.length,3);
  assert.deepEqual(groups[0].scenes.map(s=>s.id),['latest','older']);
  assert.notEqual(sceneGroupKey(scenes[0]),sceneGroupKey(scenes[3]));
  assert.equal(groups[0].label,'Path 114 · Frame 136');
});

test('Overlapping footprints cycle coverage groups rather than individual dates',()=>{
  const ids=scenes.map(s=>s.id);
  assert.equal(nextGroupAtPoint(scenes,ids,'','older').key,sceneGroupKey(scenes[0]));
  assert.equal(nextGroupAtPoint(scenes,ids,sceneGroupKey(scenes[0]),'older').key,sceneGroupKey(scenes[2]));
  assert.equal(nextGroupAtPoint(scenes,ids,sceneGroupKey(scenes[3]),'other-track').key,sceneGroupKey(scenes[0]));
  assert.equal(nextGroupAtPoint(scenes,['latest','older'],sceneGroupKey(scenes[0]),'older').key,sceneGroupKey(scenes[0]));
  assert.equal(nextGroupAtPoint(scenes,[],null,null),null);
});

test('NISAR uses its own frame and Bursts use Full Burst ID independently of polarization',()=>{
  const nisar=[scene('n1',1,null,{dataset:'NISAR',frame:0}),scene('n2',1,null,{dataset:'NISAR',frame:1})];
  assert.equal(groupScenes(nisar)[0].label,'Track 1 · Frame 0');
  assert.equal(groupScenes(nisar).length,2);
  const bursts=['VV','VH'].map(polarization=>scene(polarization,114,null,{dataset:'SLC-BURST',fullBurstID:'114_242969_IW3',polarization}));
  assert.equal(groupScenes(bursts).length,1);
  assert.equal(groupScenes(bursts)[0].label,'Burst 114_242969_IW3');
  assert.equal(groupScenes([scene('unknown-a',114,null),scene('unknown-b',114,null)]).length,2);
});

test('Map hit testing excludes polygon holes and bounding-box-only hits',()=>{
  const geometry={type:'Polygon',coordinates:[[[0,0],[4,0],[4,4],[0,4],[0,0]],[[1,1],[3,1],[3,3],[1,3],[1,1]]]};
  assert.equal(containsPoint(geometry,[.5,2]),true);
  assert.equal(containsPoint(geometry,[2,2]),false);
  assert.equal(containsPoint(geometry,[5,2]),false);
  assert.equal(containsPoint(geometry,[0,2]),true);
  const triangle={type:'Polygon',coordinates:[[[0,0],[4,0],[0,4],[0,0]]]};
  assert.equal(containsPoint(triangle,[3,3]),false);
});

test('Map hit testing supports islands, altitude coordinates, and GeoJSON features',()=>{
  const geometry={type:'MultiPolygon',coordinates:[[[[0,0,100],[1,0,100],[1,1,100],[0,1,100],[0,0,100]]],[[[3,3],[4,3],[4,4],[3,4],[3,3]]]]};
  assert.equal(containsPoint({type:'Feature',geometry},[3.5,3.5]),true);
  assert.equal(containsPoint(geometry,[2,2]),false);
  assert.equal(containsPoint(geometry,[.5,.5]),true);
});
