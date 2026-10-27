# 参与开发与反馈

普通使用问题请使用 Issue 模板，附上系统、CPU 架构、程序版本/内部修订、数据类型、复现步骤、预期与实际结果，以及脱敏日志。安全问题见 SECURITY.md。

源码验证使用 Python 3.11/3.12，安装锁定依赖。Windows 可使用已有 gdal 或 pytorch 环境；DEM 与 h5py 验证在不同子进程运行。

```sh
python -B -m unittest discover -s tests -p 'test_*.py'
npm test
npm run check
```

所有试验使用独立状态目录和随机端口，例如：

```sh
python -B sar_app.py --no-browser --port 0 --state-dir ./.verification/manual-state
```

提交修改请说明触发问题、变更后的行为和验证证据。保持界面与队列格式兼容；不要提交个人任务、账号、Cookie、Key、下载数据、构建环境或未脱敏日志。免安装包放 Releases，构建方法见 RELEASING.md。
