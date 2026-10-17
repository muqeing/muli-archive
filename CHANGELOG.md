## 0.18.0 — 2026-10-17

- Add opt-in generated-page retention with a shared archive reference gate, durable write fences and a metadata-bound incremental index. Stream canonical historical requests and preserve unknown/retained/active work. Completed self-contained requests can release display-page pins without removing original controls.
- Add the versioned JSON `muli-archive-maintenance` CLI, standard-library host zipapp, disabled configuration and user-unit templates. Default invocations preview; shadow updates only its own index and never freezes or deletes pages. Timer installation and real deletion require separate deployment authorization.
- Limit execution to old generated model/HTML pairs, preserve current/latest/recent and referenced pages, bound batch/byte/freeze budgets, journal outcomes and reconcile only the original classifier after an interrupted run. No recursive or media deletion.
- Keep source admission, target verification, tickets, execution and original audit retention unchanged; derived-index failure disables cleanup rather than original control writes. Synthetic tests and source distribution do not certify production compatibility or natural timer operation.
- Initial NAS shadow rollout passed 546 isolated archive-role checks, exact 110-file readback, desktop/mobile GET and CLI version checks; original business controls and other service instances were preserved. The shadow timer is active with deletion off. Record initial indexing and explicit shadow costs, transient cold-start health delay, and the user-journal observation limitation; the 24-hour natural observation remains pending. No real media or page cleanup acceptance is claimed.

## 0.17.0 — 2026-10-16
- Restore completed-job feedback after a receipt reader device number changes. Bind the exact original request, persistent roots and file plans; accept only the three transient reader fields when the projected full manifest digest matches. Preserve source admission, target verification and unverified-copy protections.
- Stream original request binding with bounded buffers and cache metadata only; avoid decoding historical page models. Suppress an old active revocation warning only after a newer fully confirmed consumer acknowledgement; retain the audit history and partial warnings.
- Enforce the four opened-page snapshot limit inside the cache itself, including background page builds. Reopening an identity refreshes its place; failed serialization preserves existing snapshots. No change to media checks, tickets, execution or retention cleanup.
- NAS phase-one read-only acceptance completed with exact role installation hashes, desktop/mobile pagination and CLI checks; initial failures and recovery remain documented. No media or retention deletion acceptance.

- Accept multiple current copies of a historical origin in the browser summary; reuse metadata-bound original projections for display while execution reads remain strict.
- Keep startup history validation within small temporary files by streaming indexed winners; workflow handoff renewal does not invalidate existing media evidence tokens or requeue old classifications.
- Add opt-in durable classification closure from a fresh complete revision-bound archive handoff; retired models leave the active queue, partial work stays active, new revisions remain discoverable.
- Add explicit classification cancellation and query-only paged history CLI; neither operation deletes media or cancels archive execution.
- Freeze legacy reservation identity with persistent filesystem ID plus inode, preserving NAS reboot compatibility without weakening replacement checks.
- Add identity-bound transactional reservation queries, immutable legacy migration and bounded-memory rollback export; never fall back to stale JSON after index activation.
- Add an explicit-build derived history catalog with latest unit/content routing and SQL pagination. Current scopes still revalidate original receipts, retained-copy protections and move journals. Bound recent job summaries to20 and compressed report caches to4/32MiB.
- Preserve default service behavior behind activation flags. No automatic history expiry/deletion; existing handoff freshness and cleanup authorization remain. Source publication is independent from NAS migration and production acceptance.

## 0.16.4 — 2026-10-16

- Stream atomic archive-state JSON writes through a bounded buffer instead of constructing the full historical reservation output in memory; preserve record format, fsync/rename durability and fail-closed writes.
- Add explicit recovery for a persisted confirmed request interrupted before job publication. Validate the original ticket, submission time/scope, roots, request digest and reservations; retain the original job ID and all execution checks. Reads and ordinary submissions never auto-recover.
- Show a guarded “恢复原提交” action when request and confirmation evidence exist; add JSON CLI `recover-submission ID --execute --confirmed`. Recovery executes the original copy/move plan, never the page's newer selections.

## 0.16.3 — 2026-10-13

- Add versioned JSON archive API client `muli-archive`, with read-only discovery, unconfirmed plan templates, explicit preflight/submission, guarded move authorization and bounded GET waits.
- Preserve detailed service diagnostics, unknown write outcomes and confirmation ticket binding; no automatic retry or media access by the client.
- Build and check the installed CLI wheel on every GitHub update; client, package metadata and future health version share the release version.
- Make the ingest Git worktree authoritative in the publishing helper; historical deployment bundles no longer overwrite maintained source/CLI files.
- This release adds client tooling. Publishing source does not restart or update existing NAS images.
- Make synthetic content-proof test fixtures self-contained and check the shared coordination protocol against the maintained ingest repository in CI.

## 2026-10-11 photo-clock-original-1（已上线验收）

支持读取独立的已完成 ARW 拍摄时间校正回执，绑定原始摄入清单、原独立内容校验及校正后的文件摘要/身份；分类使用校正后的时间和日期候选，归档使用新内容证明。原始证据保留，未完成、冲突、跨批引用或错误回执继续阻断。可选回执只使相关批次的分类和来源缓存失效；程序不会自动修改原片。

