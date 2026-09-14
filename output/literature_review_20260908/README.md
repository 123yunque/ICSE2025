# 文献报告归档

本目录保存 2026-09-08 文献调研的已有成品与辅助脚本。本次归档不修改报告正文，
也不代表重新核验报告中的论文或实验结论。

## 保留在版本库中的内容

- `代码大模型文献读后报告_2025-2026.md`：报告源文档。
- 同名 `.docx`、`.pdf`：已生成的可编辑版与阅读版。
- `build_report.py`：将 Markdown 源文档生成为 Word。
- `render_with_word.ps1`：通过本地 Microsoft Word 更新文档并导出 PDF。
- `fetch_sources.py`：下载脚本中列出的来源 PDF 并提取文本。
- `read_evidence.py`：从已提取的文本中查看指定论文的片段。
- `check_layout.py`：检查 PDF 文本并为已有页面图片生成预览拼图。
- `source_manifest.json`：已有下载运行的来源链接和状态记录，含失败项；不是所有来源均成功获取的声明。

## 本地辅助文件

`sources/` 中的下载论文和提取文本、`qa/` 与 `qa_final/` 中的页面图片及检查结果，
以及 `report_check.json` 均保留在本地，由仓库 `.gitignore` 排除。
这些文件不随 Git 克隆分发。

## 复用脚本

Python 脚本按用途使用 `python-docx`、`pypdf` 和 Pillow；Word 导出脚本要求
Windows 上安装 Microsoft Word。可在本目录按需执行：

```powershell
python build_report.py
powershell -File render_with_word.ps1
```

以上命令会重新生成同名成品。本次整理没有运行它们，也没有重新下载论文。

`fetch_sources.py` 需要网络，运行时会更新来源清单；`read_evidence.py` 依赖
`sources/` 中的文本。`check_layout.py` 要求 `qa_final/` 已存在；拼图还依赖该目录中
预先渲染的 `page-*.png`，脚本本身不负责将 PDF 渲染为页面图片。
