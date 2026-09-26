import AppKit
import Darwin
import Foundation

/// 菜单栏 App 单实例文件锁的 fd（进程存活期间保持打开，锁才有效；进程退出内核自动释放）
var barLockFD: Int32 = -1

// MARK: - 命令行模式（便于脚本/诊断，不弹界面）

func runCheck() {
    let cfg = RouterConfig.load()
    let (ok, models) = syncHealth(port: cfg.port)
    print("配置文件   : \(configPath)")
    print("监听端口   : \(cfg.port)")
    print("运行状态   : \(ok ? "运行中" : "未运行")")
    print("开机自启   : \(loginItemEnabled() ? "已启用" : "未启用")")
    print("默认模型   : \(cfg.defaultModel.isEmpty ? "（未设置，回退到清单第一个）" : cfg.defaultModel)")
    print("模型 \(models.count) 个:")
    for m in models { print("  - \(m)") }
    let recent = tailLog(5, onlyRequests: true)
    if !recent.isEmpty {
        print("最近请求:")
        for line in recent { print("  \(line)") }
    }
}

let cliArgs = CommandLine.arguments
if cliArgs.contains("--check") || cliArgs.contains("--diagnose") {
    runCheck()
    exit(0)
}
if cliArgs.contains("--start") {
    RouterService().start()
    Thread.sleep(forTimeInterval: 1.5)
    runCheck()
    exit(0)
}
if cliArgs.contains("--stop") {
    RouterService().stop()
    print("已停止路由服务")
    exit(0)
}
if cliArgs.contains("--restart") {
    RouterService().restart()
    Thread.sleep(forTimeInterval: 2.0)
    runCheck()
    exit(0)
}
if cliArgs.contains("--login-on") {
    print(setLoginItem(true) ? "已启用开机自启（路由服务 + 菜单栏图标）" : "启用失败")
    exit(0)
}
if cliArgs.contains("--login-off") {
    _ = setLoginItem(false)
    print("已关闭开机自启")
    exit(0)
}

// MARK: - 性能打点
//
// 只在超过阈值时往 bar.log 写一行，平时零开销。用来回答「点一下怎么有点慢」：
// 若日志里出现 menuNeedsUpdate 的行，说明慢在菜单构建（菜单项变多 / 依赖变慢）；
// 若出现 action.* 的行，说明慢在那个动作本身（如打开浏览器、launchctl 调用）。
private let perfThresholdMS: Double = 20
private let perfFormatter: DateFormatter = {
    let f = DateFormatter()
    f.dateFormat = "HH:mm:ss.SSS"
    return f
}()

func perfLog(_ label: String, _ ms: Double) {
    guard ms >= perfThresholdMS else { return }
    let line = "[\(perfFormatter.string(from: Date()))] PERF \(label) \(String(format: "%.0f", ms))ms\n"
    guard let h = openAppendHandle(barLogPath), let data = line.data(using: .utf8) else { return }
    h.write(data)
    try? h.close()
}

func measure<T>(_ label: String, _ body: () -> T) -> T {
    let t0 = CFAbsoluteTimeGetCurrent()
    let result = body()
    perfLog(label, (CFAbsoluteTimeGetCurrent() - t0) * 1000)
    return result
}

