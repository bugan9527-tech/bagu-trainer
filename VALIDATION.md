# 验证范围与复现

## 自动化

发布检查覆盖首次引导、七页导航、后台监听、AI 生成及独立关联审核、内嵌答题、提交幂等、历史快照与重练、配置保存、暂停练习、单实例、退出等待及便携 EXE。

所有运行配置、数据库、项目文件、模型响应和凭据均在隔离环境中生成。发布前必须检查源码和安装包不包含个人配置、访问 Key、学习记录、进程认证或第三方完整题库。

```powershell
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pytest release_tests -q -W error
python -m pytest tests -q -W error
```

普通单元测试自行创建临时合成题库，不读取项目旁或已安装客户端的用户数据。匹配器的历史语料基准单独依赖外部只读数据：设置 BAGU_TEST_BANK_ROOT 为包含 data/questions.json、data/question_concepts.json、data/concept_profile_report.json 的目录可执行全部历史断言；未提供时仅显式跳过这些语料基准，不跳过产品发布门禁。

便携 EXE 用例需要先构建，再设置 BAGU_PORTABLE_BUNDLE 与 BAGU_PORTABLE_ARCHIVE 指向本次产物，避免因缺包跳过。发布脚本拒绝失败或跳过的 release_tests 报告、脏源码构建、提交不匹配的产物和未完成的冻结验收。

显示测试包含长中文、路径、选项、解析、键盘和四档 Tk scaling。键盘用例在 Windows 最大化/恢复后的原生焦点稳定到目标控件后才发送事件，仍严格检查滚动末尾和焦点顺序。

## 冻结与发布

生产程序和独立验收驱动应从同一个干净提交构建。冻结流程使用 localhost 题库/模型、内存凭据及临时学习库，验证首次使用、审核拒绝与重试、提交、重练、重启和合成升级保留记录。

GitHub Release 附带本次 ZIP、SHA-256、无失败或跳过的发布测试报告及脱敏冻结摘要。原始本机路径、机器名、内部运行认证及测试 profile 不上传。源码身份以完整提交 SHA 和相对路径文件摘要核对。

## 范围限制

- Tk scaling 模拟不等同于 Windows 原生 125%/150%/200%，也不涵盖多显示器或运行中换 DPI。
- 清空 PATH 不等于无 Python/Git 的干净 Windows；应使用 [干净系统流程](CLEAN_WINDOWS_ACCEPTANCE.md)。
- localhost 模拟链路不能代表真实服务商连通性、计费或真实系统凭据写入。
- 程序没有代码签名；正式公开发布需由维护者审阅草稿及目标环境结果。
