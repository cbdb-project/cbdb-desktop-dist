# cbdb-desktop-dist

Turns one upstream **CBDB-Desktop** release archive into three shippable
distributions — macOS Apple Silicon, macOS Intel, and Windows x64 — **from a
Windows machine**, without ever modifying the upstream source.

```
python -m pip install -r requirements.txt   # once
copy .env.sample .env                        # then edit it
python crossbuild/build-dist.py              # all three targets, ~10 minutes
```

## What it produces

| Target | Archive | Notes |
| --- | --- | --- |
| macOS Apple Silicon | `CBDB-Desktop-mac-arm64.tar.gz` | carries the ad-hoc signature arm64 requires, applied by the linker |
| macOS Intel | `CBDB-Desktop-mac-x64.tar.gz` | |
| Windows x64 | `CBDB-Desktop-win-x64.zip` | plus a `.ico` for a desktop shortcut |

A Go binary targets one CPU architecture, so macOS genuinely needs two
archives. Each is about 390 MB, dominated by the 1.3 GB SQLite database it
carries.

## Why it is not just `tar`

Three defects in the upstream Go source have to be fixed **in the code**; no
amount of packaging works around them. The kit copies the Go module to a
scratch directory, applies anchored patches to the *copy*, and builds from
that — the upstream tree is never written to, and the last phase proves it.

