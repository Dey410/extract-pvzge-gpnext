# PvZGE Lite 资源提取

在 GitHub Actions 的 **Extract PvZGE Lite Tauri Assets** 手动运行一次，唯一输入
`release_tag` 留空时使用最新非草稿 Release（包含 Pre-release）的非 macOS Lite ZIP。
Actions 固定执行核心测试、Cocos 音频修补、ASTC 转换、全新目录组装和 ZIP 重开验证。
任何未知引擎、补丁布局、纹理映射或封存验证失败都不会上传正式资源 ZIP。

产物是 `GardendlessResources.zip` 和独立的 `reports/`。ZIP 内只有 `docs/` 运行资源：
实际选择的 ASTC（PNG/AVIF/WebP 共用一条转换路径）、游戏代码、音频和必要的网页图片。native 原图、解码中间 PNG、
元数据备份及已被 ASTC 替代的 PVR/PKM 保留在构建源目录或 reports 中，不进入正式资源 ZIP，不用删除源文件来减包。
每次构建使用全新的 run 输出目录。

音频全部走 WebAudio，不再创建 `<audio>`。原因：
- iOS 卡顿：游戏每帧都给音乐设置 `playbackRate`，Cocos 的 DOM 播放器每次都重写 `preservesPitch` 和
  `playbackRate`，WebKit 因此每帧向 GPU 进程发 IPC 并重设 AVPlayer，画面跟着卡。
- BGM 丢失：游戏靠当前音乐的 `currentTime` 切下一首。DOM 元素加载失败或被系统暂停后，Cocos 不会恢复，
  时间不再前进，整关不再有 BGM。
- iPhone 上 WebKit 锁定媒体元素音量，游戏的音乐音量和暂停静音不起作用。

`AudioPlayer.load`（AudioSource 和资源下载走这条路径）遇到下面两类音频时，交给 `scripts/music_player.js`
提供的播放器：`assets/resources/config.json` 中 `music/` AudioClip UUID 对应的音乐（无论调用方显式指定什么模式），
以及游戏显式要求 DOM_AUDIO 的音频（环境音、僵尸和僵王的循环音、禅境花园和世界地图音乐等）。这个播放器：
- 载入时不读文件、不解码，所以沙盒模式一次载入两百多首音乐也不占内存。
- 第一次播放时取文件并解码，同一首按 URL 共享。`stop`/`destroy` 后放入 32 MiB 的 LRU，超出时淘汰最早释放的。
- 播放时钟从请求播放时开始按 `AudioContext.currentTime × playbackRate` 推进，解码完成后从当前位置接入。
  首次播放会损失解码耗时那一小段开头，但切歌时机不变。
- 解码失败只记一次错误，时钟照常走，游戏仍会按时切到下一首。
- 速率和音量只在值变化时写；结束用实际 `onended`；中断沿用 Cocos 的 pause/resume 事件。
- 等待上下文时和引擎的 one-shot 一样走 `runContext()`：它检查 AudioContext 的实际状态并调用 `resume()`，
  不依赖可能漏发的 statechange 事件。

iOS WebKit 在页面只播 WebAudio 时使用 AmbientSound 音频会话，有 `<audio>` 在播时使用 MediaPlayback。
音乐改走 WebAudio 后，iPad 从后台回来时 AudioContext 显示 running 但时钟停止。`scripts/audio_context.js`
在引擎创建 AudioContext 之前设置 `navigator.audioSession.type = "playback"`，保持之前 DOM 音乐时的会话类别；
游戏重新显示时把 AudioContext 的 `state`、`currentTime` 以及一秒后时钟是否前进以 `[pvzge-audio]` 警告写入日志。

音乐的 one-shot 和 `loadNative` 使用游戏自带的 WebAudio；其他 one-shot 保留 Cocos 原本的显式模式规则，游戏设置里的音效加载方式照常生效。
AudioContext 固定为 32 kHz：约 85% 的音效时长、48% 的音乐时长原本就不高于 32 kHz，
解码后的 PCM 比按 48 kHz 硬件采样率解码少三分之一。
WebAudio decode 失败会传给外层 Promise。one-shot 通过实际 `onended` 结算播放、释放源节点和 Gain，
不会用原始时长计时器猜测变速后的结束时间。播放器经过隔离 JS 执行验证，仍需设备验收实际播放。
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
