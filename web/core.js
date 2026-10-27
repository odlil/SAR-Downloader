export const DATASETS = {
  'SENTINEL-1': { label: 'Sentinel-1', short: 'S1', pathLabel: 'Path · 相对轨道', columnPath: 'Path', products: ['SLC', 'GRD'], frameSchemes: [{ value: 'asf', label: 'ASF Frame' }, { value: 'esa', label: 'ESA Frame' }], hint: 'Sentinel-1 帧号可能随获取变化，必要时扩大到相邻帧。' },
  'SLC-BURST': { label: 'S1 Bursts', short: 'S1', pathLabel: 'Path · 相对轨道', columnPath: 'Path', products: ['BURST'], frameSchemes: [{ value: 'asf', label: 'Frame 不适用' }], hint: '单个 SLC Burst；可用 Full Burst ID 查找同一位置的时间序列。' },
  'NISAR': { label: 'NISAR', short: 'NI', pathLabel: 'Track · 相对轨道', columnPath: 'Track', products: ['RSLC', 'GSLC', 'GCOV', 'GUNW'], frameSchemes: [{ value: 'nisar', label: 'NISAR Frame' }], hint: 'NISAR 使用 Track / Frame；按主频段极化检索。' },
  'ALOS': { label: 'ALOS PALSAR', short: 'AL', pathLabel: 'Path · 轨道编号', columnPath: 'Path', products: ['L1.0', 'L1.1', 'L1.5', 'RTC_HI_RES', 'RTC_LOW_RES'], frameSchemes: [{ value: 'asf', label: 'JAXA / ASF Frame' }], hint: 'L1.1 为复数影像；RTC 为后向散射产品。' },
  'ALOS-2': { label: 'ALOS-2', short: 'AL2', pathLabel: 'Path · 轨道编号', columnPath: 'Path', products: ['L1.1'], frameSchemes: [{ value: 'asf', label: 'JAXA / ASF Frame' }], hint: '公开 ScanSAR L1.1；覆盖范围以 ASF 目录为准。' },
};

export function sensorForDataset(dataset) {
  return dataset === 'NISAR' ? 'nisar' : ['ALOS', 'ALOS-2'].includes(dataset) ? 'alos' : 'sentinel';
}

export function parseNumberFilter(value, { min = 0, max = 99999 } = {}) {
  const expression = String(value ?? '').trim().replaceAll('，', ',').replaceAll('–', '-').replaceAll('—', '-').replace(/\s+/g, '');
  if (!expression) return [];
  if (expression.length > 160 || !/^\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*$/.test(expression)) throw new Error('请输入单值、逗号列表或范围，例如 135、135,137 或 130-140。');
  return expression.split(',').map(token => {
    const [low, rawHigh] = token.split('-').map(Number);
    const high = rawHigh ?? low;
    if (!Number.isSafeInteger(low) || !Number.isSafeInteger(high) || low < min || high > max || low > high) throw new Error(`编号范围无效；应介于 ${min}–${max}，且起点不大于终点。`);
    return [low, high];
  });
}

export function matchesNumber(value, ranges) {
  return !ranges.length || (value !== null && value !== undefined && ranges.some(([low, high]) => Number(value) >= low && Number(value) <= high));
}

export function validateBounds(bounds) {
  if (!Array.isArray(bounds) || bounds.length !== 4 || !bounds.every(Number.isFinite)) throw new Error('请填写经度、纬度各自的起始值和结束值。');
  const [west, south, east, north] = bounds;
  if (west < -180 || east > 180 || south < -85 || north > 85 || west >= east || south >= north) throw new Error('请检查经纬度顺序。当前矩形支持经度 −180–180、纬度 −85–85；跨日期变更线范围请拆分。');
  return bounds;
}

export function frameValue(scene, scheme = 'asf') {
  return scheme === 'esa' ? scene.esaFrame : scheme === 'nisar' ? scene.frame : scene.asfFrame;
}

