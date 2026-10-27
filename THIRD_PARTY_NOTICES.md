# 第三方组件说明

SAR Downloader 的发行包包含以下组件。各组件的许可独立适用；Windows 发行包的 `licenses/` 保留完整许可文件与版本信息。

| 组件 | 用途 / 许可 |
| --- | --- |
| CPython | Python 运行时，PSF License |
| Tcl/Tk | 文件和目录选择器，Tcl/Tk 许可 |
| requests、charset-normalizer、idna、urllib3 | HTTPS 与下载；Apache-2.0 / MIT / BSD 等，见对应许可文件 |
| certifi | CA 证书，MPL-2.0 |
| pyshp | SHP 读取，MIT |
| pyproj / PROJ | 坐标转换及坐标数据库，MIT 及所附数据许可 |
| shapely / GEOS | 几何计算，BSD / LGPL-2.1 等，见所附许可 |
| NumPy | 数组与 RSLC 裁剪，BSD 及所附第三方许可 |
| h5py / HDF5 | RSLC 原生 HDF5 数据块读取与输出，BSD-3-Clause / HDF5 许可，详见发行包附带许可 |
| sardem 0.14 | CopDEM 拼接、裁剪及高程处理，MIT |
| GDAL / PROJ 及其原生依赖 | 独立 DEM 引擎；许可和实际版本见 `dem-engine/licenses/engine-packages.json` 及各包完整文本 |
| cryptography / cffi / pycparser | Linux 本地设置加密，Apache-2.0 / BSD / MIT，完整许可见发行包 licenses |
| PyInstaller bootloader | 打包启动器，GPL 加 bootloader 分发例外；不要求使用者安装 PyInstaller |
| Leaflet 1.9.4 | 地图，BSD-2-Clause，许可见 `web/vendor/LEAFLET-LICENSE` |
| PDF.js 5.6.205 | 离线说明书阅读，Apache-2.0，许可见 `web/vendor/pdfjs/LICENSE.txt` |
| Natural Earth / world-atlas | 离线世界轮廓，Natural Earth 公共领域数据；world-atlas ISC |

地图界面保留 OpenStreetMap / Natural Earth 等数据署名。在线底图和 ASF / NASA 数据下载仍适用各服务的数据、账号与使用条款；本仓库不附带 SAR 影像或平台账号。

Copernicus DEM GLO-30 使用公开 AWS 镜像，原始高程为 EGM2008，属于 DSM；数据使用和署名参见 https://copernicus-dem-30m.s3.amazonaws.com/readme.html 。PROJ 官方 `us_nga_egm08_25.tif` 网格按需下载，参见 https://cdn.proj.org/ 。这些地理数据不包含在发布 ZIP 中。源码配置脚本按需下载官方 micromamba（BSD-3-Clause），不将引导程序随 ZIP 分发。

NISAR DEM v1.2 使用 NASA / ASF 官方数据源，是 JPL 为 NISAR 准备的 Copernicus DEM 衍生数据，已转换为 WGS84 椭球高；数据说明与引用见 <https://www.earthdata.nasa.gov/data/catalog/asf-nisar-dem-1>，并保留原 Copernicus 数据署名。SRTMGL1 V003 使用 LP DAAC Earthdata Cloud，数据引用为 <https://doi.org/10.5067/MEASURES/SRTM/SRTMGL1.003>；原始 EGM96 高程的转换网格 `us_nga_egm96_15.tif` 来自 PROJ 官方 CDN。上述 DEM 数据与 EGM96 网格不包含在发行包中，使用时按需获取。

源码中的额外许可文本位于 `packaging/licenses/`：Tcl 8.6.15 取自 Tcl 官方仓库的 `core-8-6-15/license.terms`；world-atlas ISC 许可取自官方 `topojson/world-atlas` 仓库的 `LICENSE`。Leaflet 许可在 `web/vendor/LEAFLET-LICENSE`。这些文本也随 Windows 包一起分发。

Linux 本地加密使用 cryptography 的 Fernet 接口：<https://cryptography.io/en/latest/fernet/>。本地密钥由操作系统文件权限限制，仅当前用户可读写。
