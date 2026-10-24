# 0.3.1 本地可复现验证流程

本流程只生成独立便携验证包，不调用 Release，不替换安装，不执行生产安装器。
旧 `dist/verification-0.3.1` 和旧报告是历史证据，不代表当前提交。

## 环境与依赖

使用 Windows x64、CPython 3.10.11（完整 Tcl/Tk 8.6），构建环境与测试环境分开。
`requirements-build-lock.txt` 和 `requirements-test-lock.txt` 固定本轮全部直接及传递依赖。
`.gitattributes` 固定源码换行，避免不同 Git autocrlf 设置改变逐文件源码指纹。

```powershell
python -m venv build/verification-build-env
build/verification-build-env/Scripts/python.exe -m pip install -r requirements-build-lock.txt
python -m venv build/verification-test-env
build/verification-test-env/Scripts/python.exe -m pip install -r requirements-test-lock.txt
```

先执行最小环境探针：

```powershell
build/verification-test-env/Scripts/python.exe -c "import tkinter; r=tkinter.Tk(); print(r.tk.call('info','library')); r.destroy()"
```

本机受限沙箱下该标准库探针报 `Can't find a usable init.tcl`，沙箱外同解释器成功。
因此原生 Tk、命名管道及 EXE 验收在允许原生桌面访问的进程执行；不修改 Tcl 搜索判定、
不删断言、不跳过失败、不延长原有启动截止时间。构建同样需要可用的 Tk 探测。

## 固定提交后构建与验收

先完成诊断回归与必要修复，再提交相关源码、依赖、新模块和测试。
从该提交克隆到项目 `build/` 下的新目录，确保 `git status --porcelain` 为空。
构建和最终回归均以该干净目录为 cwd；构建/测试解释器可以位于外层项目的独立虚拟环境。
下面所有路径替换为该次独立目录的绝对路径，不能复用旧验收 work 目录。

```powershell
$env:BAGU_USER_DATA_DIR = 'C:\绝对路径\validation\本轮\build-user'
$env:PYTHON_KEYRING_BACKEND = 'keyring.backends.null.Keyring'
$env:BAGU_LLM_API_KEY = 'local-verification-fixture'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
& $buildPython -B scripts/build_portable.py --require-clean --output-dir $newOutput
& $buildPython -B scripts/build_acceptance.py --require-clean --output-dir $newOutput
$env:BAGU_PORTABLE_BUNDLE = Join-Path $newOutput 'BaguTrainer'
$env:BAGU_PORTABLE_ARCHIVE = Join-Path $newOutput 'BaguTrainer-0.3.1-windows-x64.zip'
& $testPython -u -B scripts/run_release_tests.py --output $releaseXml
& $testPython -B -m pytest tests -q -W error --junitxml=$unitXml
& "$newOutput\BaguTrainerAcceptance\BaguTrainerAcceptance.exe" --work $newWork --memory-credentials --production-exe "$newOutput\BaguTrainer\BaguTrainer.exe"
& $buildPython -B scripts/verify_local_bundle.py --output-dir $newOutput --report $integrityJson
```

`run_release_tests.py` 保留现有逐模块新进程和 shell 逐用例隔离，不自动重试；任何失败或跳过均返回非零。
单元回归的历史语料需要显式外部只读 `BAGU_TEST_BANK_ROOT`；没有合法基准时如实保留跳过，
不能用合成题库冒充历史匹配评估。

核验脚本要求干净源码，将生产 manifest、冻结驱动 provenance 与当前提交及完整源码指纹比较，
核对 ZIP 摘要、CRC、全部成员、大小、SHA-256，及两个 EXE 的 PYZ 中全部应用模块。
每次最终报告附原始 JUnit、冻结 ACCEPTANCE/ERROR、完整文件核验与证据摘要清单。
报告在最终提交之后生成，置于忽略的 `validation/` 和 `dist/`，用提交与源码指纹绑定，避免报告自引用。

这里的可复现指从同一干净提交与固定工具链重复执行构建及验收，并核对来源和载荷。
PyInstaller/ZIP 包含时间等信息，不承诺不同构建的 ZIP 逐字节相同；每轮均计算自己的 SHA-256。
最终报告须区分本地模拟模型、真实模型、实际生产 EXE、冻结业务驱动和未覆盖项。