export function validateFilters(filters) {
  if (!DATASETS[filters.dataset]) throw new Error('不支持的数据集。');
  parseNumberFilter(filters.path, { min: 1 });
  parseNumberFilter(filters.frame);
  if (filters.dataset==='SLC-BURST' && filters.frame) throw new Error('S1 Bursts 使用 Full Burst ID 筛选。');
  if (filters.burstID && (filters.dataset!=='SLC-BURST' || !filters.burstID.toUpperCase().replaceAll('，',',').split(',').every(id=>/^\d{3}_\d{1,8}_(IW[1-3]|EW[1-5])$/.test(id.trim())))) throw new Error('Full Burst ID 格式例如 114_242969_IW3，多个编号用逗号分隔。');
  if (filters.start && !/^\d{4}-\d{2}-\d{2}$/.test(filters.start)) throw new Error('开始日期格式不正确。');
  if (filters.end && !/^\d{4}-\d{2}-\d{2}$/.test(filters.end)) throw new Error('结束日期格式不正确。');
  if (filters.start && filters.end && filters.start > filters.end) throw new Error('开始日期不能晚于结束日期。');
  if (filters.bounds) validateBounds(filters.bounds);
  return filters;
}

export function boundsIntersect(a, b) { return a[0] <= b[2] && a[2] >= b[0] && a[1] <= b[3] && a[3] >= b[1]; }

export function filterScenes(scenes, filters) {
  validateFilters(filters);
  const paths = parseNumberFilter(filters.path, { min: 1 });
  const frames = parseNumberFilter(filters.frame);
  return scenes.filter(scene => scene.dataset === filters.dataset
    && (!filters.product || scene.product === filters.product)
    && (!filters.start || scene.date >= filters.start)
    && (!filters.end || scene.date <= filters.end)
    && (!filters.direction || scene.direction === filters.direction)
    && (!filters.polarization || scene.polarization === filters.polarization)
    && matchesNumber(scene.path, paths)
    && matchesNumber(frameValue(scene, filters.frameScheme), frames)
    && (!filters.bounds || boundsIntersect(scene.bounds, filters.bounds)));
}

export function sceneGeometry(scene) {
  if (scene.geometry) return scene.geometry;
  const [w,s,e,n] = scene.bounds;
  return { type: 'Polygon', coordinates: [[[w,s],[e,s],[e,n],[w,n],[w,s]]] };
}

export function geometryBounds(geojson) {
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity, vertices = 0;
  const visitCoordinates = coords => {
    if (!Array.isArray(coords)) throw new Error('几何坐标格式无效。');
    if (typeof coords[0] === 'number') {
      const [x,y] = coords;
      if (!Number.isFinite(x) || !Number.isFinite(y) || x < -180 || x > 180 || y < -85 || y > 85) throw new Error('坐标不在支持的经纬度范围内，请确认使用 WGS84。');
      if (++vertices > 25000) throw new Error('范围顶点超过 25,000，请先简化矢量。');
      minX = Math.min(minX,x); minY = Math.min(minY,y); maxX = Math.max(maxX,x); maxY = Math.max(maxY,y);
    } else coords.forEach(visitCoordinates);
  };
  const visit = object => {
    if (!object || typeof object !== 'object') throw new Error('无效的 GeoJSON。');
    if (object.type === 'FeatureCollection') return object.features.forEach(visit);
    if (object.type === 'Feature') return visit(object.geometry);
    if (object.type !== 'Polygon' && object.type !== 'MultiPolygon') throw new Error('研究区仅支持 Polygon / MultiPolygon。');
    visitCoordinates(object.coordinates);
  };
  if (geojson.crs && !/4326|CRS84/i.test(JSON.stringify(geojson.crs))) throw new Error('GeoJSON 请先转换为 WGS84 / EPSG:4326。');
  visit(geojson);
  return validateBounds([minX,minY,maxX,maxY]);
}

