# SAR Downloader

本地 SAR 数据下载工具，版本 1.0。由 `sar-workbench` 的检索、整景、轨道和 DEM 能力，与 `nisar-rslc-downloader` 的 RSLC 分块裁剪引擎整合而成。原来两个项目保持独立；本项目无需引用它们的运行目录。

普通用户请从本仓库的 **Releases** 下载免安装包；GitHub 的 **Code → Download ZIP** 是开发源码，需要自行配置 Python。快速操作见 [快速开始](../QUICKSTART.md)，本次替换构建说明见 [发布说明](../RELEASE_NOTES.md)。

| 下载文件 | 适用对象 |
| --- | --- |
| `SAR-v1.0-windows-x64.zip` | Windows x64 用户，解压后运行 `SAR.exe` |
| `SAR-v1.0-linux-x86_64.tar.gz` | Linux x86_64、glibc 2.35+ 用户，解压后运行 `./SAR` |
| `SAR-v1.0-source.zip` | 开发者；包括源码、测试和构建脚本 |
| `SHA256SUMS.txt` / `RELEASE_MANIFEST.json` | 文件完整性及源码与程序包对应记录 |

当前未提供 macOS、ARM64 或 Alpine/musl 免安装包。免安装版不需要 Python、Conda 或 GDAL；在线影像需使用自己的 Earthdata 账号。

## 检查更新

点击窗口右下角“检查更新”，可查看 GitHub 最新正式版、更新说明，并直接下载当前平台的新版免安装包或源码包。程序打开时只读取本地设置，点击检查后才连接 GitHub；不需要 GitHub 账号，也不使用 Earthdata 凭据。下载由浏览器处理，保存位置和进度遵循浏览器设置。

更新窗口中的“更新来源”可填写发布者提供的公开仓库主页地址或 `用户名/仓库名`，检查时保存在当前用户目录。仓库尚未创建或未发布正式 Release 时会明确提示。下载完成后先暂停任务、等待停止并退出旧程序，再完整解压新版启动；账号、队列和设置沿用独立的用户数据目录。源码版用户也可选择“下载源码”。

发布者在打包前填写根目录 `update_config.json` 的 `repository` 字段，用户收到的程序即默认检查该仓库。后续发布须同时更新 `app_runtime.py` 的 `VERSION`、创建对应数字版本 tag（如 `v1.1.0`），并上传命名匹配的程序包；仅替换同一个版本的附件不会触发“发现新版”。详见 [发布与更新配置](../RELEASING.md)。

## 启动

提供 Windows x64 ZIP、Linux x86_64 tar.gz 免安装包及源码包，版本均为 1.0。

Windows 免安装版：完整解压 `SAR-v1.0-windows-x64.zip`，进入 `SAR` 文件夹，双击 **`SAR.exe`**。无需安装 Python、Conda 或 GDAL，也无需运行首次配置。请保留同目录的 `_internal` 和 `dem-engine` 文件夹。影像检索、底图和在线下载需要联网；Earthdata 账号仍需自行登录。

Linux 免安装版（x86_64，glibc 2.35+，如 Ubuntu 22.04 / 24.04 或 Debian 12 桌面版）：

```sh
tar -xzf SAR-v1.0-linux-x86_64.tar.gz
cd SAR
./SAR
```

也可运行同目录的 `启动SAR Downloader.sh`。无需安装 Python、Conda、GDAL；保留 `_internal` 和 `dem-engine`。本机文件选择器需要图形会话，在线功能需要浏览器和网络。无桌面时可用 `./SAR --no-browser` 启动本地服务。

需要带图标的启动入口时，在解压后的 `SAR` 目录运行一次 `sh ./设置应用图标.sh`。脚本会为当前用户添加应用菜单入口，并在可写的程序目录生成 `SAR Downloader.desktop`，使用与 Windows 相同的蓝色 Logo。无需管理员权限；移动程序目录后重新运行此脚本。

