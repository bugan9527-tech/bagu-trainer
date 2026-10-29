# 桌面 UI 视觉规范（0.4.x Tk 兼容版，归档）

> 版本范围：本文仅适用于 0.4.x Tk 兼容版，保留 Tk 主题规范。0.5.0 Electron 的视觉规范以 `frontend/src/tokens.css`、`frontend/src/styles.css` 和[项目仓库 docs/ELECTRON_MIGRATION.md](https://github.com/bugan9527-tech/bagu-trainer/blob/main/docs/ELECTRON_MIGRATION.md) 为准。

适用范围：Windows 中文学习工具，长时间阅读与答题。采用浅色、扁平、低装饰界面；每组操作只突出一个主要动作。使用已安装的 UI UX Pro Max 辅助检索，采纳 Minimalism & Swiss Style 的留白、清晰层级、单一强调色和键盘焦点建议；不采用首次通用检索中偏营销页的轮播、拟物与英文字体组合。

## 公共主题

唯一实现入口为 `bagu_trainer/ui_theme.py`。窗口创建后、子控件创建前调用 `apply_theme(window)`，保留返回的主题对象。顶层窗口与内嵌面板复用所属 Tk 根的主题；向导退出后新建根窗口会重新创建主题图像与字体，模块导入不创建窗口。

| 语义 | 值 | 用法 |
| --- | --- | --- |
| 页面背景 | `#F5F7FA` | 窗口、框架、答题滚动画布 |
| 控件表面 | `#FFFFFF` | 输入框、列表、次要按钮 |
| 正文 | `#202B3C` | 题干、标签、状态正文 |
| 辅助文字 | `#526176` | 路径、说明、步骤提示 |
| 控件边界 | `#8795A8` | 输入和列表边界 |
| 主强调色 | `#2563EB` | 主要动作、焦点、进度条、单选状态 |
| 悬停 / 按下 | `#1D4ED8` / `#1E40AF` | 主要按钮交互状态 |
| 选中 / 提示底色 | `#E8EFFD` | 列表选择、状态区域 |
| 危险 | `#B42318` | 移除监听目录等破坏性动作 |
| 禁用底 / 字 | `#E4E9F0` / `#667085` | 与原有 disabled 状态同步 |

正常正文、辅助文字和主要/危险按钮文字对比度目标均 ≥ 4.5:1。提示保留完整文字，正确/错误和暂停状态不只靠颜色传达。成功和警告语义色预留给主题库，不作为日常强调色使用。

## 字体与尺寸

中文字体依次选择 Microsoft YaHei UI → Microsoft YaHei → Noto Sans SC → Noto Sans CJK SC → Microsoft JhengHei UI → SimHei → SimSun → Tk 系统默认字体。使用本机字体，不联网下载、不随包分发字体。实际显示适配及未覆盖项目见 [UI_DISPLAY.md](UI_DISPLAY.md)。

| 层级 | 字号（pt） | 样式 |
| --- | --- | --- |
| 窗口内容标题 | 18 | 粗体，`Title.TLabel` |
| 知识点 / 区块标题 | 14 | 粗体，`Section.TLabel` |
| 正文、表单、按钮 | 10 | 常规；Windows 96 DPI 下约 13.3 px |
| 次级标注 | 9 | 常规，`Caption.TLabel`；关键说明仍使用正文大小 |

布局使用 96 DPI 逻辑像素。间距阶梯为 **4 / 8 / 12 / 16 / 24 / 32**；窗口内边距 24，区块间距 16–24，表单和操作间距 8–16。通过 `theme.px()` 按 Tk 当前缩放转换；字体保留 pt，不重复缩放或强制修改系统 DPI 设置。

| 控件 | 规范 |
| --- | --- |
| 按钮 | 水平/垂直 padding 16/8，最小宽度 8 个文本单位，96 DPI 下常见高度约 36 px；随文字增高 |
| 输入框 / 下拉框 | padding 10/7，字号与正文一致，输入区白底，焦点蓝色 |
| 表格行 | 36 px，标题与正文同字号，垂直居中 |
| 滚动条 / 进度条 | 宽 14 px / 厚 6 px |
| 状态区域 | 浅蓝灰底，padding 12，正文色，长文字换行 |

`Primary.TButton` 用于继续答题、提交答案、保存、向导下一步、下载构建；`Secondary.TButton` 为普通动作默认样式，包括返回、关闭、测试、暂停和取消；`Danger.TButton` 只用于移除选中目录。三类均定义悬停、按下、禁用和可见键盘焦点；改变状态不改变布局尺寸。取消进行中的测试或构建不属于数据删除，不使用危险色。

## 四类窗口接入

窗口分区、可伸缩布局及长文本行为详见 [UI_LAYOUT.md](UI_LAYOUT.md)。

| 窗口 | 接入内容 |
| --- | --- |
| `DesktopApp`（包括答题弹窗） | 标题/说明/状态层级、主要动作、单选行与长题滚动、统一画布背景 |
| `FirstUseWizard` | 步骤、表单、列表、底部导航；内容区可收缩，底部按钮持续可见 |
| `BankSetupWindow`（独立 / 内嵌） | 来源表格、状态、进度、构建动作；表格优先收缩 |
| `ModelSettingsWindow`（独立 / 子窗口） | 标题、说明、输入、状态和保存动作；窗口可调整大小 |

保留原有命令、线程/队列、校验、凭据、作答和关闭逻辑，以及 A–D、Ctrl+Enter、Ctrl+R 快捷键。系统目录选择和消息对话框继续使用 Windows 原生外观。无装饰动画；进度反馈沿用现有进度条。

## 依赖与打包决策

引入并固定 `ttkbootstrap==1.20.4`，沿用现有 Tk/ttk 控件，不迁移业务控制器。使用其 1.x Style/ThemeDefinition API 和本项目颜色定义，避免静默升级到 2.x。Pillow 约束为 `>=10,<13`，与该版本上游运行依赖一致。代价是增加 Pillow 的图像模块和二进制体积；主题图像、复选/单选等视觉状态由它绘制。

两个 PyInstaller spec 均收集 ttkbootstrap 数据与递归 metadata；Pillow 图像模块和二进制由 PyInstaller 标准 hooks 分析。构建清单记录实际依赖版本。`--check-runtime` 和 `--check-package` 会实际初始化公共主题，后者输出主题名、字体和依赖版本。完整许可见 `THIRD_PARTY_NOTICES.md` 与生成包内的 `licenses/`。
