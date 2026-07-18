# AgentAtlas · 星图

**你所有 AI 协作项目的全景地图。** 自动发现本机上所有 Claude Code / Codex 协作过的项目，
按"对话"粒度无损续接——包括跨工具接力（Codex 干了一半，让 Claude 原地接着干）。

用 AI 编程工具的人都会遇到：项目越来越多、散落各处，记不住哪个文件夹是 AI 做的、做到哪了；
Claude 和 Codex 互不知道对方干了什么。AgentAtlas 解决这两件事。

## 功能

**🗺 自动发现（零登记，永不漏）**
- 扫描 `~/.claude/projects` 与 `~/.codex/sessions`（含归档会话），罗列所有 AI 协作过的文件夹
- 磁盘标记扫描兜底：日志被清理的老项目，只要文件夹里有 `.claude` / `.codex` / `.ai-context` /
  `CLAUDE.md` / `AGENTS.md`，照样能找到
- 每个项目显示：参与的 AI、会话次数、最后活动时间

**💬 按对话续接（核心功能，全程不做有损摘要）**
- 点开项目 → 看到每条对话（时间 / 工具 / 发言数 / 第一句话）
- **▶ 续接**：同工具原生恢复（`claude --resume` / `codex resume`），上下文完整无损
- **⇄ 接力**：跨工具！把该对话**完整原文**写入项目的 `.ai-context/CONTINUE-*.md`
  （只滤除工具调用报文，内容一字不动），然后启动对方 AI，要求它先读全文、
  复述当前进度、经你确认后接着干

**🗂 虚拟整理（绝不动你的文件）**
- 会话门槛：一次性杂活太多？设 ≥2 次会话自动折叠
- 标签、归档：只改看板显示，文件夹一个字节都不动

**🧠 双层记忆库（图书馆 + 逻辑体）**
- **📚 保全同步**：把全部对话增量导出为完整文字稿档案（`~/.agent-atlas/memory/`），
  无 LLM、零损耗——你的真实记忆永久保全，随时可查可 grep
- **🧠 蒸馏**：调用本机 `claude` CLI（无头模式，模型可选 Haiku/Sonnet/Opus），
  把文字稿提炼为「用户偏好 / 关键决策 / 可复用知识 / 教训」，生成每个项目的 `MEMORY.md`，
  并自动投放到项目的 `.ai-context/`（若存在）——让 AI 越来越懂你。蒸馏永不修改原始档案

## 使用

**Windows**：下载 [Releases](../../releases) 中的 `AgentAtlas.exe`，双击运行，浏览器自动打开。

**任意平台（需 Python 3.8+，零依赖）**：

```bash
python app.py          # 启动 Web 看板
python app.py --scan   # 命令行纯文本输出清单
```

首次使用建议在页面顶部"扫描根目录"里填上你的项目常在的目录（如 `D:\projects`），
这样日志已丢失的老项目也能被磁盘标记找到。

## 典型工作流：Codex → Claude 无损交接

1. Codex 里干到一半，等它说完当前回合
2. 打开 AgentAtlas → 🔄 重新扫描 → 点进该项目
3. 找到那条对话，点 **⇄ 接力给 claude**
4. 弹出的终端里 Claude 自动读完整对话记录，复述"当前进度 + 下一步"
5. 核对无误，回一句"对，继续"——完成交接

## 隐私与安全

- 完全本地运行，只监听 `127.0.0.1`，无任何网络上传
- 对会话日志**只读**；接力时只在项目的 `.ai-context/` 里**新建**文件，不改任何已有文件
- 平台自身状态（标签 / 归档 / 设置）存于 `~/.agent-atlas/state.json`

## 自行打包 exe

```bash
pip install pyinstaller
pyinstaller --onefile --name AgentAtlas app.py
```

> 提示：PyInstaller 打包的无签名 exe 可能被杀毒软件误报，介意的话直接 `python app.py` 运行源码。

## License

MIT
