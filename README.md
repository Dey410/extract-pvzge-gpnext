# PvZGE Lite 资源提取

在 GitHub Actions 的 **Extract PvZGE Lite Tauri Assets** 中手动运行，
`release_tag` 留空时下载最新非草稿 Release（包括 Pre-release）的非 macOS Lite ZIP。

## 可选 ASTC 转换

将 `image_transcode_format` 设为 `astc` 可转换 Cocos `assets/*/native/` 下的
PNG/AVIF 纹理。默认 `none` 保持原资源。图片按实际内容识别，支持扩展名仍为
`.png` 的 AVIF；先解码为 RGBA PNG，再转 ASTC，保留尺寸和透明通道。
不改变现有混合音频加载设置，也不转码音频。

参数参考 `codex/compact-150-audio-policy`（`c8e73d9`）：

| 条件（按表中顺序判断） | ASTC 块大小 |
| --- | --- |
| 指定的全屏旋涡图集 | 4×4 |
| 最大边 ≤ 512，或 PNG 压缩位率 ≥ 6 bits/pixel | 4×4 |
| 最大边 ≥ 2048，且最大可见连通区域占比 ≥ 0.35 | 6×6 |
| 其他 | 8×8 |

编码器固定为 `astcenc 5.6.0`，`-cl -fast`，每个编码进程 1 线程；
并行数默认为 CPU 核数。透明度阈值 64/255，分析缩略图最大边 128。
AVIF 的细节判定使用解码后 RGBA PNG 的大小，不能使用 AVIF 压缩大小，
因此与原始未压缩版本的分类结果不一定完全一致。

转换前校验独立和打包 ImageAsset 的映射；全部 ASTC 编码成功、头部尺寸及
数据长度校验通过后，更新 JSON 的 `fmt` 为对应 ASTC 格式。
Lite 原包可能已为部分图片附带 ASTC（例如 0.14.0 的 `1aae2f737`）。
已有 ASTC 通过格式、尺寸和数据长度校验后直接复用，元数据使用该文件实际的
块大小；缺少 ASTC 的图片才按上表参数生成。损坏或尺寸不匹配的文件会报错，
不会覆盖。报告会分别列出复用数量和新编码数量。
原图保留在原路径，原元数据备份在 `reports/astc-original-metadata-*/`，
可按备份的 `assets/` 相对路径复制回 `docs/` 恢复原图引用。
转换报告为 `reports/astc-texture-summary.txt`，逐图参数为
`reports/astc-texture-map.csv`，均包含在下载的 Artifact 中。

注意：原图只是保留备份，元数据只选择 ASTC，设备/WebGL 必须支持 ASTC。
ASTC 会再次有损压缩，不能恢复 AVIF 已丢失的细节，文件也可能比 AVIF 大；
由于保留原图，Artifact 总体积会增加。此选项仅处理 Cocos native 纹理，
不会把网页图标、HTML 启动画面等非 native 图片转为 GPU 纹理，
不能据此保证整个包已完全适配鸿蒙。

## 本地使用与验证

使用 Python 3.12+，安装 [ASTC 编码器](https://github.com/ARM-software/astc-encoder/releases/tag/5.6.0)，
将 `astcenc` 放入 PATH（或设置 `ASTCENC`）。
[Pillow 官方说明](https://pillow.readthedocs.io/en/stable/releasenotes/11.3.0.html#avif-support-in-wheels)
确认其 wheel 从 11.3.0 起内置 AVIF 支持；本仓库固定使用 12.1.1。

```bash
python3 -m pip install -r scripts/requirements-astc.txt
python3 -m unittest discover -s tests -v
python3 scripts/transcode_textures_astc.py docs reports/astc-texture-summary.txt
```

`--help` 可查看质量及分类阈值选项。重复运行会复用已验证的 `.astc`；
若想改变编码参数，请使用一份新提取的资源。中间 PNG 留在日志列出的临时目录中，不执行删除。
