import AppKit
import Foundation

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

// MARK: - 菜单栏应用

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    let service = RouterService()
    let menu = NSMenu()
    var statusItem: NSStatusItem!

    func applicationDidFinishLaunching(_ notification: Notification) {
        // 防重复实例：避免菜单栏出现两个图标
        let instances = NSRunningApplication.runningApplications(withBundleIdentifier: "local.cline-router.bar")
        if instances.count > 1 {
            NSApp.terminate(nil)
            return
        }

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = "⇄ …"
        menu.autoenablesItems = false
        menu.delegate = self
        statusItem.menu = menu

        service.refresh { [weak self] in self?.updateTitle() }
        Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            self?.service.refresh { self?.updateTitle() }
        }

        // 火山方舟燃料余额：独立节奏（30s）读本机路由 /api/fuel，不拖慢路由状态轮询
        FuelMonitor.shared.onUpdate = { [weak self] in self?.updateTitle() }
        FuelMonitor.shared.start(interval: 30)

        // WorkBuddy / CodeBuddy 积分：同样 30s 读本机路由 /api/workbuddy
        WorkbuddyMonitor.shared.onUpdate = { [weak self] in self?.updateTitle() }
        WorkbuddyMonitor.shared.start(interval: 30)
    }

    // 在访达里双击已在运行的 App 时，直接打开配置面板
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        openPanel()
        return true
    }

    func updateTitle() {
        let cfg = RouterConfig.load()
        let fuel = fuelInline()
        let credits = creditsInline()
        if service.running {
            statusItem.button?.title = "⇄ \(cfg.modelIds.count)" + (fuel.isEmpty ? "" : " · ⛽\(fuel)")
            statusItem.button?.toolTip = "Cline 路由：运行中 · 端口 \(cfg.port) · 默认 \(cfg.defaultModel.isEmpty ? "未设置" : cfg.defaultModel) · \(cfg.modelIds.count) 个模型"
                + (fuel.isEmpty ? "" : "\n火山燃料：最紧窗口已用 \(fuel)")
                + (credits.isEmpty ? "" : "\nWorkBuddy 积分：\(credits)")
        } else {
            statusItem.button?.title = "⇄ ⏸"
            statusItem.button?.toolTip = "Cline 路由：未运行（点此启动）"
        }
    }

    /// 状态栏内嵌的燃料指示：取已用比例最高的窗口（如 "5.0%"）
    private func fuelInline() -> String {
        guard service.running, let snap = FuelMonitor.shared.snapshot, snap.ok,
              let tight = snap.tightest else { return "" }
        return tight.brief
    }

    /// tooltip 里的积分摘要（如 "1,186.58 / 16,642（7.1%）"）
    private func creditsInline() -> String {
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

    private func buildFuelMenu() -> NSMenu {
        let sub = NSMenu()
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
        return sub
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

    private func buildCreditsMenu() -> NSMenu {
        let sub = NSMenu()
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
        return sub
    }

    @objc func refreshCredits() {
        WorkbuddyMonitor.shared.refresh { [weak self] in self?.updateTitle() }
    }

    @objc func checkinCredits() {
        WorkbuddyMonitor.shared.checkin { [weak self] _ in
            NSSound.beep()
            self?.updateTitle()
        }
    }

    @objc func openWorkbuddyState() {
        let dir = (NSHomeDirectory() as NSString).appendingPathComponent(".workbuddy-status")
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        NSWorkspace.shared.open(URL(fileURLWithPath: dir))
    }

    func refreshSoon(_ delay: Double = 1.8) {
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            self?.service.refresh { self?.updateTitle() }
        }
    }

    // MARK: 菜单构建

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        let cfg = RouterConfig.load()

        let head = NSMenuItem(title: service.running
                              ? "● 运行中 · 端口 \(cfg.port) · \(cfg.modelIds.count) 个模型"
                              : "○ 未运行 · 端口 \(cfg.port)",
                              action: nil, keyEquivalent: "")
        head.isEnabled = false
        menu.addItem(head)
        menu.addItem(.separator())

        let fuelItem = NSMenuItem(title: fuelMenuTitle(), action: nil, keyEquivalent: "")
        fuelItem.submenu = buildFuelMenu()
        menu.addItem(fuelItem)

        let creditsItem = NSMenuItem(title: creditsMenuTitle(), action: nil, keyEquivalent: "")
        creditsItem.submenu = buildCreditsMenu()
        menu.addItem(creditsItem)

        add(menu, "打开配置面板…", #selector(openPanel), "o")
        add(menu, "复制 API 地址（http://127.0.0.1:\(cfg.port)/v1）", #selector(copyAPI), "")

        // 默认模型子菜单：Cline 里固定填 auto，实际走这里的选中项
        let defItem = NSMenuItem(title: "默认模型（当前：\(cfg.defaultModel.isEmpty ? "未设置" : cfg.defaultModel)）",
                                 action: nil, keyEquivalent: "")
        let defMenu = NSMenu()
        defMenu.autoenablesItems = false
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
        defItem.submenu = defMenu
        menu.addItem(defItem)

        // 模型子菜单
        let modelItem = NSMenuItem(title: "模型（\(cfg.modelDetails.count)）", action: nil, keyEquivalent: "")
        let modelMenu = NSMenu()
        modelMenu.autoenablesItems = false
        let hint = NSMenuItem(title: "点击复制模型 ID", action: nil, keyEquivalent: "")
        hint.isEnabled = false
        modelMenu.addItem(hint)
        modelMenu.addItem(.separator())
        if cfg.modelDetails.isEmpty {
            let empty = NSMenuItem(title: "（还没有配置模型）", action: nil, keyEquivalent: "")
            empty.isEnabled = false
            modelMenu.addItem(empty)
        }
        for m in cfg.modelDetails {
            let title = "\(m.id)  →  \(m.model)".count > 60
                ? String("\(m.id)  →  \(m.model)".prefix(60)) + "…"
                : "\(m.id)  →  \(m.model)"
            let item = NSMenuItem(title: title, action: #selector(copyModelId(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = m.id
            item.toolTip = "复制 \(m.id)"
            modelMenu.addItem(item)
        }
        modelItem.submenu = modelMenu
        menu.addItem(modelItem)

        // 最近请求子菜单
        let logItem = NSMenuItem(title: "最近请求", action: nil, keyEquivalent: "")
        let logMenu = NSMenu()
        logMenu.autoenablesItems = false
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
        logItem.submenu = logMenu
        menu.addItem(logItem)

        menu.addItem(.separator())

        add(menu, "重启路由服务", #selector(restartService), "r")
        if service.running {
            add(menu, "停止路由服务", #selector(stopService), "")
        } else {
            add(menu, "启动路由服务", #selector(startService), "")
        }

        menu.addItem(.separator())

        add(menu, "打开配置文件夹", #selector(openFolder), "")
        let login = NSMenuItem(title: "开机自启（路由服务 + 菜单栏图标）",
                               action: #selector(toggleLoginItem), keyEquivalent: "")
        login.target = self
        login.state = loginItemEnabled() ? .on : .off
        menu.addItem(login)

        menu.addItem(.separator())
        add(menu, "退出", #selector(quitApp), "q")
    }

    private func add(_ menu: NSMenu, _ title: String, _ action: Selector, _ key: String) {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
        item.target = self
        menu.addItem(item)
    }

    // MARK: 动作

    @objc func openPanel() {
        let cfg = RouterConfig.load()
        if let url = URL(string: "http://127.0.0.1:\(cfg.port)/ui") {
            NSWorkspace.shared.open(url)
        }
    }

    @objc func copyAPI() {
        let cfg = RouterConfig.load()
        copyToClipboard("http://127.0.0.1:\(cfg.port)/v1")
    }

    @objc func copyModelId(_ sender: NSMenuItem) {
        if let id = sender.representedObject as? String {
            copyToClipboard(id)
        }
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
        service.restart()
        refreshSoon()
    }

    @objc func startService() {
        service.start()
        refreshSoon()
    }

    @objc func stopService() {
        service.stop()
        refreshSoon(0.5)
    }

    @objc func toggleLoginItem() {
        let nowEnabled = loginItemEnabled()
        if !setLoginItem(!nowEnabled) {
            NSSound.beep()
        }
    }

    @objc func quitApp() {
        NSApp.terminate(nil)
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
