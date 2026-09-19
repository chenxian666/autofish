# 打包成 AutoFish.exe（无控制台黑窗口）

用 `.bat` 启动每次都会弹一个命令行黑窗口，因为它是控制台程序。
打包成 exe 之后双击直接出界面，**不再有任何黑窗口**。

---

## 一、最终产物

| 项 | 值 |
| --- | --- |
| **可执行文件** | `AutoFish.exe` |
| 大小 | 约 25 MB（单文件） |
| 架构 | x64 / PE32+ |
| 子系统 | `WINDOWS_GUI` —— 见下面「为什么不会再弹黑窗口」 |
| 清单 | 内嵌 `requireAdministrator` —— 双击自动请求管理员权限 |

**双击就能用，和以前双击 `autofish.bat` 的体验一样，只是没有黑窗口了。**
第一次启动会慢一点（单文件包要先把自己解到临时目录），实测**约 2.6 ~ 4.2 秒**才出界面；
第二次起因为文件已被系统缓存，通常快一些。

运行期数据位置**完全没变**，还是 `%LOCALAPPDATA%\AutoFish\`：
配置、模板、`hits\` 录音、`hits.csv`、`autofish.log`。
所以 exe 和 `.bat` 两种方式**共用同一份配置和标定结果**，随便换着用。

---

## 二、依赖

打包用的是**已经有的那个 GUI 虚拟环境**，不需要新建环境：

| 依赖 | 版本 | 说明 |
| --- | --- | --- |
| Python | 3.14.5 | `envs\gui`，这个带 Tkinter（托管的 3.13 没有） |
| PyInstaller | 6.22.3 | 本次新装，只在打包时用，不影响运行 |
| numpy | 已装 | 检测算法 |
| soundcard | 已装 | WASAPI 回环采集 |
| tkinter | 标准库 | 图形界面 |

装 PyInstaller（如果换了机器或删了环境）：

```
python -m pip install pyinstaller
```

---

## 三、怎么重新打包

**最省事：双击 `build_exe.bat`**，等它跑完，产物直接落在同目录。

> ### 如果它报 `error: the following arguments are required: scriptname`
>
> 那是**命令行引号的问题**，不是 PyInstaller 的问题。原因：
> `%~dp0` 结尾自带反斜杠，早期写成 `--distpath "%HERE%"` 会展开成
> `"...\钓鱼\"`，而 Windows 规则里 **`\"` 是转义引号** —— 引号不闭合，
> 这个参数就一直吃到行尾，把后面所有参数（包括脚本名）全吞了。
> **现已修好**（改成 `set "DIST=%HERE%."` 再用 `"%DIST%"`）。
> 如果你自己改过这个文件又碰到同样的错，检查有没有哪个引号里的路径以反斜杠结尾。

想自己敲命令的话，等价的完整命令是（在项目目录下执行）：

```
python -m PyInstaller ^
  --noconfirm --clean ^
  --onefile --windowed ^
  --name AutoFish ^
  --distpath . ^
  --workpath .pyi-build ^
  --specpath .pyi-build ^
  --hidden-import soundcard.mediafoundation ^
  --uac-admin ^
  autofish.py
```

耗时约 40 秒。**改过 `autofish.py` 之后必须重新打包** —— exe 里装的是打包那一刻的代码副本，
改 `.py` 不会影响已经生成的 exe。

### 每个参数在干什么

