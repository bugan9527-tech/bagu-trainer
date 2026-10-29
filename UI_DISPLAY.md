# 桌面显示与键盘适配（0.4.x Tk 兼容版，归档）

> 版本范围：本文仅适用于 0.4.x Tk 兼容版，保留 Tk 显示/缩放/键盘适配说明。0.5.0 Electron 的响应式布局和快捷键说明见[项目仓库 docs/ELECTRON_DESKTOP.md](https://github.com/bugan9527-tech/bagu-trainer/blob/main/docs/ELECTRON_DESKTOP.md)。

2026-10-22。布局见 [UI_LAYOUT.md](UI_LAYOUT.md)，任务状态和退出边界见 [UI_STATES.md](UI_STATES.md)。

## 显示规则

- 主窗口默认 1200×800 逻辑像素，尺寸、最小值和恢复位置均约束到显示器工作区；保留 Windows 标题栏及 `window.json` 的尺寸/位置/最大化状态。
- 展开侧栏按空间取 200/144 逻辑像素；折叠为 80 像素图标栏，为高 DPI 的滚动条及完整图标留出空间，可悬停或聚焦查看文字并重新展开。图标以当前缩放生成并缓存，不依赖外部 SVG 引擎。
- 中文字体按 Microsoft YaHei UI、Microsoft YaHei、Noto Sans SC、Noto Sans CJK SC、Microsoft JhengHei UI、SimHei、SimSun 和 Tk 默认字体回退。
- 中英混排、长题干、选项与解析按实际宽度换行；长地址和模型输入使用原生单行编辑。路径/表格允许横向滚动，正文允许滚动至末尾，提交/保存等关键动作保持可达。
- 不用未映射时的 1 像素宽度重排长文本；调整布局时保留已明确滚至末尾的阅读意图。
- 练习中心有待答题时，宽屏列表与答题区按 1:2 分配宽度，并分别保留 260/360 逻辑像素的最小宽度。
  同一 uniform 分组两列均须使用正权重；切回上下布局时清除横向最小宽度，避免遗留占位。

## 键盘规则

- Tab/Shift+Tab 按控件顺序移动，滚动表单先显示目标再聚焦；只读状态文本可选中、复制并正常离开。
- 侧栏上/下、Home/End 移动焦点，Enter 激活页面。焦点与选中态分开，图标栏聚焦时有文字提示。
- Ctrl+R 进入并恢复练习；焦点在 Entry、Combobox、Text、Spinbox 等输入控件时保留原生编辑按键。
- A–D 和 Ctrl+Enter 只在可见练习页的阅读区、选项或提交控件焦点上生效；Ctrl/Alt 组合不作为普通答案字母。键盘事件经 Tk 顶层 bindtag 接收，并核对实际焦点，不能仅在父 Frame 上绑定后宣称子控件可用。
- Esc 在历史详情返回列表；不退出应用、不取消正在写入的提交。提交按钮与快捷键共用题号幂等保护。
- 阅读区支持上下、Page Up/Down、Home/End；输入框保留原生 Home/End、文本选择与 IME 行为。

## 可复现检查与数据来源

`python scripts/check_ui_redesign.py --output output/ui-redesign-stage6/native` 生成七页面、宽窄练习、历史及折叠栏的实际 Tk 截图。数据是临时 SQLite 中的 35 条自编作答和明确注入的合成待答显示样例，监听标签为演示，不启动真实监听或调用模型。

`python scripts/check_ui_display.py --output output/ui-redesign-stage6/display` 检查长内容、最小/最大窗口、字体回退、状态焦点和固定操作。两种工具只使用隔离配置；截图和回调异常分别进入 JSON 报告，不自动把截图成功当成视觉验收。

`--tk-scale 1.25/1.5/2` 只改变 Tk 点换算，属于模拟。自动化覆盖四档缩放，不能替代 Windows 原生比例验收。

`release_tests/test_practice_layout.py` 专门覆盖“待答列表非空 → 进入本题 → 四个选项可见并可选择/提交”，
并检查宽屏、上下排列、紧凑列表切换及待答清空/恢复，避免只检查控制器切题成功而漏掉可见区域。
显示脚本新增 `quiz-pending-split.png`，使用合成题目检查这一分栏场景，不写真实作答记录。

## 覆盖矩阵

| 环境 | 覆盖方式 |
| --- | --- |
| 本机 Windows 原生 100%，1920×1080、工作区 1920×1040、DPI 96 | 实际源码窗口、冻结验收驱动页面及生产 EXE 诊断/截图 |
| 125% / 150% / 200% Tk 缩放 | 隔离自动回归及可复现截图模拟 |
| Windows 原生 125% / 150% / 200% | 未覆盖，不能以 Tk 模拟代替 |
| 跨显示器、运行中更改 DPI、触控、缺少全部中文候选字体的系统 | 未覆盖；字体回退模拟不代表该环境 |
| 无 Python/Git 的干净 Windows / Sandbox、真实远程服务商与用户凭据写入 | 未覆盖；本机冻结验收清空 PATH，使用 localhost 合成服务与内存凭据 |

冻结验收程序使用真实主壳代码检查七页、导航图标、历史与重练，并与生产包的源码指纹核对；生产 EXE 单独验证主题、图标、首次/老用户启动、后台及单实例唤起、只读程序目录和命令入口。具体通过/失败项、报告、截图和包校验值见源码工作区 `UI_REDESIGN_PROGRESS.md` 与 `validation/stage6/REPORT.md`，未覆盖项不得写成已验收。
