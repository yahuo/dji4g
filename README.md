# dji4g

在电脑上用大疆第一代 4G 增强图传模块（`Baiwang`，USB ID `2ca3:4006`）收发短信，并
诊断它的 4G 网络。macOS 有原生菜单栏应用，Omarchy（基于 Arch Linux 和 Hyprland 的
桌面）有状态栏面板，两者内置同一个 Python 短信引擎；命令行工具负责诊断和收发短信。
所有程序只在本机运行，不开放网络端口。

本项目与大疆无关，不是官方工具。工具不刷写固件、不修改 VID/PID，也不改动主机的
网络连接、DNS 或路由；`set-mode` 会持久改写模块的 USB 模式，必须显式确认。

## 功能

- **收短信**：模块插着时每 8 秒轮询 `ME/SM` 存储，按原始 PDU 去重，长短信自动
  合并，新短信弹系统通知。
- **验证码**：识别正文里的验证码，一键复制。
- **发短信**：自动选择 GSM 7-bit 或 UCS2 编码，长短信最多 10 段；必须先预览再按住
  确认，结果不明时不会自动重发。
- **随插拔启停**：插入模块后启动引擎、显示图标，拔出后停止引擎、隐藏图标。
- **模块状态**：SIM、网络注册、制式、频段、RSRP / SINR 和短信存储占用。
- **清理存储**：存储将满时，确认后删除模块 `ME/SM` 里已存到本机的短信，本机记录
  保留。
- **删除短信**：展开短信后确认，即可从本机删除这一条；模块里的副本不会让它重新出现。
- **网络诊断**：经模块网卡验证 DHCP、模块 DNS 和 HTTPS。
- **本机存储**：短信和发送状态保存在只有当前用户可读写的 SQLite 中。

## 硬件与系统

| 项目 | 说明 |
| --- | --- |
| 模块 | 大疆第一代 4G 增强图传模块，型号 `QDC507`，固件 `QDC507GLEFM21` |
| USB 模式 | `usbnet=1`（CDC-ECM），短信与 4G 网卡可同时使用 |
| macOS | macOS 14+；ECM 网卡显示为 `Baiwang` |
| Linux | 命令行适用于一般 Linux 发行版，状态栏面板需要 Omarchy；ECM 网卡由内核 `cdc_ether` 驱动 |

运行需要 Python 3.10+ 和 `libusb`。构建 macOS 应用需要 Xcode 命令行工具，编译应用
图标需要完整的 Xcode 26 及以上。

## 快速开始

### 1. 安装依赖

```bash
git clone https://github.com/yahuo/dji4g.git
cd dji4g
```

macOS：

```bash
brew install libusb
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Omarchy / Arch Linux：

```bash
sudo pacman -S --needed python python-pip libusb usbutils iproute2 iputils bind curl libnotify
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

之后的命令都在项目根目录执行。

### 2. Linux：USB 权限

插入模块后，先检查当前用户是否已有访问权限：

```bash
dev=/dev/bus/usb/$(lsusb -d 2ca3:4006 | sed -E 's/^Bus ([0-9]+) Device ([0-9]+).*/\1\/\2/')
getfacl -p "$dev" | grep "^user:$USER:rw"
```

有输出就跳过这一步。Baiwang 带 ADB 接口，较新 systemd 自带的 `70-uaccess.rules`
会给这类设备打 `uaccess` 标签，Omarchy 默认即可访问。没有输出，或系统装有
ModemManager 时，安装项目规则：

```bash
sudo install -m 644 deploy/linux/70-dji4g.rules /etc/udev/rules.d/70-dji4g.rules
sudo udevadm control --reload-rules
# 随后拔插模块，让新规则生效
lsusb -d 2ca3:4006
```

规则只允许当前本地活动会话的用户访问 Baiwang，并让 ModemManager 忽略该设备，避免
争用 AT 接口；不会授予所有用户写权限，也不面向仅 SSH 登录的会话。不要用
`sudo python` 运行本工具。

程序会跳过 CDC/无线网络接口和内核已占用的接口，不会自动解绑驱动。报告
`kernel driver; not detached` 时先用 `lsusb -t` 检查绑定，不要为取得 AT 接口而解绑
整台设备或 `cdc_ether` 网卡。

### 3a. macOS：安装菜单栏应用

```bash
macos/build.sh --install             # 不带参数时只生成 macos/build/DJISMS.app
open /Applications/DJISMS.app
```

应用常驻后台、不占 Dock，没插模块时菜单栏不显示图标。应用退出后就不再随插拔启动，
建议插上模块后在面板右上角菜单开启“登录时启动”。

