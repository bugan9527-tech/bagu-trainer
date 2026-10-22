# 第三方桌面 UI 组件

项目自身许可保持 [LICENSE](LICENSE)。新增主题运行组件：

| 组件 | 约束 | 许可和用途 |
| --- | --- | --- |
| ttkbootstrap | `1.20.4` | 安装 wheel 的 `licenses/LICENSE` 为 MIT，作者 Israel Dryer；提供 Tk/ttk 主题和控件状态。上游包 metadata 标注 `MIT AND (Apache-2.0 OR BSD-2-Clause)`，构建时原样保留 metadata 和完整许可文件。 |
| Pillow | `>=10,<13` | 核心为 MIT-CMU；wheel 的完整 LICENSE 另含随附第三方图像库声明。用于主题控件图像，构建时保留该文件全文。 |

版本与上游说明：[ttkbootstrap 1.20.4](https://pypi.org/project/ttkbootstrap/1.20.4/)、[Pillow](https://pillow.readthedocs.io/en/stable/about.html)。以发布包中实际安装版本附带的许可文本为准。

`scripts/build_portable.py` 的 `prepare_notices()` 从独立构建环境复制各 distribution 的 LICENSE / COPYING / NOTICE 文件到 `licenses/<distribution>/`，并生成 `THIRD_PARTY_NOTICES.txt`。ttkbootstrap 与 Pillow 的许可缺失时构建失败。生产包与独立冻结验收包均收录这些完整文本。

Tcl/Tk、Python 和 keyring 的既有收集规则继续生效。中文字体使用系统已安装字体，不包含商业字体文件。题库来源许可仍由来源清单和下载时的声明独立管理。

每次重新构建都会从构建环境重生成 `THIRD_PARTY_NOTICES.txt` 和 `licenses/`；实际版本同时写入 `BUILD_MANIFEST.json`。`--check-package REPORT` 会实例化主题按钮、单选、下拉、表格等控件并记录主题图像、四级字体及 Tcl/Tk 资源位置，供冻结包回归核对。诊断成功不代替窗口截图验收。