// MARK: - 菜单栏应用

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    let service = RouterService()
    let menu = NSMenu()
    var statusItem: NSStatusItem!

    // 固定菜单项：启动时构建一次，之后只更新「文本 / 可用性 / 子菜单内容」，绝不重建结构。
    // 旧实现每次打开菜单都 removeAllItems + 重建上百个菜单项，菜单项越多弹出越慢，且纯属浪费。
    private var headItem: NSMenuItem!
    private var fuelItem: NSMenuItem!
    private let fuelMenu = NSMenu()
    private var creditsItem: NSMenuItem!
    private let creditsMenu = NSMenu()
    private var copyItem: NSMenuItem!
    private var defItem: NSMenuItem!
    private let defMenu = NSMenu()
    private var logItem: NSMenuItem!
    private let logMenu = NSMenu()
    private var restartItem: NSMenuItem!
    private var startItem: NSMenuItem!
    private var stopItem: NSMenuItem!
    private var loginItem: NSMenuItem!

    func applicationDidFinishLaunching(_ notification: Notification) {
        // 防重复实例：用文件锁（flock）而不是 NSRunningApplication 计数 ——
        // 后者在「launchd 拉起」与「用户双击」几乎同时发生时会各看到自己（竞态），
        // 结果菜单栏出现两个图标，且两个实例各自去拉路由服务 → 双 router 互相拖死。
        let lockPath = appSupportDir + "/.bar.lock"
        barLockFD = open(lockPath, O_CREAT | O_RDWR, 0o644)
        if barLockFD < 0 || flock(barLockFD, LOCK_EX | LOCK_NB) != 0 {
            if barLockFD >= 0 {
                close(barLockFD)
                barLockFD = -1
            }
            NSApp.terminate(nil)
            return
        }

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = "⇄ …"
        menu.autoenablesItems = false
        menu.delegate = self
        statusItem.menu = menu

        buildFixedMenu()        // 菜单结构只建这一次，之后永不重建
        refreshMenuData()       // 填第一版数据

        service.refresh { [weak self] in
            guard let self = self else { return }
            self.refreshMenuData()
            // 打开 App 时若路由没在跑：先给 launchd 的 router agent 几秒（开机时它也在启动），
            // 等几轮仍没有才自己拉起 —— 直接抢着拉会与 agent 形成双实例竞争（会互相拖死）。
            if !self.service.running {
                self.refreshSoon(6.0, retries: 3, autoStartIfDown: true)
            }
        }

        // 统一刷新节奏：60 秒一轮（服务状态 + 燃料 + 积分共用，不再各开一个定时器）
        Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in
            self?.tick()
        }

        // 火山方舟燃料余额：每 60s 读本机路由 /api/fuel（路由内部自己去拉上游，约 300s 一轮）
        FuelMonitor.shared.onUpdate = { [weak self] in self?.refreshMenuData() }
        FuelMonitor.shared.start(interval: 60)

        // WorkBuddy / CodeBuddy 积分：同样每 60s 读本机路由 /api/workbuddy
        WorkbuddyMonitor.shared.onUpdate = { [weak self] in self?.refreshMenuData() }
        WorkbuddyMonitor.shared.start(interval: 60)
    }

    /// 一轮定时刷新：探一次服务状态 → 刷新状态栏标题与菜单里的动态数据
    func tick() {
        service.refresh { [weak self] in
            guard let self = self else { return }
            self.refreshMenuData()
        }
    }

    /// 菜单即将展开：只刷状态文本与可用性（<5ms，零重建），菜单瞬时弹出。
    /// 子菜单内容（燃料/积分/默认模型/最近请求）由 60s 定时器与数据回调负责，最多滞后一分钟。
    func menuWillOpen(_ menu: NSMenu) {
        measure("menuWillOpen(刷状态文本)") { refreshStatus() }
    }

    // 在访达里双击已在运行的 App 时，直接打开配置面板
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        openPanel()
        return true
    }

    /// 状态栏标题：按用户要求保持极简，只有「火山月度占比｜WorkBuddy 余额」——
    /// 形如 `8.9%｜1,045.73`，不带任何图标/emoji/单位说明（跟系统菜单栏图标保持同一种克制风格）。
    /// 某一项暂时没有数据时该位置显示 `—`；完整信息（端口/默认模型/模型数/重置时间）放 tooltip。
    func updateTitle() {
        let cfg = RouterConfig.load()
        let fuel = fuelMonthlyInline()
        let credits = creditsInline()
        statusItem.button?.title = "\(fuel.isEmpty ? "—" : fuel)｜\(credits.isEmpty ? "—" : credits)"

        if service.running {
            var tip = "Cline 路由：运行中 · 端口 \(cfg.port) · 默认 \(cfg.defaultModel.isEmpty ? "未设置" : cfg.defaultModel) · \(cfg.modelIds.count) 个模型"
            if let m = fuelMonthlyWindow() {
                tip += "\n火山月度：\(m.detail)" + (m.resetAt.isEmpty ? "" : " · \(m.resetAt) 重置")
            } else if !fuel.isEmpty {
                tip += "\n火山燃料（最紧窗口）：已用 \(fuel)"
            }
            let creditsBrief = creditsBriefInline()
            if !creditsBrief.isEmpty { tip += "\nWorkBuddy 积分：\(creditsBrief)" }
            statusItem.button?.toolTip = tip
        } else {
            statusItem.button?.toolTip = "Cline 路由：未运行（点此启动）"
        }
    }

    /// 火山「月度」窗口（状态栏按用户偏好只看月度）
    private func fuelMonthlyWindow() -> FuelWindow? {
        guard service.running, let snap = FuelMonitor.shared.snapshot, snap.ok else { return nil }
        return snap.windows.first { $0.key == "monthly" }
    }

    /// 状态栏里的火山指示：月度已用占比（如 "8.9%"）；没有月度窗口数据时退回「最紧窗口」
    private func fuelMonthlyInline() -> String {
        if let m = fuelMonthlyWindow() { return m.brief }
        guard service.running, let snap = FuelMonitor.shared.snapshot, snap.ok,
              let tight = snap.tightest else { return "" }
        return tight.brief
    }

    /// 状态栏里的 WorkBuddy 数值：余额取整（如 "1,046"）——用户要求不显示小数
    private func creditsInline() -> String {
        guard service.running, let snap = WorkbuddyMonitor.shared.snapshot, snap.ok else { return "" }
        return fuelInt(snap.totalRemain)
    }

    /// tooltip 里的完整积分摘要（如 "1,045.73 / 16,642（6.3%）"）
    private func creditsBriefInline() -> String {
        guard service.running, let snap = WorkbuddyMonitor.shared.snapshot, snap.ok else { return "" }
        return snap.brief
    }

    // MARK: 燃料菜单

    private func fuelMenuTitle() -> String {
        if !service.running { return "⛽ 火山燃料：（路由未运行）" }
        guard let snap = FuelMonitor.shared.snapshot else { return "⛽ 火山燃料：查询中…" }
        if !snap.ok { return "⛽ 火山燃料：\(snap.error.isEmpty ? "不可用" : snap.error)" }
        let parts = snap.windows.map { "\($0.shortLabel) \($0.brief)" }
        return "⛽ 火山燃料 " + parts.joined(separator: " · ")
    }

    /// 重填燃料子菜单内容（复用同一个 NSMenu 对象：菜单结构固定，只换内容）
    private func fillFuelMenu() {
        let sub = fuelMenu
        sub.removeAllItems()
        sub.autoenablesItems = false
        let snap = FuelMonitor.shared.snapshot
        func disabled(_ title: String) {
            let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
            item.isEnabled = false
            sub.addItem(item)
        }

        if let snap = snap {
            if !snap.ok {
                disabled("✗ \(snap.error.isEmpty ? "查询失败" : snap.error)")
                sub.addItem(.separator())
            }
            if snap.windows.isEmpty && snap.ok {
                disabled("（无窗口数据）")
            }
            for w in snap.windows {
                disabled("\(w.label)   \(w.detail)")
                disabled("        ↳ \(w.resetAt.isEmpty ? "-" : w.resetAt) 重置\(w.resetIn.isEmpty ? "" : "（\(w.resetIn)后）")")
            }
            sub.addItem(.separator())
            disabled("档位 \(snap.planType.isEmpty ? "-" : snap.planType) · 更新于 \(snap.updatedAt)")
        } else {
            disabled(service.running ? "正在查询…" : "（路由未运行，无法查询）")
        }

        sub.addItem(.separator())
        let now = NSMenuItem(title: "立即刷新燃料", action: #selector(refreshFuel), keyEquivalent: "")
        now.target = self
        sub.addItem(now)
        let cred = NSMenuItem(title: "打开凭据文件（volc-fuel.json）", action: #selector(openFuelCred), keyEquivalent: "")
        cred.target = self
        sub.addItem(cred)
    }

    @objc func refreshFuel() {
        FuelMonitor.shared.fetch()
    }

    @objc func openFuelCred() {
        let candidates = [
            "\(appSupportDir)/volc-fuel.json",
            (configPath as NSString).deletingLastPathComponent + "/volc-fuel.json",
        ]
        for path in candidates where FileManager.default.fileExists(atPath: path) {
            NSWorkspace.shared.open(URL(fileURLWithPath: path))
            return
        }
        NSWorkspace.shared.open(URL(fileURLWithPath: appSupportDir))
    }

    // MARK: 积分菜单（WorkBuddy / CodeBuddy）

    private func creditsMenuTitle() -> String {
        if !service.running { return "⚡ WorkBuddy 积分：（路由未运行）" }
        guard let snap = WorkbuddyMonitor.shared.snapshot else { return "⚡ WorkBuddy 积分：查询中…" }
        if !snap.ok { return "⚡ WorkBuddy 积分：\(snap.error.isEmpty ? "不可用" : snap.error)" }
        var title = "⚡ WorkBuddy 积分 " + snap.brief
        if let c = snap.checkin, !c.today { title += " · 未签" }
        return title
    }

    /// 重填积分子菜单内容（复用同一个 NSMenu 对象）
    private func fillCreditsMenu() {
        let sub = creditsMenu
        sub.removeAllItems()
        sub.autoenablesItems = false
        let snap = WorkbuddyMonitor.shared.snapshot
        func disabled(_ title: String) {
            let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
            item.isEnabled = false
            sub.addItem(item)
        }

        if let snap = snap {
            if !snap.ok {
                disabled("✗ \(snap.error.isEmpty ? "查询失败" : snap.error)")
                sub.addItem(.separator())
            }
            if snap.ok {
                disabled("剩余 \(snap.brief)")
                for p in snap.packages {
                    disabled("      · \(p.name)   \(p.detail)")
                }
                if let c = snap.checkin {
                    var line = "签到：\(c.brief)"
                    if c.week > 0 { line += " · 本周 \(c.week) 天" }
                    disabled(line)
                }
                if let days = snap.tokenDaysLeft, days < 3 {
                    disabled(days <= 0
                             ? "令牌已过期：请打开 WorkBuddy 桌面端重新登录"
                             : String(format: "令牌 %.1f 天后过期：请打开桌面端刷新登录", days))
                }
                sub.addItem(.separator())
                disabled("更新于 \(snap.updatedAt)")
            }
        } else {
            disabled(service.running ? "正在查询…" : "（路由未运行，无法查询）")
        }

        sub.addItem(.separator())
        let now = NSMenuItem(title: "立即刷新积分", action: #selector(refreshCredits), keyEquivalent: "")
        now.target = self
        now.isEnabled = service.running && !WorkbuddyMonitor.shared.busy
        sub.addItem(now)

        let canCheckin = (snap?.ok ?? false) && !(snap?.checkin?.today ?? true)
        let check = NSMenuItem(title: canCheckin ? "每日签到" : "每日签到（今日已签）",
                               action: canCheckin ? #selector(checkinCredits) : nil,
                               keyEquivalent: "")
        check.target = self
        check.isEnabled = canCheckin && !WorkbuddyMonitor.shared.busy
        sub.addItem(check)

        let openState = NSMenuItem(title: "打开 WorkBuddy 状态文件夹", action: #selector(openWorkbuddyState), keyEquivalent: "")
        openState.target = self
        sub.addItem(openState)
    }

    @objc func refreshCredits() {
        WorkbuddyMonitor.shared.refresh { [weak self] in self?.refreshMenuData() }
    }

    @objc func checkinCredits() {
        WorkbuddyMonitor.shared.checkin { [weak self] _ in
            NSSound.beep()
            self?.refreshMenuData()
        }
    }

    @objc func openWorkbuddyState() {
        let dir = (NSHomeDirectory() as NSString).appendingPathComponent(".workbuddy-status")
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        NSWorkspace.shared.open(URL(fileURLWithPath: dir))
    }

    /// 稍后刷新状态。路由器是 onefile 打包，启动要自解压 2~4 秒，所以支持带重试。
    /// autoStartIfDown=true 时：重试都还失败才自己拉起服务（用于 App 启动自愈）——
    /// 先给 launchd 的 router agent 足够时间，避免开机时两个实例抢着启动互相拖死。
    func refreshSoon(_ delay: Double = 1.8, retries: Int = 0, autoStartIfDown: Bool = false) {
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            guard let self = self else { return }
            self.service.refresh {
                self.refreshStatus()
                self.updateTitle()
                if self.service.running { return }
                if retries > 0 {
                    self.refreshSoon(3.0, retries: retries - 1, autoStartIfDown: autoStartIfDown)
                } else if autoStartIfDown {
                    self.service.start()
                    self.refreshSoon(3.0, retries: 2)
                }
            }
        }
    }

    /// 等服务就绪：重启/启动后 onefile 要自解压 15~20 秒才监听端口。
    /// 期间每 1.5 秒刷一次状态与标题，一亮就把图标切回「● 运行中」——用户不必自己再点一次。
    func waitForRunning(tries: Int = 24, interval: Double = 1.5) {
        func tick(_ left: Int) {
            guard left > 0 else { return }
            service.refresh { [weak self] in
                guard let self = self else { return }
                self.refreshStatus()
                self.updateTitle()
                if self.service.running { return }
                DispatchQueue.main.asyncAfter(deadline: .now() + interval) { tick(left - 1) }
            }
        }
        tick(tries)
    }

    /// 等停止完成（bootout + 进程退出一般 <1 秒），最多等 ~6 秒。
    func waitForStopped(tries: Int = 6, interval: Double = 1.0) {
        func tick(_ left: Int) {
            guard left > 0 else { return }
            service.refresh { [weak self] in
                guard let self = self else { return }
                self.refreshStatus()
                self.updateTitle()
                if !self.service.running { return }
                DispatchQueue.main.asyncAfter(deadline: .now() + interval) { tick(left - 1) }
            }
        }
        tick(tries)
    }

    // MARK: 菜单构建

    /// 构建固定菜单：所有菜单项对象只在这里创建一次，之后只由 refreshMenuData() 改文本/可用性。
    private func buildFixedMenu() {
        headItem = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        headItem.isEnabled = false
        menu.addItem(headItem)
        menu.addItem(.separator())

        fuelItem = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        fuelMenu.autoenablesItems = false
        fuelItem.submenu = fuelMenu
        menu.addItem(fuelItem)

        creditsItem = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        creditsMenu.autoenablesItems = false
        creditsItem.submenu = creditsMenu
        menu.addItem(creditsItem)

        add(menu, "打开配置面板…", #selector(openPanel), "o")

        copyItem = NSMenuItem(title: "", action: #selector(copyAPI), keyEquivalent: "")
        copyItem.target = self
        menu.addItem(copyItem)

        // 默认模型子菜单：Cline 里固定填 auto，实际走这里的选中项
        defItem = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        defMenu.autoenablesItems = false
        defItem.submenu = defMenu
        menu.addItem(defItem)

        // 最近请求子菜单
        logItem = NSMenuItem(title: "最近请求", action: nil, keyEquivalent: "")
        logMenu.autoenablesItems = false
        logItem.submenu = logMenu
        menu.addItem(logItem)

        menu.addItem(.separator())

        restartItem = add(menu, "重启路由服务", #selector(restartService), "r")
        startItem = add(menu, "启动路由服务", #selector(startService), "")
        stopItem = add(menu, "停止路由服务", #selector(stopService), "")

        menu.addItem(.separator())

        add(menu, "打开配置文件夹", #selector(openFolder), "")
        loginItem = NSMenuItem(title: "开机自启（路由服务 + 菜单栏图标）",
                               action: #selector(toggleLoginItem), keyEquivalent: "")
        loginItem.target = self
        menu.addItem(loginItem)

        menu.addItem(.separator())
        add(menu, "退出", #selector(quitApp), "q")
    }

    @discardableResult
    private func add(_ menu: NSMenu, _ title: String, _ action: Selector, _ key: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
        item.target = self
        menu.addItem(item)
        return item
    }

    // MARK: 菜单数据刷新（只改内容，不动结构）

    /// 全量刷新：状态行 / 燃料 / 积分 / 默认模型 / 最近请求 / 按钮可用性 / 状态栏标题。
    /// 纯文本与少量子菜单重建，实测 1~3ms。
    func refreshMenuData() {
        refreshStatus()
        fillDefaultMenu(RouterConfig.load())
        fuelItem.title = fuelMenuTitle()
        fillFuelMenu()
        creditsItem.title = creditsMenuTitle()
        fillCreditsMenu()
        fillLogMenu()
        updateTitle()
    }

    /// 只刷「状态相关」的文本与可用性 —— 不重建任何子菜单。
    /// 用在：菜单展开前、服务启停的 1.5s 等待轮询、开机自启开关之后。
    func refreshStatus() {
        let cfg = RouterConfig.load()
        headItem.title = service.running
            ? "● 运行中 · 端口 \(cfg.port) · \(cfg.modelIds.count) 个模型"
            : "○ 未运行 · 端口 \(cfg.port)"
        copyItem.title = "复制 API 地址（http://127.0.0.1:\(cfg.port)/v1）"
        defItem.title = "默认模型（当前：\(cfg.defaultModel.isEmpty ? "未设置" : cfg.defaultModel)）"
        restartItem.isEnabled = true
        startItem.isEnabled = !service.running
        stopItem.isEnabled = service.running
        loginItem.state = loginItemEnabled() ? .on : .off
    }

    private func fillDefaultMenu(_ cfg: RouterConfig) {
        defMenu.removeAllItems()
        if !service.running {
            let off = NSMenuItem(title: "（服务未运行，无法切换）", action: nil, keyEquivalent: "")
            off.isEnabled = false
            defMenu.addItem(off)
        } else if cfg.modelDetails.isEmpty {
            let empty = NSMenuItem(title: "（还没有配置模型）", action: nil, keyEquivalent: "")
            empty.isEnabled = false
            defMenu.addItem(empty)
        } else {
            let hint = NSMenuItem(title: "点选即切换（Cline 的 auto 立即生效）", action: nil, keyEquivalent: "")
            hint.isEnabled = false
            defMenu.addItem(hint)
            defMenu.addItem(.separator())
            for m in cfg.modelDetails {
                let item = NSMenuItem(title: m.id, action: #selector(setDefaultModel(_:)), keyEquivalent: "")
                item.target = self
                item.representedObject = m.id
                item.toolTip = m.model
                if m.id == cfg.defaultModel { item.state = .on }
                defMenu.addItem(item)
            }
        }
    }

    private func fillLogMenu() {
        logMenu.removeAllItems()
        let recent = tailLog(8, onlyRequests: true)
        if recent.isEmpty {
            let empty = NSMenuItem(title: "（暂无请求记录）", action: nil, keyEquivalent: "")
            empty.isEnabled = false
            logMenu.addItem(empty)
        }
        for line in recent {
            let brief = line.count > 72 ? String(line.prefix(72)) + "…" : line
            let item = NSMenuItem(title: brief, action: nil, keyEquivalent: "")
            item.isEnabled = false
            logMenu.addItem(item)
        }
        logMenu.addItem(.separator())
        let openLogItem = NSMenuItem(title: "打开完整日志", action: #selector(openLog), keyEquivalent: "")
        openLogItem.target = self
        logMenu.addItem(openLogItem)
    }

    // MARK: 动作

    @objc func openPanel() {
        let cfg = RouterConfig.load()
        measure("action.openPanel(含拉起浏览器)") {
            if let url = URL(string: "http://127.0.0.1:\(cfg.port)/ui") {
                NSWorkspace.shared.open(url)
            }
        }
    }

    @objc func copyAPI() {
        let cfg = RouterConfig.load()
        copyToClipboard("http://127.0.0.1:\(cfg.port)/v1")
    }

    @objc func setDefaultModel(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String else { return }
        let cfg = RouterConfig.load()
        guard let url = URL(string: "http://127.0.0.1:\(cfg.port)/api/default") else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.timeoutInterval = 5
        req.setValue("1", forHTTPHeaderField: "X-Router-UI")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["id": id])
        URLSession.shared.dataTask(with: req) { data, resp, _ in
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            DispatchQueue.main.async { [weak self] in
                if code == 200 {
                    self?.refreshSoon(0.3)
                } else {
                    NSSound.beep()
                }
            }
        }.resume()
    }

    private func copyToClipboard(_ text: String) {
        let pb = NSPasteboard.general
        pb.clearContents()
        pb.setString(text, forType: .string)
    }

    @objc func openLog() {
        NSWorkspace.shared.open(URL(fileURLWithPath: logPath))
    }

    @objc func openFolder() {
        NSWorkspace.shared.open(URL(fileURLWithPath: appSupportDir))
    }

    @objc func restartService() {
        measure("action.restartService(提交)") { service.restart() }
        waitForRunning()
    }

    @objc func startService() {
        measure("action.startService(提交)") { service.start() }
        waitForRunning()
    }

    @objc func stopService() {
        measure("action.stopService(提交)") { service.stop() }
        waitForStopped()
    }

    @objc func toggleLoginItem() {
        let nowEnabled = loginItemEnabled()
        // setLoginItem 内部有 4~6 次 launchctl 同步调用（实测每次 20~45ms），整体放后台，
        // 否则点这一下主线程会僵住近一秒。
        DispatchQueue.global(qos: .userInitiated).async {
            let ok = setLoginItem(!nowEnabled)
            DispatchQueue.main.async { [weak self] in
                if !ok { NSSound.beep() }
                self?.refreshStatus()
                self?.updateTitle()
            }
        }
    }

    @objc func quitApp() {
        // 关键：先卸载菜单栏 LaunchAgent 再退出。
        // plist 里 KeepAlive=1，不先 bootout 的话进程一退出就被 launchd 立刻拉回来（表现为"关不掉"）。
        // plist 文件保留，所以下次登录仍会按「开机自启」设置自动启动。
        // bootout 是同步阻塞调用：放后台执行，退出会慢一两百毫秒但不再卡主线程。
        DispatchQueue.global(qos: .userInitiated).async {
            if agentLoaded(barLabel) { bootout(barLabel) }
            DispatchQueue.main.async { NSApp.terminate(nil) }
        }
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
