export function demSettings(source) {
  if (source === "NISAR") return {
    source, heights: [["ellipsoid", "WGS84 椭球高（原始基准）"]], tiles: false,
    note: "NISAR DEM v1.2 已为 WGS84 椭球高，按范围读取约 30 m 高程；需要登录 Earthdata。",
  };
  if (source === "SRTM") return {
    source, heights: [["ellipsoid", "WGS84 椭球高"], ["egm96", "保留 EGM96 高程"]], tiles: false,
    note: "SRTMGL1 V003 约 30 m，覆盖南纬 56°至北纬 60°陆地；需要登录 Earthdata，转换椭球高时首次下载 EGM96 网格。",
  };
  return {
    source: "COP", heights: [["ellipsoid", "WGS84 椭球高"], ["egm2008", "保留 EGM2008 高程"]], tiles: true,
    note: "GLO-30 为 EGM2008 DSM，无需登录；转换椭球高时，首次需下载高程转换网格。",
  };
}
