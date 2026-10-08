# 发布到 GitHub

本系统由两个仓库管理，源码以仓库为准：

| 服务 | 运行容器 | GitHub 仓库 | 发布方式 |
| --- | --- | --- | --- |
| 归档控制台、分类服务、独立校验 | muli-archive-archive-console-1、muli-sorter-muli-sorter-1、muli-sorter-muli-postcopy-1 | https://github.com/muqeing/muli-archive（私有） | 本目录就是 git 工作副本，改完运行 tools/publish-archive.sh |
| 拷贝与备份 | app-muli-ingest-1 | https://github.com/muqeing/muli-ingest（公开） | 改 work/ingest-source-release 源码后运行 tools/publish-ingest.sh |

约定：每次上线前先跑通测试，再做镜像与切换；上线回读通过后立即推送，提交信息写清改了什么、影响哪个服务。两个脚本都带语法门禁，没有差异时直接退出，不会产生空提交。

历史：2026-09-29 之后拷贝仓库停了十天，归档侧一直没有仓库；2026-10-09 把两个仓库都对齐到线上实际运行的包（逐文件 sha256 比对）。