WSL 中若提示 `xdg-open: no method available`，服务仍可使用：保持终端运行，在 Windows 浏览器访问终端显示的网址；网页界面无需安装 Linux 浏览器或 WSLg。原生文件选择器需要 WSLg / 图形会话。不要同时运行占用同一端口的 Windows 版。

免安装版自带 HTTPS 根证书，ASF 检索、下载和 DEM 引擎不依赖构建机器的证书目录。`./SAR --self-check` 会报告 `https.caBundle` 与 `https.rootCertificates`，后者应大于 0。使用需要自定义根证书的代理时，可在启动前设置 `REQUESTS_CA_BUNDLE=/绝对路径/ca-bundle.pem`（需包含完整受信任 CA 集）；也兼容 `CURL_CA_BUNDLE`、`SSL_CERT_FILE`。普通代理沿用 `HTTP_PROXY` / `HTTPS_PROXY`，无需关闭证书验证。

源码版与 Windows 版的检索恢复原有 urllib 连接方式、45 秒超时和系统 / 环境代理行为；Linux 免安装版仅额外指定包内 CA 证书。程序不再自行调整直连 / 代理优先级。ASF 返回 HTTP 400 且错误内容包含上游 HTTP 500 时，会明确提示上游目录服务异常；普通参数错误继续显示原始原因。

以下为源码版启动方式：

双击 **`启动SAR Downloader.cmd`**。优先使用本机 8770；被其他程序占用时自动选择空闲端口并打开实际地址。保留启动窗口；关闭浏览器不停止后台任务，关闭启动窗口停止服务。

同一用户重复启动时，只在实例身份、内部修订和后端能力均匹配的情况下复用已有服务与队列。旧版或无法确认的服务不会被停止或接管，请先暂停任务、等待停止后关闭旧启动窗口。不同电脑上的用户互不影响；同一后台的多个页面共用队列。需要独立运行时使用不同的 `--state-dir`、`--port 0` 和下载目录。显式指定的非零端口被占用时会报错，不会悄悄更换。

启动器优先选择本项目 `.venv`，或已配置的 `gdal` / `pytorch` 环境，不使用 conda base。缺少依赖时运行 `首次配置.cmd`；DEM 引擎另可运行 `配置DEM环境.cmd`。手动运行：

```powershell
conda activate gdal
python -B sar_app.py
```

## 功能模块

| 模块 | 功能 |
| --- | --- |
| Sentinel-1 | 选择整景 SLC / GRD 或 S1 Bursts。可勾选轨道和 DEM；轨道优先 POEORB，缺失时使用 RESORB，随所选影像一起加入队列。Bursts 可附带 XML。 |
| NISAR | 整景下载 RSLC / GSLC / GCOV / GUNW，或选择按范围裁剪 RSLC。裁剪支持频率 A / B、极化、2–16 个连接和可选高程范围。 |
| 辅助数据 | DEM 与轨道两个子页，可单独准备 Copernicus GLO-30、NISAR DEM v1.2 或 SRTMGL1 V003，或扫描本地 Sentinel-1 ZIP / SAFE 匹配精密 / 恢复轨道。 |
| 下载任务 | 统一队列、真实进度、下载前检查，按类型 / 状态筛选，开始、暂停、继续、重试和移除记录。 |

所有模块共用一个研究区，导入 SHP / KML / GeoJSON 或修改范围后，切换 Sentinel-1、NISAR、ALOS / ALOS-2 和辅助数据都会使用最新范围。各卫星分别保留日期、产品等筛选条件；仅恢复与当前完整研究区一致的检索结果和勾选，范围改变后需要重新检索。浏览器刷新后恢复工作状态，不自动检索或下载。

## 使用流程

首次打开显示欧洲、非洲与亚洲的大范围地图，不预设研究区，也不会自动检索。先填写经纬度并应用、在地图绘制矩形，或导入研究区文件，再检索影像或预览 DEM。已有工作状态仍恢复上次设置的研究区与筛选条件；“重置筛选”会清空研究区并回到大范围视图。

右上角“帮助”可弹出《SAR Downloader使用说明书》，支持新窗口打开或下载 PDF。说明书已包含在程序包内，离线也可查看。

