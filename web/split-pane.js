const STORAGE_KEY='sar-light-workbench-map-ratio';
const DEFAULT_RATIO=.47;

// Keep the preferred ratio through window resizes without adding a UI dependency.
export function createMapSplit(stage,separator,{onResize=()=>{}}={}){
  let ratio=DEFAULT_RATIO,active=true,drag=null,frame=0;
  try{const value=Number(localStorage.getItem(STORAGE_KEY));if(value>0&&value<1)ratio=value;}catch{}
  const save=()=>{try{localStorage.setItem(STORAGE_KEY,String(ratio));}catch{}};
  function dimensions(){
    const available=Math.max(0,stage.clientHeight-separator.offsetHeight);
    const minMap=Math.min(200,available*.45),minResults=Math.min(220,available*.45);
    return {available,min:minMap,max:available-minResults};
  }
  function render(){
    frame=0;if(!active)return;
    const {available,min,max}=dimensions();if(!available)return;
    const height=Math.max(min,Math.min(max,ratio*available));
    stage.style.setProperty('--map-pane-size',`${height}px`);
    const percent=Math.round(height/available*100);
    separator.setAttribute('aria-valuemin',String(Math.round(min/available*100)));
    separator.setAttribute('aria-valuemax',String(Math.round(max/available*100)));
    separator.setAttribute('aria-valuenow',String(percent));
    separator.setAttribute('aria-valuetext',`地图 ${percent}%，下方列表 ${100-percent}%`);
    onResize();
  }
  function resize(){if(!frame)frame=requestAnimationFrame(render);}
  function setHeight(height){
    const {available,min,max}=dimensions();if(!available)return;
    ratio=Math.max(min,Math.min(max,height))/available;resize();
  }
  function finish(){
    if(!drag)return;
    const {pointerId}=drag;drag=null;
    document.body.classList.remove('is-resizing-panes');
    if(separator.hasPointerCapture(pointerId))separator.releasePointerCapture(pointerId);
    save();resize();
  }
  separator.addEventListener('pointerdown',event=>{
    if(!active||event.button!==0||!event.isPrimary)return;
    event.preventDefault();separator.focus({preventScroll:true});
    drag={pointerId:event.pointerId,y:event.clientY,height:stage.firstElementChild.getBoundingClientRect().height};
    separator.setPointerCapture(event.pointerId);
    document.body.classList.add('is-resizing-panes');
  });
  separator.addEventListener('pointermove',event=>{
    if(drag?.pointerId===event.pointerId)setHeight(drag.height+event.clientY-drag.y);
  });
  separator.addEventListener('pointerup',event=>{
    if(drag?.pointerId!==event.pointerId)return;
    setHeight(drag.height+event.clientY-drag.y);finish();
  });
  separator.addEventListener('pointercancel',finish);
  separator.addEventListener('lostpointercapture',finish);
  window.addEventListener('blur',finish);
  separator.addEventListener('dblclick',()=>{ratio=DEFAULT_RATIO;save();resize();});
  separator.addEventListener('keydown',event=>{
    if(!active)return;
    const height=stage.firstElementChild.getBoundingClientRect().height;
    const {min,max}=dimensions(),step=event.shiftKey?48:16;
    const next={ArrowUp:height-step,ArrowDown:height+step,Home:min,End:max}[event.key];
    if(next===undefined)return;
    event.preventDefault();setHeight(next);save();
  });
  new ResizeObserver(resize).observe(stage);
  render();
  return {setVisible(visible){finish();active=visible;separator.hidden=!visible;if(visible)resize();}};
}