- 应用按构建时的项目路径寻找 `.venv/bin/python` 和 `dji4g_desktop.py`。移动项目后
  重新构建，或执行 `defaults write io.github.yahuo.dji4g.sms EngineRoot <项目路径>`。
- 应用为 ad-hoc 签名，只适合在本机构建、本机使用。
- 只装了命令行工具、没有完整 Xcode 时，构建会跳过应用图标并给出提示。

### 3b. Omarchy：安装状态栏面板

面板是用户级 Quickshell 插件，不修改 Omarchy 系统文件。插件通过固定入口
`~/.local/lib/dji4g` 找到项目：

```bash
mkdir -p ~/.local/lib
# 此固定入口指向项目；若已存在，先确认其用途，不要覆盖其他目录
ln -sT "$PWD" ~/.local/lib/dji4g
mkdir -p ~/.config/omarchy/plugins/dji4g.sms
install -m 644 deploy/omarchy/dji4g.sms/{manifest.json,Panel.qml,Service.qml} \
  ~/.config/omarchy/plugins/dji4g.sms/
# 必需：给模块一个固定的设备单元，面板据此随插拔启停
sudo install -m 644 deploy/linux/71-dji4g-hotplug.rules /etc/udev/rules.d/71-dji4g-hotplug.rules
sudo udevadm control --reload-rules
# 可选：插拔时立即通知面板
install -D -m 644 deploy/omarchy/dji4g-sms-panel.service \
  ~/.config/systemd/user/dji4g-sms-panel.service
systemctl --user daemon-reload
omarchy plugin validate ~/.config/omarchy/plugins/dji4g.sms
omarchy-shell shell rescanPlugins
omarchy plugin enable dji4g.sms
# 随后拔插模块，让新规则生效
systemctl --user status dev-dji4g.device
```

- `71-dji4g-hotplug.rules` 给模块一个与 USB 口无关的设备单元 `dev-dji4g.device`，
  面板只认 systemd 报告的该单元状态。没装这条规则，面板始终隐藏。它与权限规则
  `70-dji4g.rules` 互不依赖。
- `dji4g-sms-panel.service` 绑定到同一设备单元，插入和拔出时各通知面板一次，所以
  启停是即时的，不需要 `enable`。不装也能自行发现：插入最长晚 30 秒，拔出要等引擎
  发现模块断开。
- 更新插件后执行 `omarchy restart shell` 重新加载。停用：
  `omarchy plugin disable dji4g.sms`，引擎随之停止，不删除数据。

## 使用

### macOS 菜单栏应用

- 左键点击菜单栏图标打开面板，右键直接写短信。手风琴行原地展开短信，验证码单独
  显示为按钮，写短信在面板内完成，按住发送确认。快捷键与 Omarchy 面板相同。
- 新短信用系统通知，像“信息”一样显示完整号码和正文，可直接“复制验证码”；发件人
  是号码时还可“回复”。回复只把内容填进写短信界面并生成预览，仍需在面板里按住
  发送。
- URL：`dji4g://open` 打开面板，`dji4g://latest` 展开最新一条短信，
  `dji4g://compose?to=<号码>&body=<正文>` 进入写短信并预填（参数可省略）。任何网页
  或应用都能打开这些 URL，所以预填后不会自动预览，仍需手动预览并按住发送。
- 没插模块时，URL 和通知里的“回复”都不打开面板，“复制验证码”照常可用。
- 存储将满的提示右侧有“清理”，点“删除”确认后才删除模块里已存到本机的短信，本机
  记录不变，`Esc` 取消；右上角菜单的“清理模块短信…”随时可用。
- 展开短信后点垃圾桶“从本机删除”，再点“删除”确认才删除这条本机记录，`Esc` 取消。
  模块里若还有这条短信，不会再显示或通知，点“清理”时一并删除。
- 面板右上角菜单可退出应用。没插模块时图标隐藏，可用
  `osascript -e 'quit app id "io.github.yahuo.dji4g.sms"'` 退出。

### Omarchy 面板

左键点击状态栏短信图标打开面板，右键直接写短信。有未读时图标变为实心并带角标；
模块已插入但未就绪（连接中、无 SIM、未注册）时图标变暗。面板顶部显示信号格、网络
制式 / 频段、RSRP / SINR 和短信存储占用，存储将满、发送结果未确认等异常才多出一行
提示。存储将满的提示右侧有“清理”，点“删除”确认后才删除模块里已存到本机的短信，
本机记录不变，`Esc` 取消。

列表显示最近 50 条短信，默认掩码号码、不显示正文；展开短信才读取正文并标为已读。
识别到的验证码单独显示为按钮，点击或按 `c` 即复制。展开后点垃圾桶“从本机删除”，
再点“删除”确认才删除这条本机记录，`Esc` 取消；模块里若还有这条短信，不会再显示或
通知，点“清理”时一并删除。

