# SAR Downloader 发布

## 软件内检查更新

在首次发布前将根目录 `update_config.json` 配置为实际公开仓库，例如 `{"repository":"你的用户名/SAR-Downloader"}`。该文件随源码和两平台免安装包一起分发；不要填示例仓库发布。暂未创建仓库时可保持空字符串，软件会提示尚未配置，仍可正常使用下载功能。个人在更新窗口填写的仓库覆盖随包默认值，并独立保存在用户目录的 `update-settings.json`。

后续更新按以下步骤发布：

1. 修改 `app_runtime.py` 中的 `VERSION`，例如从 `1.0` 改为 `1.1.0`，并更新内部 `REVISION`。界面版本从后台读取。
2. 完成测试，按原流程构建源码、Windows 和 Linux 程序包，并校验同一套发布材料。
3. 在上述公开 GitHub 仓库创建正式 Release，tag 使用 `v1.1.0` 等数字版本，填写 Release 更新说明。
4. 上传 `SAR-v1.1.0-windows-x64.zip`、`SAR-v1.1.0-linux-x86_64.tar.gz` 和 `SAR-v1.1.0-source.zip` 等附件。程序只识别同仓库、同 tag、同版本的正式上传附件；草稿和预发布版不作为正式更新。

用户点击“检查更新”后，程序使用 GitHub 的最新正式 Release API，按数字版本比较并匹配当前平台附件。“下载新版”直接打开 GitHub 官方附件，由浏览器完成下载；不自动停止任务、解压、安装或替换正在运行的程序。若没有匹配的安装包，会显示发布页和可用的源码下载，不把它误报为无需更新。当前公开仓库查询不需要用户设置 GitHub Token。

同版本替换附件无法通过版本比较通知用户；应发布递增的新版本。GitHub 网络不可达、访问频率受限或仓库尚无 Release 时，更新窗口显示可重试的错误，影像下载功能照常使用。仓库地址发生迁移时请更新配置并重新发布，已有用户也可在“更新来源”中修改。

当前版本号以 app_runtime.py 的 VERSION 为准。以下文件名中的 1.0 仅为当前版本示例。构建前安装 requirements-lock.txt；裁剪引擎与统一队列均随源码打包。以下命令中的 SAR 为打包产物的兼容内部文件名。

本次按维护者要求以 **1.0 替换构建**交付，内部修订见 `app_runtime.REVISION`。两平台新包完成验证后清理本地旧发行包，仅保留最新一套；不要把新源码与旧程序混用。若旧 Release 已启用不可变发布，不能直接替换附件，应在正式操作前核对 GitHub 设置；本地构建不会修改任何线上 Release 或 tag。

两平台完成后，将同一源码树对应的三份压缩包放入同一个目录，执行：

```sh
python -B scripts/verify_release_set.py release-artifacts
```

该命令比较源码 ZIP 的逐文件指纹与两平台 `_internal/build-info.json`，一致后生成 `RELEASE_MANIFEST.json` 和完整 `SHA256SUMS.txt`。将三份压缩包、两个清单和 RELEASE_NOTES.md 作为同一套发布材料。源码树在构建期间变化会导致构建失败，需从固定输入重新构建。

# 发布与构建

仓库只提交源码。`release-artifacts/` 下的 ZIP 作为 GitHub Releases 附件；不上传个人队列、虚拟环境、影像、账号配置或构建中间文件。

## 本机构建 Windows x64

在 Windows 上用 64 位 Python 3.11 创建**干净的构建环境**。不直接冻结装有完整 GDAL / PyTorch 的开发环境。

```bat
python -m venv .build-env
.build-env\Scripts\python.exe -m pip install -r requirements-build.txt
.build-env\Scripts\python.exe -B scripts\setup_dem.py --managed --build --prefix .runtime\dem-build\gdal
.build-env\Scripts\python.exe -B -m unittest discover -s tests -p "test_*.py" -q
.build-env\Scripts\python.exe -B scripts\build_release.py
.build-env\Scripts\python.exe -B scripts\smoke_release.py release-artifacts\SAR-v1.0-windows-x64.zip
.build-env\Scripts\python.exe -B scripts\smoke_source.py release-artifacts\SAR-v1.0-source.zip
```

`requirements-lock.txt` 固定核心依赖及间接依赖；`requirements-build.txt` 固定打包工具。DEM 在独立的 conda-forge gdal 环境冻结，`build_release.py --dem-python` 可显式指定其解释器；默认读取配置文件。构建时包含该环境的 GDAL / PROJ 数据库、原生库和许可证清单。主程序与 DEM 引擎各自拥有 DLL 目录；不会把整个开发环境直接复制到发行包。Node.js 仅用于开发测试。

