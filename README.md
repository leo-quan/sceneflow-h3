# SceneFlow H3 Web App

局域网连续故事视频生成界面，使用本机 ComfyUI `8188` 和 MiniMax H3 Director 作为生成引擎。

## 开源许可

Copyright 2026 leo_quan (jujur@qq.com).

SceneFlow H3 的 `WEB-APP/` 源代码及随附文档采用 [Apache License 2.0](LICENSE) 授权，可依许可条款使用、修改和分发。转载或分发时请保留许可证文本及原有的版权和归属声明，并在修改的文件中标明变更。

本项目通过 API 调用独立运行的 ComfyUI；ComfyUI 本体、MiniMax H3 Director 节点、模型权重和 FFmpeg 均不包含在此授权范围内。安装、获取或再分发这些外部组件时，请分别遵守其自身的许可条款。`requirements.txt` 中的 Python 依赖同样各自适用其许可证。

公开发布时仅包含源码、静态页面、脚本、`requirements.txt`、`README.md` 和 `LICENSE`；不要上传本地的 `BAK/`、`data/`、`uploads/`、`videos/`、`.venv/`、日志、模型文件或任何密钥。`.gitignore` 只对尚未纳入版本控制的文件生效，发布压缩包前也请检查实际内容。

## 安装与运行

需要 Python 3.12、FFmpeg 和已运行的 ComfyUI（默认 `http://127.0.0.1:8188`）。请将本项目克隆到 ComfyUI 根目录的直接子目录中（如 `ComfyUI/sceneflow-h3/`）；程序通过父目录定位 ComfyUI 的 `input/` 和 `output/`。ComfyUI 中还需安装 MiniMax H3 Director / DirectorRefine 节点及 `comfy_client.py` 所指定的 MiniMax H3 模型文件。此仓库不包含 ComfyUI、节点或模型权重。

在项目根目录创建独立的 Python 虚拟环境并安装依赖（不要提交 `.venv/`）：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

首次启动前设置 `SCENEFLOW_INITIAL_PASSWORD`（至少 12 个字符），供初始账号 `admin` 和 `leo` 使用；请为这两个账号分别修改密码，不要使用示例密码。已有数据库的账号不会被覆盖。Linux 上启动服务：

```bash
SCENEFLOW_INITIAL_PASSWORD='请替换为自己设置的长密码' ./start-background.sh
```

浏览器访问 `http://本机IP:9018`。服务监听 `0.0.0.0`，首次启动会创建本地数据库及初始用户。停止服务：

```bash
./stop.sh
```

## 数据目录

- `data/app.db`：项目、片段、任务和视频索引
- `uploads/`：人物参考图副本
- `videos/project_<id>/segment_<n>/`：应用归档的独立 MP4
- `webapp.log`：服务日志

应用一次只向 GPU 提交一个任务。时长和帧数为：2秒56帧、3秒73帧、5秒124帧、6秒158帧、8秒192帧、10秒243帧、12秒294帧、15秒362帧、18秒447帧、20秒481帧；接续片段使用22帧 MiniMax H3 motion context。

画布提供16:9、9:16、1:1、4:3、3:4、3:2、2:3、4:5、5:4和21:9低显存预设。所有尺寸均为32的倍数，并限制在约0.2MP显存预算内。

Web服务重启时会使用SQLite中持久化的 ComfyUI `prompt_id` 自动对账：恢复运行/等待任务，补归档已完成任务，并重新排入尚未提交的本地任务。不要手工删除ComfyUI历史和队列，否则无法恢复对应任务。