原696项本地全套及新增4项分类检查通过，字段工具9项和整批合成写入3项通过；分类66/归档303项NAS隔离检查通过且无跳过。两服务已上线，96/97安装文件精确匹配，分类1GiB/归档2GiB及其他四服务实例配置保持；正式1440/390页面及校正后的日期/候选/摘要回读通过。已明确授权的一批原片完成等长日期字段改写、逐文件完整内容和逆向原摘要回读，以及独立ExifTool日期/子秒/时区验收。未执行新的归档提交、移动或删除。

首次隔离检查发现分类基底缺少归档诊断依赖；保留失败镜像、日志和明确失败结果，随后按角色修复打包：分类只覆盖三个相关模块，归档覆盖四个模块。原源码包不变，修复清单与检查单独留存。正式页面冷启动约97秒；校正后重新分类期间旧页面暂缺该批次，等待新生成恢复。验收最初误将六个范围外附属单元计入原片数量，已按授权原片路径核对；上述过程记录均保留。

## 2026-10-11 queue-candidate-refresh-1（已上线验收）

- 将分类完成证据与项目目录候选拆开，已验证批次不因无关项目变化重新排队。
- 空页面区分后台进度、异常/附属文件、筛选无结果和旧快照，进度文字更新不改计划。
- 旧页面预检按选中素材、项目和必要依赖验证当前兼容性；未知旧报告或选中证据变化继续阻断并定位，不重绑报告、不添加新素材，live核对与提交票据保持。
- 截图HTML与模型JSON分开缓存，修正大于64MiB已生成报告重复写出的问题；输入证据读取上限不变，半成品和篡改保护保持。
- 修正测试引用旧checkout导致spawn worker与主进程使用不同算法。690本地、分类41/归档266项NAS隔离检查及1440/390正式GET页面验收通过。分类95/归档96安装文件精确匹配，分类1GiB/归档2GiB及其他四服务实例配置保持；原业务控制和旧来源保留保护保持。未执行真实媒体业务或性能测试。首次部署保护范围过大与首次发布健康心跳过期的失败记录保留，未盲重启或重放。

## 2026-10-11 直接移动来源属性兼容（已上线验收）

复用已确认内容校验时，仅来源ctime变化、其余磁盘/文件身份/大小/mtime与单链接保持一致，直接移动在建立意图前只重读该文件并比对绑定BLAKE3；核对期间任何变化继续拒绝。新的实际身份绑定后续直接rename与恢复记录，意图建立后的检查不放宽。无变化文件仍不重读，属性重新核对计入真实hashed_files和字节进度。非ctime身份变化错误增加具体差异字段。清单版本不一致改为明确页面清单更新提示并给出保存草稿/重新加载/查找旧报告草稿步骤，不强行替换报告编号。658项本地检查、186项NAS隔离检查（无跳过）及1440/390宽度合成、正式页面验收通过。归档服务已切换 direct-source-attributes-1-archive，94个安装文件精确匹配，其他五服务实例/配置与归档2GiB保持；失败任务记录保持，未重试真实任务。本次没有执行真实预检、提交、移动或删除，不宣称真实新归档性能已验收。

## 2026-10-10 归档错误定位（已上线验收）

预检、提交以及移动/复制执行错误新增结构化位置：拍摄段编号与名称、素材编号/拍摄时间、对应文件来源及目标路径。页面支持定位当前显示的拍摄段，较多文件折叠查看。清单版本不一致明确显示计划级阻断与两份报告编号，不把尚未核实的素材误报为损坏。只补充检查上下文和展示，不额外读取媒体、放宽验证、修改归属或自动重交；恢复成功清除旧诊断。647项本地检查、NAS隔离175项检查（无跳过）及正式1440/390宽度电脑、手机页面验收通过。归档服务已更新为 archive-error-location-1-archive，94个安装文件精确匹配；保留2GiB及其他五服务实例/配置。正式页面已回读ready，刷新提示约1秒消失。本次未执行真实预检、提交或移动，不代表另一台电脑旧草稿已恢复。初次冷启动页面加载85.5秒，仍需等待后台生成。首次构建的FROM标识错误和首次隔离检查缺少合成资产的失败记录均保留，显式纠正后通过。

# 变更记录

逐次上线的范围、证据与回读记录见 README.md（保留历史失败），本文件只列目录级变动。

- 2026-10-10 修复独立视频预览服务与归档清单的快照身份兼容；显示后台故障/排队原因和待生成视频名称；增加无时长图像流的首帧解码，新解码版本立即重试旧错误并复用正常截图。

- 2026-10-09 归档控制台与拷贝服务的包合并为同一份源码；快照编号改按素材计算；来源文件仅属性变化时改用内容摘要证明；批次反馈面板显示回执撤回原因；页面缓存键加入程序指纹。
- 2026-10-08 以各容器实际生效的 site-packages 为基准完成合并，归档控制台与分类服务同时上线。