| 按键 | 作用 |
| --- | --- |
| `↑` `↓` / `j` `k` | 移动光标 |
| `Enter` / `Space` / `→` | 展开短信（读取正文并标为已读） |
| `←` / `Esc` | 收起；再按 `Esc` 关闭面板 |
| `c` | 复制验证码，没有验证码时复制正文 |
| `n` / `r` / `a` | 写短信 / 刷新 / 全部已读 |
| `Tab` | 写短信时在号码、正文、按钮间切换 |
| `Ctrl+Enter` | 写短信时生成预览 |

可绑定到 Hyprland 快捷键的 IPC：

```bash
omarchy-shell dji4g.sms toggle    # 开关面板，另有 open / close
omarchy-shell dji4g.sms latest    # 打开并展开最新一条收到的短信
omarchy-shell dji4g.sms compose   # 打开并进入写短信
omarchy-shell dji4g.sms.service device   # 面板看到的模块状态：up / down
```

模块未插入时，`toggle` / `open` / `latest` / `compose` 不做任何事。

草稿只在内存中：在写短信界面关闭面板（例如误点面板外）会保留号码和正文，下次打开
继续；返回收件箱后再关闭、发送被模块接受或 Shell 重启则清除。关闭面板总会作废预览
确认。

### 命令行

同一时间只能有一个进程使用模块的 AT 接口。运行命令行前，先退出菜单栏应用或停用
面板。

```bash
python dji4g.py --help
python dji4g.py sms --help
```

#### 诊断

```bash
# 模块、SIM、信号、运营商、PDP 和 USBNet 状态
python dji4g.py status

# 只读验证短信 AT 能力
python dji4g.py sms-check

# 验证 LTE、DHCP、模块 DNS 和绑定 Baiwang 网卡的 HTTPS（macOS/Linux）
python dji4g.py network-test

# 模块自身 QPING；实际联网能力仍以 network-test 为准
python dji4g.py modem-ping 1.1.1.1
```

`network-test` 会通过模块产生少量 4G 流量。它向模块网关查询 DNS，再用
`curl --resolve` 带着该结果绑定 Baiwang 网卡访问 HTTPS，不使用系统 DNS。主机用代理
的 Fake-IP DNS 时，普通解析会返回 `198.18.x.x`，绑定物理网卡访问这类地址会失败，
不要据此判断模块不能联网。

Linux 上 `network-test` 通过 sysfs 的 USB VID/PID 定位网卡，不假设名称为 `usb0`。
网卡需已由 NetworkManager 等取得 DHCP IPv4 地址和默认网关；存在多个匹配网卡、多个
IPv4 地址或多个不同默认网关时会拒绝猜测。

#### 查看与监听短信

```bash
# 读取 ME/SM 中的短信，可限定存储或读取模式
python dji4g.py sms list
python dji4g.py sms list --storage ME
python dji4g.py sms list --mode text

# JSON 输出；默认仍掩码发送方且不输出原始 PDU
python dji4g.py sms list --json
python dji4g.py sms list --show-sender

# 先记录现有短信，再轮询打印新短信
python dji4g.py sms watch
```

读取和监听都不会执行 `AT+CMGD`，不会清空 `ME` 或 `SM`。`watch` 以轮询为准，不依赖
单次 USB URC 发现新短信。

#### 发送短信

不带 `--execute` 时只生成预览，不访问模块：

```bash
python dji4g.py sms send --to 10086 --text "example"
printf '%s' 'example' | python dji4g.py sms send --to 10086 --text-file -
```

预览显示掩码收件人、编码和分段数。真正发送要加 `--execute`，再在控制终端里准确
输入 `SEND`，没有 `--yes` 之类的非交互绕过选项：

```bash
python dji4g.py sms send --to 10086 --text "example" --execute
```

`--mode text` 只支持单段短信。命令行发送不请求送达回执。

#### USB 模式切换

模式切换会持久写入模块并重启模块，因此必须显式加 `--yes`：

```bash
python dji4g.py set-mode mac --yes
python dji4g.py set-mode linux --yes
python dji4g.py set-mode windows --yes
python dji4g.py set-mode restore --yes
```

`mac` 和 `linux` 都选择 CDC-ECM（`usbnet=1`），当前已是 ECM 时无需切换。短信引擎
不会调用这些命令，也不会改变当前的 `usbnet`。

## 工作方式

`dji4g_desktop.py` 是两个界面共用的短信引擎。界面以子进程启动它，通过标准输入输出
交换 JSON 行；引擎在进程内独占模块，不监听任何网络端口，也不访问公网。