1. 选择 Sentinel-1、NISAR 或 ALOS / ALOS-2，输入经纬度、在地图画矩形，或导入 SHP / KML / KMZ / GeoJSON，设置日期后检索。
2. 勾选影像；在 Sentinel-1 页面选择所需配套数据，或在 NISAR 页面选择整景 / 裁剪方式。
3. 打开“下载任务”，选择影像目录（绝对路径），保存设置。在“个人中心”登录 NASA Earthdata。
4. 加入队列后点击“开始下载”。仅加入队列不会发起影像传输；配套轨道 / DEM 会先访问公开目录核对可用性。

NISAR 裁剪在添加任务时固定保存目录，需先保存下载设置；已创建任务继续使用创建时的目录。所有任务共用设置中的 1–4 个并发名额，其中 DEM 处理最多 1 个、裁剪最多 2 景，每景连接数独立设置。新裁剪结果名为 `原影像名_clipped.h5`；同景不同范围 / 极化需选择不同影像目录，以免同名冲突。已有任务继续使用原来的输出路径和断点。

所选目录用于保存 Sentinel-1 / NISAR / ALOS / ALOS-2 影像、Burst XML 与 NISAR 裁剪结果。轨道自动保存到该目录下的 `orbit` 文件夹，DEM 原始瓦片和处理结果保存到 `dem` 文件夹，下载时自动创建；辅助数据单独下载也遵循相同规则。已经开始下载的任务继续使用原目录和断点，不自动移动文件。所有任务直接显示在统一队列中，不使用批次管理。

“下载前检查”检查本地登录状态、输出目录权限、同名目标冲突、磁盘空间及 DEM 处理依赖；开始、继续和重试时服务端也会检查。空间按磁盘汇总已知剩余下载量，并扣除普通文件已有断点；大小未知的任务会单独提示，裁剪、DEM 处理及缓存可能需要额外空间。检查不会访问远程数据源，实际授权和文件可用性仍以传输时的响应为准。

“下载轨道”为单一勾选项，默认启用。每景影像优先匹配精密 POEORB；精密尚未发布时才匹配恢复 RESORB，已有精密轨道不会重复添加恢复轨道。按卫星和完整获取时间核对，轨道文件自动去重。Bursts 使用目录返回的开始 / 结束 UTC 匹配，无有效时间时明确报错。暂未发布或历史恢复轨道缺失时显示提示，不创建空任务。配套数据目录查询失败则整批不入队，可取消该选项后重试。

配套 DEM 默认覆盖所选影像的联合外包范围，另可选当前研究区，配置缓冲、GeoTIFF / ISCE / 原始瓦片和高程基准。DEM 单独下载时也可通过地图设置研究区。CopDEM 是 DSM；缺失瓦片保留 NoData，椭球高转换使用 PROJ EGM2008 网格。

DEM 单独下载提供三个数据源：Copernicus GLO-30（免登录，EGM2008）、NISAR DEM v1.2（Earthdata 登录，已经是 WGS84 椭球高）和 SRTMGL1 V003（Earthdata 登录，原始 EGM96，可转换 WGS84 椭球高）。NISAR 与 SRTM 支持约 30 m 的 GeoTIFF / ISCE 输出；仅下载原始瓦片仍限 CopDEM。SRTM 含缓冲范围须位于南纬 56°至北纬 60°，海域或缺瓦片会明确报错。NISAR/SRTM 的预览为范围规划，启动时再核验源站实际覆盖。

SRTM 使用 LP DAAC 新的 Earthdata Cloud 地址，已替换失效的数据池地址；椭球高转换使用独立的 EGM96 网格并做抽样校验。NISAR 读取官方分层 VRT 和 COG，仅按需传输覆盖区域的数据块，暂停后重试会重新获取短期地址，不承诺保留已读块。两个数据源复用程序登录会话，不要求配置 `.netrc`；会话经工作进程标准输入传入，不写入任务 JSON、日志或命令行，临时签名地址仅保存在内存中。