* Assets were resolved against the **current working directory**. Windows
  Explorer happens to set that to the executable's folder; macOS Finder sets it
  to the user's home directory, so a double-click died with `Database file not
  found`.
* `/static/` was registered **twice**, and the earlier registration —
  hardcoded to `./Static/` — won, so every asset 404'd unless the program
  started in its own directory. Go serves a 404 as `text/plain`, which is why
  the browser reported it as a *stylesheet MIME-type error* rather than a
  missing file.
* `SetupNavigationRoutes`'s error was discarded: on failure no route was
  registered at all, yet the server still started.

A fourth change is hardening rather than a fix for anything observed: MIME
types for the bundled assets are registered explicitly instead of being read
from the host's MIME database.

`--audit-only` reports **five** findings: those four plus
`database-filename-case`, which is a consequence the first patch happens to
fix (upstream's default is `Data/CBDB.db`, the shipped file is
`Data/cbdb.db`).

Cross-compiling cgo to macOS from Windows also needs `zig cc` plus
hand-written `.tbd` stubs for `libresolv`, CoreFoundation and Security, since
zig ships only `libSystem.tbd`.

**[AGENTS.md](AGENTS.md) documents every trap in detail** — paths with spaces
silently breaking `$CC`, NTFS having no execute bit, `gofmt` exit codes,
Gatekeeper, console encoding — and is the first thing to read before changing
anything here.

## Verification

Nothing is given a release name until it has been checked. Each archive is
compared against a manifest of the folder that produced it: every member's
size and SHA-256, every file mode, the executable entries, and — for
`.tar.gz` — that the gzip stream runs to EOF.

That thoroughness is not decorative. A weaker check was demonstrated to pass
an archive containing a **zero-byte binary, an empty `Templates/` and no
stylesheet**, while printing `database: PRISTINE`.

## Requirements

* Windows, with **Go 1.21+** and **Zig 0.16+** (`winget install zig.zig`)
* **Python 3.8+** and `pip install -r requirements.txt`
* An upstream CBDB-Desktop release archive (`.7z` or `.zip`)

## Configuration

Copy `.env.sample` to `.env`. Everything is optional. Resolution order is
**command line > a non-empty `CBDB_*` environment variable > `.env` > default**,
so CI can override without editing anyone's file.

| Setting | Meaning |
| --- | --- |
| `CBDB_ARCHIVE` | Path to the upstream archive. Can point anywhere — a Dropbox folder, a share — so a 200 MB input need not be copied into the repository. |
| `CBDB_USE_INPUT_ICON` | Take the app icon from the upstream payload (default `true`) rather than this repository's generated one. |
| `CBDB_INPUT_ICON` | Which `.ico`, when the payload ships more than one. |

`.env` is gitignored: it holds machine-specific paths.

## Taking a new upstream drop

Point `CBDB_ARCHIVE` at the new archive and re-run. Nothing here names a
version, a folder or a path — the payload root is found by looking for the
directory that holds both `Code/` and `Data/`.

Run `python crossbuild/build-dist.py --audit-only` first. If a patch reports
`already fixed upstream`, upstream adopted it and it is skipped. If the run
stops with `cannot classify`, upstream edited those exact lines — read the new code and
rewrite the patch rather than loosening the anchor. A patch that applies in the
wrong place is worse than one that fails loudly.

## What is not in this repository

The upstream archive, `extracted/`, `dist/`, `packages/`, `maintainer-tools/`
and the database are all gitignored — they are input or reproducible output,
and a single `cbdb.db` is 1.3 GB. `.gitignore` explains each one.

## Licence and provenance

The build tooling here is ours. **No licence has been set yet**, which in
practice means all rights reserved: the code is readable but not yet free to
reuse. Ask the CBDB project if you want to.

The CBDB-Desktop application and its database belong to the [China
Biographical Database Project](https://projects.iq.harvard.edu/cbdb) and are
**not** in this repository — the build reads them from an archive you supply.

The one exception is artwork: `LOGO.png` is the CBDB badge, committed as the
master for the fallback icon, and `crossbuild/assets/favicon.ico` is a recolour
of it. Both are CBDB's, included here by the project itself. By default the
build does not even use them — it takes the icon from the upstream archive.

The `.tbd` files under `crossbuild/macstub/` are hand-written stubs containing
only symbol names and placeholder UUIDs. No Apple SDK content is reproduced.

---

# cbdb-desktop-dist（中文）

把一个上游 **CBDB-Desktop** 发布压缩包，转成三个可分发的版本——macOS Apple
Silicon、macOS Intel、Windows x64——**全部在 Windows 上完成**，且不修改上游源码。

```
python -m pip install -r requirements.txt   # 一次
copy .env.sample .env                        # 然后编辑它
python crossbuild/build-dist.py              # 三个目标，约 10 分钟
```

## 产出什么

| 目标 | 归档 | 说明 |
| --- | --- | --- |
| macOS Apple Silicon | `CBDB-Desktop-mac-arm64.tar.gz` | 带 arm64 必需的 ad-hoc 签名，由链接器加上 |
| macOS Intel | `CBDB-Desktop-mac-x64.tar.gz` | |
| Windows x64 | `CBDB-Desktop-win-x64.zip` | 另附一个用于桌面快捷方式的 `.ico` |

Go 编译出的是单架构二进制，所以 macOS 确实需要两个包。每个约 390 MB，主要
体积来自随包的 1.3 GB SQLite 数据库。

## 为什么不是简单打个包

上游 Go 源码有三处缺陷**必须改代码**，打包层面绕不过去。本工具把 Go 模块复制到
临时目录，对**副本**打锚定补丁，再从副本构建——上游树全程只读，最后一个阶段会
证明这一点。

* 资源路径按**当前工作目录**解析。Windows 资源管理器恰好把它设成可执行文件所在
  目录，而 macOS Finder 设成用户主目录，于是双击直接死在 `Database file not
  found`。
* `/static/` 被注册了**两次**，而先注册的那条——硬编码成 `./Static/`——胜出，
  导致除非从程序自身目录启动，否则所有资源 404。Go 的 404 响应是
  `text/plain`，所以浏览器报的是**样式表 MIME 类型错误**而不是文件缺失。
* `SetupNavigationRoutes` 的错误被丢弃：失败时一条路由都不注册，服务器却照常
  启动。

第四处改动属于加固而非修复已观察到的问题：随包资源的 MIME 类型改为显式注册，
不再依赖宿主机的 MIME 数据库。

`--audit-only` 会报**五**项发现：上述四项，加上 `database-filename-case`——
它是第一个补丁顺带修掉的后果（上游默认值是 `Data/CBDB.db`，而随包文件是
`Data/cbdb.db`）。

从 Windows 交叉编译 cgo 到 macOS 还需要 `zig cc`，外加手写的 `libresolv`、
CoreFoundation、Security 三个 `.tbd` 桩文件——zig 只自带 `libSystem.tbd`。

**[AGENTS.md](AGENTS.md) 详细记录了每一个坑**：路径含空格如何静默破坏 `$CC`、
NTFS 没有执行位、`gofmt` 的退出码语义、Gatekeeper、控制台编码——改动本项目前
请先读它。

## 校验

任何产物在通过校验之前都不会获得发布名。每个归档都要与生成它的文件夹清单逐项
比对：每个成员的大小与 SHA-256、每个文件的权限位、可执行项清单，以及
`.tar.gz` 的 gzip 流是否完整到 EOF。

这种严格不是装饰。一个较弱的版本曾被实测证明：**二进制 0 字节、`Templates/`
全空、没有样式表**的归档照样通过，还打印 `database: PRISTINE`。

## 环境要求

* Windows，装有 **Go 1.21+** 与 **Zig 0.16+**（`winget install zig.zig`）
* **Python 3.8+**，并 `pip install -r requirements.txt`
* 一个上游 CBDB-Desktop 发布压缩包（`.7z` 或 `.zip`）

## 配置

把 `.env.sample` 复制成 `.env`。所有项都可选。优先级是**命令行 > 非空的 `CBDB_*`
环境变量 > `.env` > 默认值**，所以 CI 不必改动任何人的文件就能覆盖。

| 配置项 | 含义 |
| --- | --- |
| `CBDB_ARCHIVE` | 上游压缩包的路径。可以指向任意位置——Dropbox 目录、网络共享——所以 200 MB 的输入不必复制进仓库。 |
| `CBDB_USE_INPUT_ICON` | 从上游载荷取应用图标（默认 `true`），而不是用本仓库生成的那个。 |
| `CBDB_INPUT_ICON` | 当载荷里有多个 `.ico` 时，指定用哪一个。 |

`.env` 已加入 gitignore：它记录的是本机特有的路径。

## 换新的上游版本

把 `CBDB_ARCHIVE` 指向新压缩包，重新运行即可。本仓库不写死任何版本号、目录名或
路径——载荷根目录是靠"同时含有 `Code/` 和 `Data/`"探测出来的。

先跑 `python crossbuild/build-dist.py --audit-only`。若某个补丁报
`already fixed upstream`，说明上游已采纳，会自动跳过。若运行中止并报 `cannot classify`，
说明上游改动了那几行——请阅读新代码后重写补丁，**不要**为了让它匹配而放宽锚点。
打错位置的补丁比大声失败的补丁更糟。

## 仓库里没有什么

上游压缩包、`extracted/`、`dist/`、`packages/`、`maintainer-tools/` 以及数据库
全部在 gitignore 里——它们要么是输入、要么是可重建的产物，而单个 `cbdb.db` 就有
1.3 GB。`.gitignore` 里对每一项都写了理由。

## 授权与归属

本仓库的构建工具是我们自己的。**尚未设定授权协议**，因此在法律上属于保留所有
权利：代码可阅读，但暂不可自由复用。如需使用请联系 CBDB 项目方。

CBDB-Desktop 应用本身及其数据库属于
[中国历代人物传记资料库（CBDB）项目](https://projects.iq.harvard.edu/cbdb)，
且**不在**本仓库内——构建时从你提供的压缩包读取。

唯一的例外是美术资源：`LOGO.png` 是 CBDB 的徽标，作为备用图标的母图提交在此，
`crossbuild/assets/favicon.ico` 是它的改色版。两者都属于 CBDB，由项目方自己
放入。而且默认情况下构建根本不用它们——图标取自上游压缩包。

`crossbuild/macstub/` 下的 `.tbd` 文件是手写桩，只含符号名与占位 UUID，不复制
任何 Apple SDK 内容。
