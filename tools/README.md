# 发布到 GitHub

本系统由两个仓库管理，源码以仓库为准：

| 服务 | 运行容器 | GitHub 仓库 | 发布方式 |
| --- | --- | --- | --- |
| 归档控制台、分类服务、独立校验、视频截图 | muli-archive-archive-console-1、muli-sorter-muli-sorter-1、muli-sorter-muli-postcopy-1、muli-sorter-muli-sorter-previews-1 | https://github.com/muqeing/muli-archive（公开） | 本目录就是 git 工作副本，改完运行 tools/publish-archive.sh |
| 拷贝与备份 | app-muli-ingest-1 | https://github.com/muqeing/muli-ingest（公开） | 改 work/github/muli-ingest 仓库源码后运行 tools/publish-ingest.sh |

约定：每次上线前先跑通测试，再做镜像与切换；上线回读通过后立即推送，提交信息写清改了什么、影响哪个服务。纯客户端 CLI 可在现有 API 上使用，提交需明确标注是否已更新 NAS。两个仓库都以 Apache License 2.0 发布；发布脚本检查源码、测试、构建后提交，没有差异时不会产生空提交。公开提交前仍需审查内容和授权。

2026-10-13 起 CLI 与服务共用包版本、测试和构建产物：归档客户端为 `muli-archive`，拷贝客户端为 `muli-ingest-client`。两个仓库每次 push 均检查客户端并生成安装包。拷贝发布脚本不再从历史发行 bundle 删除并重建仓库源码，避免把新 CLI 或后续修复覆盖掉。NAS overlay/快照仅作为部署证据，不再作为源码权威入口。

涉及共享快照/签名规则的更新，需要同时检查发布方和读取方的实际安装代码。特别包括独立视频截图进程；进程仍在运行不能证明它能识别新清单。按每个相关服务的旧镜像检查导入依赖、真实清单接受与各自职责的回归，再验证实际截图生成及页面显示。

历史：2026-09-29 之后拷贝仓库停了十天，归档侧一直没有仓库；2026-10-09 把两个仓库都对齐到线上实际运行的包（逐文件 sha256 比对）。