NISAR 默认勾选“同时下载配套数据 DEM”，使用 NISAR DEM v1.2，固定生成 WGS84 椭球高 GeoTIFF：整景覆盖所选影像联合范围，裁剪覆盖研究区外包矩形，两者均外扩 5 km。DEM 自动去重并加入统一队列，保存到影像目录下的 `dem`；不需要时可以取消勾选。Sentinel-1、ALOS / ALOS-2 配套 DEM 继续默认使用 CopDEM。

NISAR 裁剪仅支持 **L-band、二维分块 RSLC HDF5**，使用研究区的经纬度外包矩形，并按原生雷达数据块扩展边界；导入多边形不会逐像元掩膜，不支持跨日期变更线。输出保留复数值、频率、极化及匹配的元数据。默认使用全部高度层；特殊 A/B 时间轴布局可能要求分别下载。下游科学处理兼容性需在实际流程验证。

## 认证、续传和存储

整景、Bursts、ASF EOF 和 RSLC 裁剪共享已验证的 Earthdata 令牌 / Cookie。密码登录后丢弃；会话和底图 Key 在 Windows 使用当前用户 DPAPI 加密，在 Linux 使用私有本地密钥加密（`private-settings.key`，权限 600），密文保存在用户数据目录的 `private-settings.bin`，关闭程序后再次打开会自动恢复，不包含在任务导出与发行包中。底图提供方、各自的 Key 和底图选择一并记住。退出登录暂停所有未完成任务并清除保存的 Earthdata 会话，底图设置保留；会话过期或官方授权失效时仍需重新登录。CopDEM 原始瓦片不需要登录。首次使用须自行在 Earthdata 官方站点完成账号与 ASF 授权。

队列和设置在 Windows 默认保存于 `%LOCALAPPDATA%\SARDownloader`，Linux 为 `${XDG_STATE_HOME:-~/.local/state}/sar-downloader`，浏览器工作状态使用本项目独立的 localStorage / IndexedDB。不会自动迁移旧项目的队列。可用 `--state-dir` 设置独立测试目录，或 `--port` 设置端口。`SAR_STATE_DIR` 显式覆盖状态目录。

加密设置暂时无法读取或解密时，程序仍可启动并提示；保存登录状态、底图 Key 等操作会明确失败，保留原文件，避免把已有设置覆盖为空。请检查当前 Windows 用户和文件权限，或恢复自己的有效备份后重试。

文件下载保留 Range / If-Range 断点并校验源文件身份。裁剪任务保存 HDF5 分块计划与已完成块校验记录，完成后清理自己的续传缓存。重启服务后未完成任务保持暂停。新工具与旧工具可并存，但不要对同一个输出文件同时下载。

下载任务提供三种清理操作，均保留正式影像、轨道和 DEM 成品：

- **移除**：只移除任务记录，保留断点文件。
- **清理闲置缓存**：清理未关联任务的临时文件，保留未完成任务的断点及完整高程转换网格。
- **清理未完成数据…**：暂停全部并等待停止后，预览、选择任务并确认；清理文件传输断点、NISAR `.h5.part` / 进度数据库、DEM 中间结果，任务进度归零并保持暂停。默认保留记录，也可勾选同时移除记录；单条任务提供“移除并清理未完成数据”入口。共享 DEM 瓦片和未完成的网格下载仅在全部未完成 DEM 任务都选中时清理，完整网格保留。预览 5 分钟内有效，任务或文件发生变化后需重新预览。

普通下载和裁剪记录分别逐条校验；损坏时保留原始备份 `downloads.json.invalid-*.bak` 或 `tasks.json.invalid-*.bak` 并提示，继续恢复其他有效记录。关闭程序会等待工作线程停止、保存状态后再释放用户目录锁。认证被官方源明确拒绝（HTTP 401）时清除对应旧会话，个人中心会重新显示登录入口。