export function unionBounds(bounds) {
  if (!bounds.length) throw new Error('请先选择影像或设置研究区。');
  return [Math.min(...bounds.map(b=>b[0])),Math.min(...bounds.map(b=>b[1])),Math.max(...bounds.map(b=>b[2])),Math.max(...bounds.map(b=>b[3]))];
}

export function demTiles(bounds, bufferKm = 0) {
  validateBounds(bounds);
  const [w,s,e,n] = bounds;
  const latPad = Number(bufferKm) / 111.32;
  const lonPad = latPad / Math.max(.1, Math.cos((s+n)*Math.PI/360));
  const padded = [Math.max(-180,w-lonPad),Math.max(-85,s-latPad),Math.min(180,e+lonPad),Math.min(85,n+latPad)];
  const firstX=Math.floor(padded[0]), lastX=Math.ceil(padded[2])-1, firstY=Math.floor(padded[1]), lastY=Math.ceil(padded[3])-1;
  if ((lastX-firstX+1)*(lastY-firstY+1)>500) throw new Error('预览范围超过 500 个瓦片，请缩小研究区。');
  const tiles=[];
  for(let y=firstY;y<=lastY;y++) for(let x=firstX;x<=lastX;x++) tiles.push({id:`${y<0?'S':'N'}${String(Math.abs(y)).padStart(2,'0')}_${x<0?'W':'E'}${String(Math.abs(x)).padStart(3,'0')}`, bounds:[x,y,x+1,y+1]});
  return {bounds:padded,tiles};
}

export function scanScenePaths(paths) {
  const found = new Map();
  for (const item of paths) {
    const segments = String(item).replaceAll('\\','/').split('/');
    for (const segment of segments) {
      const name=segment.replace(/\.(zip|SAFE)$/i,'');
      const match=/^(S1[A-D])_(IW|EW|SM|WV)_[A-Z0-9_]+_(\d{8}T\d{6})_(\d{8}T\d{6})_\d{6}_[A-Z0-9]+_[A-Z0-9]+$/i.exec(name);
      if (match) found.set(name,{id:name,platform:match[1],start:match[3],end:match[4],state:'unmatched'});
    }
  }
  return [...found.values()];
}

export function uniqueTasks(current, incoming) {
  const ids=new Set(current.map(t=>t.id));
  return [...current,...incoming.filter(t=>{if(ids.has(t.id))return false;ids.add(t.id);return true;})];
}

export function formatSize(bytes) {
  if (!Number.isFinite(bytes)) return '—';
  if(bytes>=1e9)return `${(bytes/1e9).toFixed(2)} GB`;
  if(bytes>=1e6)return `${(bytes/1e6).toFixed(1)} MB`;
  return `${Math.round(bytes/1e3)} KB`;
}

export function buildSearchRequest(filters, geometry) {
  validateFilters(filters);
  return { provider:'asf', dataset:filters.dataset, product:filters.product, start:filters.start||null, end:filters.end||null,
    path:filters.path||null, frame:filters.frame||null, frameScheme:filters.frameScheme,
    direction:filters.direction||null, polarization:filters.polarization||null, burstID:filters.burstID||null, limit:Number(filters.limit||200),
    aoi:geometry||{type:'Polygon',coordinates:[[[filters.bounds[0],filters.bounds[1]],[filters.bounds[2],filters.bounds[1]],[filters.bounds[2],filters.bounds[3]],[filters.bounds[0],filters.bounds[3]],[filters.bounds[0],filters.bounds[1]]]]} };
}

export function sceneDownloadUrls(scene, includeXML=false) {
  return [...new Set([scene.url,...(includeXML?(scene.xmlUrls||[]):[])].filter(value=>{try{return new URL(value).protocol==='https:';}catch{return false;}}))];
}

export function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[char]);
}
