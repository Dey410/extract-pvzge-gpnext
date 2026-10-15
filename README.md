# PvZGE Lite 资源提取

在 GitHub Actions 的 **Extract PvZGE Lite Tauri Assets** 手动运行一次，唯一输入
`release_tag` 留空时使用最新非草稿 Release（包含 Pre-release）的非 macOS Lite ZIP。
Actions 固定执行核心测试、Cocos 音频修补、ASTC 转换、全新目录组装和 ZIP 重开验证。
任何未知引擎、补丁布局、纹理映射或封存验证失败都不会上传正式资源 ZIP。

产物是 `GardendlessResources.zip` 和独立的 `reports/`。ZIP 内只有 `docs/` 运行资源：
实际选择的 ASTC（PNG/AVIF/WebP 共用一条转换路径）、游戏代码、音频和必要的网页图片。native 原图、解码中间 PNG、
元数据备份及已被 ASTC 替代的 PVR/PKM 保留在构建源目录或 reports 中，不进入正式资源 ZIP，不用删除源文件来减包。
每次构建使用全新的 run 输出目录。

音频只使用游戏自带的 DOM Audio / WebAudio。显式 `audioLoadMode` 优先；没有显式模式时，
`assets/resources/config.json` 的 `music/` AudioClip UUID 映射决定音乐走 DOM，其他音效走 WebAudio。
不再逐文件 ffprobe 或按十秒阈值分流。DOM 等待有效 metadata，错误、abort、八秒超时均拒绝；
WebAudio decode 失败会传给外层 Promise。one-shot 通过实际 `onended` 结算播放、释放源节点和 Gain，
不会用原始时长计时器猜测变速后的结束时间。此修改经过隔离 JS 执行验证，仍需设备验收实际媒体播放。
目前仅接受已核对布局的 Cocos 3.8.4；不自动尝试其它播放器或引擎布局。

ASTC 固定采用 `astcenc 5.6.0 -cl -fast`，每编码进程一个线程。规则保持：
指定旋涡图集、最大边不超过 512 或 PNG 位率至少 6 bits/pixel 用 4×4；
最大边至少 2048 且最大可见连通区占比至少 0.35 用 6×6；其余用 8×8。
Pillow 固定 12.1.1，AVIF/WebP 按解码 RGBA PNG 大小分类。已有 ASTC 经块、尺寸和长度核对后复用。
元数据只选择一个 ASTC 格式，设备必须支持 ASTC。

包包含 `docs/__gardendless/package/resource-manifest.json`（恰八字段）及
`file-index.json`（`{schemaVersion:1,files:[{path,sizeBytes,sha256}]}`）。索引覆盖所有运行文件，
排除这两个固定元数据文件，路径按 UTF-16 顺序严格排序且唯一。manifest 保存游戏/Cocos版本、
GP 事实、真实 producer commit、`minHostAbi:1` 和索引原字节 SHA256；包身份是 manifest
原字节 SHA256，不写回自身。完整工具、输入资产和运行详情留在包外 reports。
ZIP 封存后重新核对成员、路径、CRC、实际展开大小和每项 SHA256。摘要验证一致性，不认证来源。

资源包要求 GardendlessLoader 0.9.0 / Host ABI 1。页面在检查 `window.__gardendlessHost.config.hostAbi`
之后才创建真实 module 入口；旧宿主显示升级文字，游戏入口不会执行。

本地执行（Python 3.12+、Node 24、ASTC 编码器 5.6.0）：

```bash
python3 -m pip install -r scripts/requirements-astc.txt
python3 -m unittest discover -s tests -t . -v
python3 -m scripts.patch_audio_runtime SOURCE_DOCS REPORTS/audio-runtime-patch.json
python3 -m scripts.transcode_textures_astc SOURCE_DOCS REPORTS/astc-texture-summary.txt
python3 -m scripts.resource_package SOURCE_DOCS NEW_OUTPUT REPORTS/astc-texture-map.csv \
  --producer-commit ACTUAL_COMMIT_SHA --report REPORTS/package-build.json
```

测试夹具和失败暂存文件保留，不执行递归删除。