前端按检索、辅助数据、偏好设置、队列显示划分模块，共用 `web/api.js` 处理超时与取消，`web/core.js` 提供 HTML 转义，入队响应和提示统一处理。列表只替换变化行，下载中每 1.5 秒刷新，空闲每 5 秒刷新，后台每 15 秒刷新。`storage.py` 统一原子写入，`task_scheduler.py` 统一跨引擎并发额度。下载预检查与实际传输共用目标路径计算；统一队列检查通过后直接应用操作，避免再次探测磁盘。

## 开发验证

```powershell
conda activate gdal
python -B -m unittest discover -s tests -p "test_*.py"
npm test
npm run check
python -B sar_app.py --self-check --native
```

测试使用本机 HTTP 源和小型 HDF5 样本验证 Range、暂停 / 重启、分块续传、复数值、元数据一致性、配套批量入队和任务生命周期。GDAL 检查在独立子进程运行，避免与 h5py 的 HDF5 DLL 混载。真实 Earthdata 登录与大型在线下载需在用户自己的账号和网络下验证。

第三方组件许可证见 `THIRD_PARTY_NOTICES.md`；程序源代码使用 MIT License。

### NISAR 范围下载

选择“按范围裁剪 · RSLC”并勾选影像后，点击“设置范围并下载”。弹窗初始使用研究区的外包矩形，可输入经纬度或拖动四角，并设置频率、极化、并发、高程层和影像目录。本次裁剪范围不改变检索研究区。

裁剪经纬度显示并使用两位小数；拖拽、导入或输入的范围按 0.01° 向外取整，避免缩小研究区。若下载根目录为 `Test`，新建的裁剪成品保存为 `Test/rslc/<场景编号>_clipped.h5`，配套 DEM 继续保存在 `Test/dem`。根目录的 `summary.txt` 累计记录请求范围、频率/极化、高程层、逐景编号、UTC 成像时间、升降轨、Track/Frame、文件位置与完成状态。清单包含已提交任务，不能将待下载记录视为已下载；已有断点任务继续沿用原路径。

弹窗中“同时下载 DEM”与 NISAR 侧栏选项一致，默认勾选；DEM 使用确认后的下载范围外扩 5 km，输出 WGS84 椭球高 GeoTIFF 到影像目录下的 dem。点击“开始下载”会创建并启动本次影像与 DEM 任务，失败的任务可在“下载任务”中继续或重试。整景模式不弹范围窗口，仍直接加入队列。

### ALOS / ALOS-2

“ALOS / ALOS-2”模块通过下拉框选择卫星。ALOS 默认检索历史观测时段，可选 L1.1（复数影像）、L1.0（原始信号）、L1.5（幅度影像）、RTC 12.5 m / 30 m（后向散射影像；此数值不是附带 DEM 的原生精度）。ALOS-2 默认从 2014 年开始，当前提供 ASF 公开 ScanSAR L1.1；覆盖仅为部分全球区域，新观测随 JAXA 开放逐步收录。

ALOS 使用 ASF SearchAPI；ALOS-2 使用 NASA CMR 中 ASF 管理的 `ALOS2_L1_PSR2` 集合，不依赖新 Python 包。ALOS-2 的目录、平台和产品严格核对，Path / Frame 列表与完整研究区面在本地再次精确筛选。候选达到上限时需缩小条件或提高上限。

影像以原始 ZIP 整景加入统一队列，登录 Earthdata 后开始；可选择配套 Copernicus DEM（默认不勾选），按影像联合范围外扩 5 km 输出 WGS84 椭球高 GeoTIFF 至 dem。ALOS 系列不匹配 Sentinel-1 的 POEORB / RESORB，也不使用 NISAR 的 HDF5 裁剪引擎。大范围 DEM 如超出处理上限，可取消配套 DEM，在辅助数据模块按研究区单独下载。

官方说明：[ASF SDK 数据集](https://github.com/asfadmin/Discovery-asf_search/blob/master/asf_search/constants/DATASET.py)、[ALOS-2 官方集合元数据](https://cmr.earthdata.nasa.gov/search/concepts/C3315903479-ASF.umm_json)。
