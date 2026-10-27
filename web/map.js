import { createBasemap } from "./basemap.js";
import { sceneGeometry, frameValue } from './core.js';
import { containsPoint } from './scene-view.js';

export function createWorkbenchMap({ onBounds, onScene, onDrawEnd, onStatus, onSatelliteRequest }) {
  if (!window.L) throw new Error('地图组件未加载，请通过启动器打开。');
  const L=window.L;
  const map=L.map('map',{zoomControl:false,preferCanvas:true,minZoom:2,maxZoom:19,zoomSnap:.1,zoomDelta:.5,worldCopyJump:false}).setView([25,65],3);
  L.control.zoom({position:'bottomright'}).addTo(map);
  L.control.scale({position:'bottomleft',imperial:false,maxWidth:85}).addTo(map);
  map.createPane('reference'); map.getPane('reference').style.zIndex=190;
  map.createPane('aoi'); map.getPane('aoi').style.zIndex=450;
  map.createPane('tiles'); map.getPane('tiles').style.zIndex=420;
  // Decorative canvases must not intercept clicks intended for the SAR footprints.
  map.getPane('aoi').style.pointerEvents='none';
  map.getPane('tiles').style.pointerEvents='none';
  let reference,aoiLayer,draft, drawing=false, dragStart=null;
  const footprints=L.featureGroup().addTo(map), tileOverlays=L.featureGroup().addTo(map);
  const sceneLayers=new Map();let latestScenes=[],latestSelection=new Set(),activeScenes=new Set(),activeScene=null,lastHighlight=null,pendingFit=null,autoFitTarget=null,fitting=false;
  map.on('dragstart',()=>{autoFitTarget=null;pendingFit=null;});
  map.on('zoomstart',()=>{if(!fitting){autoFitTarget=null;pendingFit=null;}});
  fetch('./assets/world.geojson').then(r=>{if(!r.ok)throw new Error('离线底图不可用');return r.json();}).then(data=>{
    reference=L.geoJSON(data,{pane:'reference',interactive:false,style:{color:'#c8d3e0',weight:.8,fillColor:'#f4f7fb',fillOpacity:1}}).addTo(map);
    map.attributionControl.addAttribution('轮廓 © Natural Earth');
  }).catch(()=>onStatus('离线轮廓加载失败；在线底图仍可使用。'));

  const { setBasemap } = createBasemap(map, {
    onStatus: (message) => { document.getElementById('map-connection').textContent = message; },
    onSatelliteRequest,
  });
  function setBounds(bounds,geometry,fit=false){
    if(aoiLayer)map.removeLayer(aoiLayer);
    aoiLayer=null;
    if(!bounds){
      if(fit){autoFitTarget=null;pendingFit=null;map.setView([25,65],3,{animate:false});}
      return;
    }
    const options={pane:'aoi',interactive:false,color:'#b68e43',weight:4,fillColor:'#d4bd7c',fillOpacity:.045};
    aoiLayer=geometry?L.geoJSON(geometry,{style:options,...options}).addTo(map):L.rectangle([[bounds[1],bounds[0]],[bounds[3],bounds[2]]],options).addTo(map);
    if(fit)fitBounds();
  }
  function fit(target){
    pendingFit=target;autoFitTarget=target;
    if(!map.getContainer().clientWidth||!map.getContainer().clientHeight)return;
    const bounds=Array.isArray(target)?L.latLngBounds([[target[1],target[0]],[target[3],target[2]]]):target==='scenes'?footprints.getBounds():aoiLayer?.getBounds();
    if(!bounds?.isValid()){pendingFit=null;return;}
    map.invalidateSize({animate:false});
    fitting=true;
    try{map.fitBounds(bounds,{paddingTopLeft:target==='aoi'?[18,18]:[14,12],paddingBottomRight:target==='aoi'?[38,40]:[42,46],maxZoom:target==='aoi'?18:17,animate:false});}
    finally{fitting=false;}
    pendingFit=null;
  }
  function fitBounds(){fit('aoi');}
  function fitScenes(){if(sceneLayers.size)fit('scenes');}
  function fitExtent(bounds){fit(bounds);}
  const style=(id,hover=false)=>{
    const current=activeScene===id,focused=activeScenes.has(id),selected=latestSelection.has(id);
    return {color:focused?'#c46b2a':selected?'#2563a6':'#5e89ba',weight:current?3:focused||selected||hover?2:1,fillColor:focused?'#e7a15e':selected?'#3b82d0':'#7ea6d4',fillOpacity:current?.18:focused?.09:selected?.2:hover?.12:.045,opacity:focused||selected?1:.8};
  };
  function paint(){
    for(const [id,layer] of sceneLayers)layer.setStyle(style(id));
    for(const id of activeScenes)sceneLayers.get(id)?.bringToFront();
    sceneLayers.get(activeScene)?.bringToFront();
  }
  function setScenes(scenes,selected){
    latestSelection=new Set(selected);
    if(latestScenes===scenes){paint();return;}
    latestScenes=scenes;footprints.clearLayers();sceneLayers.clear();lastHighlight=null;
    activeScenes=new Set();activeScene=null;
    for(const scene of scenes){
      const layer=L.geoJSON(sceneGeometry(scene),{style:style(scene.id)});
      const label=document.createElement('div');label.className='scene-tooltip';
      const strong=document.createElement('strong');strong.textContent=`${scene.platform} · ${scene.date}`;
      label.append(strong,document.createTextNode(scene.dataset==='SLC-BURST'?`Burst ${scene.fullBurstID||'—'}`:`Path / Track ${scene.path??'—'} · Frame ${frameValue(scene,scene.dataset==='NISAR'?'nisar':'asf')??'—'}`));
      const help=document.createElement('div');help.textContent='点击查看此覆盖组；重叠处再次点击切换';label.append(help);
      layer.bindTooltip(label,{sticky:true,direction:'top',opacity:.98});
      layer.on('mouseover',()=>layer.setStyle(style(scene.id,true)));
      layer.on('mouseout',()=>layer.setStyle(style(scene.id)));
      layer.addTo(footprints);sceneLayers.set(scene.id,layer);
    }
  }
  function setActiveScenes(ids,id){if(activeScene!==id)for(const layer of sceneLayers.values())layer.closeTooltip();activeScenes=new Set(ids);activeScene=id;lastHighlight=null;paint();}
  function highlightScene(id){if(lastHighlight&&lastHighlight!==id)sceneLayers.get(lastHighlight)?.setStyle(style(lastHighlight));const layer=sceneLayers.get(id);if(layer){layer.bringToFront();layer.setStyle({...style(id,true),weight:3});lastHighlight=id;}}
  function clearHighlight(){if(lastHighlight)sceneLayers.get(lastHighlight)?.setStyle(style(lastHighlight));lastHighlight=null;paint();}
  function showScenes(show){if(show)footprints.addTo(map);else map.removeLayer(footprints);}
  function setTiles(tiles){tileOverlays.clearLayers();for(const tile of tiles)L.rectangle([[tile.bounds[1],tile.bounds[0]],[tile.bounds[3],tile.bounds[2]]],{pane:'tiles',color:tile.available===false?'#b08442':'#2563a6',weight:1,dashArray:tile.available===false?'3 5':'4 3',fillOpacity:tile.available===false?.02:.07,interactive:false}).addTo(tileOverlays);}
  function cancelDraw(){drawing=false;dragStart=null;map.dragging.enable();map.doubleClickZoom.enable();map.getContainer().classList.remove('map-drawing');document.getElementById('draw-instruction').hidden=true;document.getElementById('draw-aoi').classList.remove('active');if(draft){map.removeLayer(draft);draft=null;}onDrawEnd();}
  function startDraw(){drawing=true;map.dragging.disable();map.doubleClickZoom.disable();map.getContainer().classList.add('map-drawing');document.getElementById('draw-instruction').hidden=false;document.getElementById('draw-aoi').classList.add('active');}
  map.on('click',event=>{
    if(drawing||!map.hasLayer(footprints))return;
    const point=[event.latlng.lng,event.latlng.lat];
    const hits=latestScenes.filter(scene=>containsPoint(sceneGeometry(scene),point)).map(scene=>scene.id);
    if(hits.length)onScene(hits[0],hits);
  });
  map.on('mousedown',e=>{if(drawing&&e.originalEvent.button===0){dragStart=e.latlng;draft=L.rectangle(L.latLngBounds(dragStart,dragStart),{pane:'aoi',color:'#b68e43',weight:4,fillOpacity:.08,interactive:false}).addTo(map);}});
  map.on('mousemove',e=>{if(drawing&&dragStart&&draft)draft.setBounds(L.latLngBounds(dragStart,e.latlng));});
  window.addEventListener('mouseup',e=>{
    if(!drawing||!dragStart||!draft)return;
    const point=map.mouseEventToLatLng(e), bounds=L.latLngBounds(dragStart,point);
    const output=[bounds.getWest(),bounds.getSouth(),bounds.getEast(),bounds.getNorth()];
    cancelDraw();if(output[2]-output[0]>.001&&output[3]-output[1]>.001)onBounds(output);else onStatus('矩形太小，请重新拖动绘制。');
  });
  window.addEventListener('keydown',e=>{if(e.key==='Escape'&&drawing)cancelDraw();});
  function restoreView(view){
    if(!Array.isArray(view?.center)||view.center.length!==2||!view.center.every(Number.isFinite)||!Number.isFinite(view.zoom))return;
    autoFitTarget=null;pendingFit=null;
    map.setView([Math.max(-85,Math.min(85,view.center[0])),view.center[1]],Math.max(2,Math.min(19,view.zoom)),{animate:false});
  }
  return {setBounds,fitBounds,fitScenes,fitExtent,setScenes,setActiveScenes,showScenes,highlightScene,clearHighlight,setTiles,startDraw,cancelDraw,setBasemap,restoreView,resize:()=>{map.invalidateSize({animate:false,debounceMoveend:true});if(pendingFit||autoFitTarget)fit(pendingFit||autoFitTarget);},map};
}
