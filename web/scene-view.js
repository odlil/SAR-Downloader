import { frameValue } from './core.js';

// Frame numbers repeat on different tracks; dates belong to the same coverage group.
export function sceneGroupKey(scene){
  if(scene.dataset==='SLC-BURST')return JSON.stringify([scene.dataset,scene.fullBurstID||scene.id]);
  const frame=frameValue(scene,scene.dataset==='NISAR'?'nisar':'asf');
  return JSON.stringify([scene.dataset,scene.path,Number.isFinite(frame)?frame:scene.id]);
}

export function groupScenes(scenes){
  const groups=new Map();
  for(const scene of scenes){
    const key=sceneGroupKey(scene);
    if(!groups.has(key)){
      const frame=frameValue(scene,scene.dataset==='NISAR'?'nisar':'asf');
      const label=scene.dataset==='SLC-BURST'?`Burst ${scene.fullBurstID||scene.id}`:`${scene.dataset==='NISAR'?'Track':'Path'} ${scene.path??'—'} · Frame ${Number.isFinite(frame)?frame:'—'}`;
      groups.set(key,{key,label,scenes:[]});
    }
    groups.get(key).scenes.push(scene);
  }
  return [...groups.values()];
}

export function nextGroupAtPoint(scenes,ids,activeKey,preferredID){
  const hitIDs=new Set(ids),hits=groupScenes(scenes.filter(scene=>hitIDs.has(scene.id)));
  if(!hits.length)return null;
  const current=hits.findIndex(group=>group.key===activeKey);
  if(current>=0&&hits.length>1)return hits[(current+1)%hits.length];
  return hits.find(group=>group.scenes.some(scene=>scene.id===preferredID))||hits[0];
}

function inRing(point,ring){
  const [x,y]=point;let inside=false;
  for(let i=0,j=ring.length-1;i<ring.length;j=i++){
    const [xi,yi]=ring[i],[xj,yj]=ring[j];
    const cross=(x-xi)*(yj-yi)-(y-yi)*(xj-xi);
    if(Math.abs(cross)<1e-10&&x>=Math.min(xi,xj)-1e-10&&x<=Math.max(xi,xj)+1e-10&&y>=Math.min(yi,yj)-1e-10&&y<=Math.max(yi,yj)+1e-10)return true;
    if((yi>y)!==(yj>y)&&x<(xj-xi)*(y-yi)/(yj-yi)+xi)inside=!inside;
  }
  return inside;
}

// Exact point hits allow switching footprints beneath other dates and frames.
export function containsPoint(geometry,point){
  if(!geometry)return false;
  if(geometry.type==='Feature')return containsPoint(geometry.geometry,point);
  const polygons=geometry.type==='Polygon'?[geometry.coordinates]:geometry.type==='MultiPolygon'?geometry.coordinates:[];
  return polygons.some(rings=>rings.length&&inRing(point,rings[0])&&!rings.slice(1).some(ring=>inRing(point,ring)));
}