输出包括：

- `SAR-v1.0-source.zip`：清洁的源码及首次配置入口。
- `SAR-v1.0-windows-x64.zip`：`SAR.exe`、`_internal`、`dem-engine` 和许可文件。EGM2008 网格是运行时首次使用时下载的数据，不随包分发。
- `SAR-v1.0-linux-x86_64.tar.gz`：Linux 原生可执行文件、独立 DEM 引擎、启动脚本和许可文件。
- `SHA256SUMS.txt`：当前目录已生成发布包的 SHA-256。

`python scripts/build_release.py --source-only` 可仅生成源码 ZIP。打包脚本使用明确的源码清单，排除 `.runtime`、虚拟环境、截图、用户文件和构建产物；输出必须位于项目子目录。

源码版默认用户目录的旧队列迁移，在进程锁保护下只复制一次、不覆盖新记录。Windows 包的用户数据与资源目录分开，升级不会把队列写进 `_internal`。文件选择器使用同一 EXE 的 `--native-dialog` 子进程入口，不依赖外部 python.exe。

## 本机构建 Linux x86_64

Linux x86_64 在 Linux 上原生构建，不能从 Windows 交叉冻结。建议使用 Ubuntu 22.04（glibc 2.35），在项目下创建干净的 gdal 环境：

```sh
python -B scripts/setup_dem.py --managed --build --prefix .runtime/linux-build/gdal
.runtime/linux-build/gdal/bin/python -m pip install -r requirements-build.txt
.runtime/linux-build/gdal/bin/python -B -m unittest discover -s tests -p 'test_*.py'
.runtime/linux-build/gdal/bin/python -B scripts/build_release.py --dem-python .runtime/linux-build/gdal/bin/python
.runtime/linux-build/gdal/bin/python -B scripts/smoke_release.py release-artifacts/SAR-v1.0-linux-x86_64.tar.gz
```

Linux 构建与原生选择器自检需 X11 / XWayland 图形会话；CI 可用 `xvfb-run -a`。tar.gz 保留 `SAR`、`dem-engine/SAR-DEM` 和启动脚本的执行权限，以及 PyInstaller 所需的内部符号链接。发布包面向 x86_64、glibc 2.35+；不适用于 ARM64 或 Alpine / musl。Linux 会话使用 cryptography 加密，密钥保存在当前用户状态目录，权限 600。

Linux 包同时包含 `SAR.svg` 和 `设置应用图标.sh`。后者只向当前用户的 XDG 数据目录注册应用菜单与图标，并在可写的解压目录生成 `.desktop` 入口；不需要管理员权限，移动目录后重新运行。发布自检会在隔离用户目录验证注册和启动，避免修改维护者的真实应用菜单。

发布前另运行 `scripts/smoke_release.py <压缩包> --online`，通过实际冻结程序的 `/api/search` 验证 Sentinel-1、S1 Bursts、NISAR 和轨道匹配。离线自检同时确认主程序与 DEM 的根证书数量大于 0、证书来自解压目录；仅确认 PEM 文件存在不足以证明 HTTPS 可用。在线验证不需要 Earthdata 登录，也不会下载整景影像。

## GitHub

1. 新建仓库，将源码 ZIP 中的文件上传或通过 Git 提交，保留 MIT LICENSE 和第三方说明。
2. 仓库包含 `.github/workflows/build.yml`。手动运行 Actions，或推送 `v*` 标签后，会分别在 Windows、Linux 上测试、打包并上传构建产物。
3. 下载 Actions 构建产物或使用本地生成的文件，在 GitHub Releases 创建版本，附加所需平台的包和校验文件，填写功能与限制后发布。

工作流只有读取仓库和上传 Actions 产物的权限，**不会自动发布 Release**。本地制作不需要 GitHub 账号，也不会自动提交或推送文件。

## 验证范围

回归测试覆盖真实本机字节传输、范围续传、登录跳转的认证隔离和队列迁移。`smoke_release.py` 在中文与空格路径下解压、移除 Python/Conda 的 PATH 与配置变量，验证打包库、Tcl/Tk、原生选择器子进程、静态资源、队列接口及未登录拦截。

`smoke_source.py` 实际执行源码 ZIP 中的首次配置，创建独立 `.venv` 并安装锁定依赖，随后检查中文与空格路径下的启动器会选用该环境。

这些检查不能替代在另一台干净 Windows 上的验收。发布前另测一次真实账号登录、小范围检索及真实下载 / 暂停 / 继续；勿把账号或 Cookie 附加到发布包。免安装包暂未做代码签名。