| 参数 | 作用 |
| --- | --- |
| `--windowed` | **关键**。把 PE 子系统标记成 GUI 程序，Windows 永远不会给它分配控制台 —— 黑窗口就是这么消失的（等价写法 `--noconsole`） |
| `--uac-admin` | 内嵌"需要管理员权限"清单，双击时自动弹 UAC。**不加这个的话程序看着在跑、游戏里毫无反应**，原因见下 |
| `--onefile` | 打成单个 exe，方便挪动。代价是每次启动要先解包 |
| `--hidden-import soundcard.mediafoundation` | soundcard 按平台条件导入后端，声明一下防止漏收 |
| `--distpath` / `--workpath` / `--specpath` | 产物放 `钓鱼\`，编译中间物和 `.spec` 丢进 `.workbuddy\scratch\pyi\`，不污染工作目录 |

### 为什么必须带 `--uac-admin`

三角洲的 ACE 反作弊让游戏跑在**管理员权限**下。Windows 的 UIPI 规定：
普通权限进程**不能**向提权窗口注入输入，而且是**静默丢弃** —— 不报错、不弹窗。

所以不带这个清单的话，exe 能启动、界面正常、检测也正常，**但点不动游戏**。
这正是之前查了一整轮的那个故障（`autofish.bat` 里那套自动提权就是为它加的）。

不带清单的版本也想编，就 `build_exe.bat noadmin`。

---

## 四、为了无控制台做的三处改造

`--windowed` 之后 `sys.stdout` 和 `sys.stderr` 都变成 `None`。
这不是"看不见输出"这么简单 —— 任何一个 `print()` 都会直接
`RuntimeError: lost sys.stdout`，而且因为是窗口程序，**连崩溃信息都看不到**。
所以 `autofish.py` 里加了三道防护（都在最上面一段）：

**1. `ensure_std_streams()`** —— 启动第一件事。检测到没有控制台，就把
stdout/stderr 接到空设备上，`print()` 恢复正常，不再抛异常。

**2. `install_crash_handler()`** —— 未捕获异常写进
`%LOCALAPPDATA%\AutoFish\crash.log`，并弹一个错误窗告诉你文件在哪。
同时挂钩 `threading.excepthook`：后台线程（采集线程、收鱼序列）的异常
不再无声消失，会写进 `crash.log` 和 `autofish.log`。

**3. 覆盖 Tk 的 `report_callback_exception`** —— 界面回调里抛的异常默认只往
`sys.stderr` 打印。Tkinter 的源码注释原话是"sys.stderr 为 None 时应用应当覆盖它"，
于是把它也接到了同一个崩溃处理器上。

有了这三条，exe 出问题至少能查，不会是"双击没反应"。

---

## 五、验证过什么

| 检查 | 方法 | 结果 |
| --- | --- | --- |
| **无黑窗口** | 直接解析 exe 的 PE 头读 `Subsystem` 字段 | `2 = WINDOWS_GUI` ✅ |
| **自动提权** | 扫描内嵌清单 | `requireAdministrator=True`，`asInvoker=False` ✅ |
| 无控制台下的 `print()` | 把 `sys.stdout/stderr` 置成 `None` 后跑完整流程 | 不再抛 `lost sys.stdout` ✅ |
| 崩溃留痕 | 故意抛异常 + 后台线程抛异常 | `crash.log` 与 `autofish.log` 都有记录 ✅ |
| 冻结后能真跑 | 启动测试版 exe，等它把窗口画出来 | 窗口正常、日志有 `运行方式：独立 exe` ✅ |
| **构建脚本的命令行** | 从 `.bat` 抽出真实命令行 → 用 Windows 的 `CommandLineToArgvW` 切分 → 再原样执行 | 19 个参数全部正确、构建成功 ✅ |
| 构建脚本静态检查 | 纯 ASCII / `^` 续行完好 / 引号内路径不以反斜杠结尾 | 全部 PASS ✅ |

"无黑窗口"这一条我特意用**读 PE 头**而不是"启动看看"来验证 ——
子系统标记是文件本身的静态事实，比肉眼观察更硬。

"命令行"那一条是 2026-09-19 补的。之前只做了静态检查，结果漏掉了那个
`\"` 吞参数的问题（详见第三节的说明）；现在改成**把 `.bat` 里的命令行抽出来、
按 Windows 规则切一遍、再原样跑一次**，等于把"cmd 自身语法"以外的环节都测到了。
顺手把这个检查做成了脚本 `lint_bat.py`，放在技能
`~/.workbuddy/skills/pyinstaller-windowless-exe/` 里，改完 `.bat` 可以跑一下。

---

## 六、几种启动方式的对比

| | `AutoFish.exe` | `autofish.bat` | `python autofish.py` |
| --- | --- | --- | --- |
| 黑窗口 | **没有** | 有 | 有（就是控制台本身） |
| 自动提权 | 有（内嵌清单） | 有（批处理自己重启） | 无 |
| 需要 Python 环境 | **不需要** | 需要 | 需要 |
| 改代码后生效 | 要重新打包 | 直接生效 | 直接生效 |
| 启动耗时 | 约 2.6～4.2 秒 | 快 | 快 |
| 命令行参数 | 基本不用（无控制台） | 可用 | 可用 |

`--list`、`--calibrate` 这类要往控制台打字的参数**在 exe 里没有意义**
（没有控制台可看）。需要的时候还是用 `.bat` 或 `python autofish.py`；
标定在界面里点「标定…」按钮就行。

---

## 七、该知道的事

- **`--onefile` 实际上有两个进程**：一个 bootloader（负责解包、等待），
  一个才是真正的 Python 程序。任务管理器里看到两个 `AutoFish.exe` 是正常的。
  bootloader 自己也有一个隐藏窗口（类名 `PyInstallerOnefileHiddenWindow`）。
- **首次启动慢**：单文件包每次启动都要把自己解到临时目录，实测 2.6～4.2 秒。
  介意的话改成 `--onedir`（在 `build_exe.bat` 里把 `--onefile` 换成 `--onedir`），
  启动接近瞬时，代价是产物变成一个文件夹而不是单个 exe。
- **杀软/反作弊可能误报**：PyInstaller 打出来的 exe 被误报是常见现象。
  这只影响"能不能运行"，不影响它的能力范围 —— exe 和 `.bat` 做的事完全一样，
  没有新增任何权限或手段。
- **`--onefile` 会把 exe 解到 `%TEMP%\_MEIxxxx`**，如果系统盘空间紧张或清理软件很激进，
  换 `--onedir` 更稳。
- **exe 与 `.bat` 共用 `%LOCALAPPDATA%\AutoFish\`**，标定结果互通，不用重新标定。
- 想给 exe 加图标：加 `--icon 路径.ico`，图标文件得是 `.ico` 格式。
