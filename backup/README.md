# Pulse 画布设计备份

导出时间: 2026-10-14

| 文件 | 内容 |
|---|---|
| `pulse-canvas-frame.html` | 画布里 "CPU Monitor" frame 的完整 HTML (设计源文件, 48072 字符) |
| `pulse-canvas-2x.png` | 该 frame 的 2x 截图 (2104x1604) |
| `canvas-meta.json` | 画布 / frame / 评论的元数据 (id、尺寸、位置、更新时间) |
| `diff-canvas-vs-local.txt` | 画布 HTML 与本地 `pulse.html` 的逐段差异 (两边各自独有什么) |

## 恢复

1. 在 doop 里新建画布和 frame (或 MCP `create_frame`)。
2. 把 `pulse-canvas-frame.html` 的内容作为 frame 的 HTML 写入 (MCP `set_frame_html`)。
3. 尺寸设为 1052 x 802。

## 本次导出 (2026-10-14) 相对上一版 (2026-10-13) 的新增

**Frame 是同一个 (cqRmTuFCPK), 在原来那份上继续改的** —— 上一版 42693 字符, 这一版 48072。
Diff 存在 `_diff_old_vs_canvas.txt`。新增的是:

- **CPU 线程使用率竖条** (`.thr` + 44 根 `.thb div`, CPU 卡里"综合"和折线图之间)
- **CPU 功耗行** (`.pw`, 闪电图标 + 数值 + 细条, 页脚 `/ 253 W`)
- **每张 GPU 卡的功耗行** (同一个 `.pw` 组件, `/ 450 W`)
- **GPU 汇总里的"总功率"** (各个卡相加)
- **磁盘卡右上角容量条** (`.dcap`, 「容量 1.43 / 2 TB」+ 占用细条, 悬停有详情)
- **GPU 卡头部徽标** (`.gpuchip`, 显卡图标 + "GPU" + 序号 —— 只有前两张卡带)
- **标题栏改成** `<程序名> <版本> · 作者 <邮箱> — <机型> · 每秒采样 · 模拟数据` (画布里是示例机型)
- CPU 卡统计四格从 2×2 改成一行四列 (`.stats.s4`)

## 说明

- 这是单个 frame 的备份, 不含画布里的 Memory。要完整备份请先停掉 doop, 再复制 `C:\frank\develop\tools\doop\data` 和 `.env` (`.env` 含密钥, 注意保管)。
- **画布版比本地 `pulse.html` 多一层 `html,body{width:1049px;height:802px} body{padding:40px}` 的开头规则, 但末尾又被 `html,body{width:100%;height:100%} body{padding:14px}` 覆盖掉了** —— 后半段和本地一致。所以两边的实际布局是同一个 (padding 14px), 前面那两条是画布上历史遗留的死规则。**别用本备份直接覆盖 `pulse.html`**。
- 本地独有、画布里没有的: `#live` 模式 (地址带 `#live` 就不跑模拟数据)、`hasApi()` / `window.pywebview.api.top()` 前10进程弹窗的实时取数、`pushSample` 的外部推数入口、空状态块 (`.empty` / `.chart.nodata`)。这些是给 Python 程序用的, 画布是设计稿不需要。相反方向 (画布有本地没有) 的差异见下条。
- 本地有 `pushSample(... threads, disk)` 两处签名注释提到 8 个参数, 画布版只推到 `power` 为止 —— **本地是超集, 画布是它的设计稿**。要对比时看 `_diff_canvas_vs_local.txt`。