- 唯一的 AT Worker 串行执行探测、短信扫描、发送和清理；每 8 秒轮询 `ME/SM`，USB
  断开后退避重连。
- 每 15 秒采集 SIM、网络注册和脱敏后的无线状态（制式、Band、RSRP/SINR 等，不含
  PLMN、Cell ID、SIM 标识），并跟踪 `ME/SM` 的已用与总容量。占用达到 80% 并连续两次
  确认才进入 `near_full`，降到 70% 及以下才恢复；达到总容量立即进入 `full`。
- SQLite 保存短信、长短信分段、发送状态和逐段送达回执；以原始 PDU 的 SHA-256 去重，
  不把 storage/index 当作消息身份。
- 两个界面都只启动一个引擎进程，并且只在模块插着时运行它；拔出或界面退出即停止，
  进程意外退出后自动重启。切换 USB 模式或模块重启会重新枚举，引擎随之重启一次。
- 因此只有界面运行且模块插着时才接收短信。期间错过的短信仍在模块的 `ME/SM` 里，
  下次启动时补收。Omarchy 插件随桌面 Shell 加载，Shell 未运行（例如未登录桌面）时
  不接收。
- 所有 USB AT 操作共用一个跨进程锁，同一数据目录只能运行一个引擎。另一个引擎在运行
  时，新引擎报告“另一个短信进程正在使用短信数据”后退出。

macOS 的数据位置：

```text
~/Library/Application Support/dji4g/gateway.sqlite3
~/Library/Application Support/dji4g/desktop/desktop.sqlite3
~/Library/Application Support/dji4g/at.lock
```

Linux 的根目录为 `${XDG_DATA_HOME:-~/.local/share}/dji4g`，结构相同；相对路径的
`XDG_DATA_HOME` 会被忽略。命令行和界面应使用一致的环境，才能共用数据和 AT 锁。
`desktop/desktop.sqlite3` 只保存界面状态：消息 ID、通知游标、未读标记和发送尝试，
不额外保存号码或正文。

## 安全与隐私

- **数据**：数据目录权限 `0700`，数据库 `0600`，内容是本机明文 SQLite，请配合登录
  保护和磁盘加密（macOS FileVault / Linux LUKS）使用。普通日志不写入完整号码、
  短信正文、验证码或原始 PDU。
- **通知**：macOS 通知显示完整号码和正文，会进入通知中心和锁屏，可在系统设置的通知
  中关闭预览。Omarchy 通知只显示数量，不显示号码、正文或验证码。
- **剪贴板**：Omarchy 上复制经 `wl-copy --sensitive` 走标准输入，不出现在命令参数
  里，并带敏感标记，遵循该标记的剪贴板历史不会记录。
- **发送确认**：界面发送必须先预览，再按住“按住发送”约 1 秒（鼠标或 `Enter` /
  `Space`），中途松开即取消。确认绑定原始内容、5 分钟过期、只能用一次，改动号码或
  正文即作废；发送期间输入框只读，无法重复提交。
- **不自动重发**：提交 PDU 后超时或进程中断，状态记为结果未知（`unknown`），不会自动
  重试，以免重复发送。界面会持续提示，自行核实后点击提示右侧的对勾即可消除；这只
  消除提示，不会重发，也不会让已用过的确认重新生效。模块在有未决发送时被拔出，图标
  保留并提示，再次插入后提示仍在，直到确认。
- **送达**：“模块已接受”不等于对方收到。引擎为每个 PDU 分段请求状态报告，收到
  `+CDS` 后更新状态，界面暂不显示送达回执。
- **只删已存到本机的短信**：只有在界面点“清理”并确认后才执行 `AT+CMGD`。清理时
  AT Worker 重新扫描 `ME/SM`，每条短信写入本机数据库后才删除它所在的位置，无法解析
  或保存的留在模块；本机记录不受影响。存储将满不会自动删除，命令行也不删除短信。
  “全部已读”只改本机未读状态。
- **本机删除**：只有在界面展开短信、点删除并确认后才删除本机记录，不动模块，删除后
  无法恢复。号码、正文和原始 PDU 被清空，只留去重用的指纹，模块里的副本因此不会
  重新入库或通知；数据库开启 `secure_delete`，被删内容在数据库文件中被覆盖，但已进入
  备份（如 Time Machine）的副本不受影响。
- **通知去重**：首次启动以当前最新消息为基线，历史短信不弹通知；通知游标和未读状态
  持久保存，正常重启不重复通知。若通知已发出、游标尚未提交时进程崩溃，该批通知可能
  重复。
- **拔出即清除**：模块拔出后引擎被终止，界面里已读入的短信列表与正文一并清除。

## 许可证

[MIT](LICENSE)
