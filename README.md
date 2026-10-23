# PvZGE Lite 资源提取

在 GitHub Actions 的 **Extract PvZGE Lite Tauri Assets** 手动运行一次。`release_tag` 是
Gzh0821/pvzg_site 的 Release 标签，例如 `0.15.0`，留空时使用最新非草稿 Release（包含 Pre-release），
取其中非 macOS 的 Lite ZIP。运行标题显示为 `0.15.0 · 测试` 或 `0.15.0 · 发布`，留空时显示
`最新 · 测试`，实际包名写在运行摘要里。`publish` 默认关闭，勾选后把本次产物发布为本仓库 Release。
Actions 固定执行核心测试、Cocos 音频修补、ASTC 转换、全新目录组装和 ZIP 重开验证。
任何未知引擎、补丁布局、纹理映射或封存验证失败都不会上传正式资源 ZIP。

产物以 `<游戏版本>+<packageId 前 8 位>` 命名，例如 `GardendlessResources-0.15.0+916b6a83.zip`，
另有独立的 `reports/`。本仓库已有 Release 时，以创建时间最新的 Release 为基础，同时生成
`GardendlessResources-<本次>-from-<基础>.zip` 增量包。两者分别作为保留 7 天的 Actions 产物上传
（reports 随完整包），增量包可以单独下载；勾选 `publish` 后另外创建同名 Release，它成为下一次的基础。相同 packageId 已发布时直接失败，
旧 Release 不自动删除。完整包 ZIP 内只有 `docs/` 运行资源：
实际选择的 ASTC（PNG/AVIF/WebP 共用一条转换路径）、游戏代码、音频和必要的网页图片。native 原图、解码中间 PNG、
元数据备份及已被 ASTC 替代的 PVR/PKM 保留在构建源目录或 reports 中，不进入正式资源 ZIP，不用删除源文件来减包。
每次构建使用全新的 run 输出目录。

音频全部走 WebAudio，不再用 `<audio>`。0.16.0 起 Cocos 不再播放音频（资源里没有 `cc.AudioClip`），
游戏通过 `TauriAudioAPI` 调用入口模块创建的 `window.audioEngine`，上游实现是 60 个 `<audio>` 元素的池子，
音频文件放在 `audio/`（`audio/manifest.json` 把名字映射到 mp3）。`<audio>` 在移动端 WebView 有两个问题：
- BGM 丢失：游戏每帧用 `getPosition()` 判断当前音乐是否到了 `musicLength` 再切下一段。元素加载或播放失败时
  池子直接回收这个槽位，位置从此是 0，整关不再有 BGM。
- iPhone 上 WebKit 锁定媒体元素音量，游戏的音乐、音效音量和淡出都不起作用。

`scripts/patch_audio_runtime.py` 把入口模块里构造池子的那一处换成 `scripts/audio_engine.js` 的
WebAudio 引擎，方法与池子相同，也保留 60 个声音的上限：
- 第一次播放时取文件并解码，同一文件按 URL 共享。释放后按解码字节放入 32 MiB 的 LRU，超出时淘汰最早释放的；
  单个超过上限的不放入，不会把其他缓存挤掉。
- 播放时钟从请求播放时开始按 `AudioContext.currentTime × speed` 推进，解码完成后从当前位置接入。
  首次播放会损失解码耗时那一小段开头，但切歌时机不变；解码失败只记错误，时钟照常走，游戏仍会按时切到下一段。
- 音量和淡出用 GainNode；`setRestartBeforeEnd` 用源节点的 `loopEnd` 在池子同样的位置循环，不靠计时器。
- 播放和任意点击、按键都会在上下文未运行时调用 `resume()`。挂起与恢复的时机由 iOS 宿主
  （GardendlessLoader 的 `audio_lifecycle.js`）按场景活跃状态处理，资源包里不做挂起/恢复或音频会话类型的绕行。

AudioContext 固定为 32 kHz：0.16.0 约 82% 的音效时长、50% 的音乐时长原本就不高于 32 kHz，
解码后的 PCM 比按 48 kHz 硬件采样率解码少三分之一。补丁只接受已核对的布局：Cocos 3.8.4、
`audio/manifest.json` 的每项都指向 `audio/` 内的文件、Cocos 没有 `cc.AudioClip`、`TauriAudioAPI`
的命令恰好是引擎实现的那 12 个、只有一个模块以已知写法创建 `window.audioEngine`，替换后用 Node 检查语法。
任何一项不符就失败，不生成资源包。引擎经过隔离 JS 执行验证，仍需设备验收实际播放。

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

增量包只放哈希不在基础包索引里的文件，另带与完整包逐字节相同的 manifest、file-index，以及
`docs/__gardendless/package/delta.json`（`{"schemaVersion":1,"basePackageId":"sha256:…"}`）。
加载器确认当前资源的 packageId 等于 basePackageId 后，按 SHA256 从当前资源复制其余文件，
删除 delta.json 再完整校验，结果与完整包相同。封存后先完整验证基础包和新完整包，再逐项确认
增量包内文件与索引一致、缺省文件的哈希都在基础包中，且没有未索引文件。增量包需要 0.9.0 之后
支持增量导入的 GardendlessLoader；当前资源不是基础包时只能导入完整包。

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
