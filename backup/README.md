# Pulse 画布设计备份

导出时间: 2026-10-13

| 文件 | 内容 |
|---|---|
| `pulse-canvas-frame.html` | 画布里 "CPU Monitor" frame 的完整 HTML (设计源文件, 42692 字符) |
| `pulse-canvas-2x.png` | 该 frame 的 2x 截图 (2104x1604) |
| `canvas-meta.json` | 画布 / frame 的 id、尺寸、位置、更新时间 |

## 恢复

1. 在 doop 里新建画布和 frame (或 MCP `create_frame`)。
2. 把 `pulse-canvas-frame.html` 的内容作为 frame 的 HTML 写入 (MCP `set_frame_html`)。
3. 尺寸设为 1052 x 802。

## 说明

- 这是单个 frame 的备份, 不含画布里的评论、Memory 等。要完整备份请先停掉 doop, 再复制 `C:\frank\develop\tools\doop\data` 和 `.env` (`.env` 含密钥, 注意保管)。
- 画布版本的 CSS 末尾是 `html,body{width:100%;height:100%} body{padding:14px}`, 与本地 `pulse.html` (底部留白 60px、高 822) 不同, 这是有意的本地差异, 不要直接用本备份覆盖 `pulse.html`。
