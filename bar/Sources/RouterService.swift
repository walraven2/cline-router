import Darwin
import Foundation

// MARK: - 路径与常量
//
// 路径策略（.app 版）：
//   - 「数据目录」（models.json / images/ / 日志 / pid）统一在
//     ~/Library/Application Support/ClineRouter/，与 router.py --data-dir 完全对齐。
//   - 「router 二进制」永远随 .app 走：Bundle.main/Contents/MacOS/router。
//   - 「配置模板」在 .app/Contents/Resources/models.json.template，首次启动拷过去。
//   - 老的 ~/Cline 路由/ 目录只作为「一次性迁移源」（有 models.json 就搬过来，免得重配密钥）。

let homeDir = NSHomeDirectory()
let appSupportDir = homeDir + "/Library/Application Support/ClineRouter"
let configPath = appSupportDir + "/models.json"
let logPath = appSupportDir + "/router.log"
// launchd 抓的裸 stderr：刻意与 router.log 分开，否则「launchd 重定向」与「log() 自己追加」
// 会同时写同一个文件，出现重复行甚至互相覆盖（router.log 里的结构化日志由 log() 负责）
let stderrLogPath = appSupportDir + "/router.stderr.log"
let barLogPath = appSupportDir + "/bar.log"
let pidPath = appSupportDir + "/router.pid"
let imagesDir = appSupportDir + "/images"
let agentsDir = homeDir + "/Library/LaunchAgents"
let routerAgentPath = agentsDir + "/com.wangcheng.cline-router.plist"
let barAgentPath = agentsDir + "/com.wangcheng.cline-router-bar.plist"
let routerLabel = "com.wangcheng.cline-router"
let barLabel = "com.wangcheng.cline-router-bar"

// router 二进制：永远在 .app 内
let routerBin = Bundle.main.bundlePath + "/Contents/MacOS/router"
// 配置模板（构建时由 bar/build.sh 拷进 Resources）
let templatePath = Bundle.main.bundlePath + "/Contents/Resources/models.json.template"

// 旧源码路径（仅用于一次性迁移）
let legacySourceDir = homeDir + "/Cline 路由"
let legacyConfigPath = legacySourceDir + "/models.json"

// MARK: - 进程与 launchd

@discardableResult
func runProcess(_ path: String, _ args: [String]) -> Int32 {
    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: path)
    proc.arguments = args
    proc.standardOutput = Pipe()
    proc.standardError = Pipe()
    do {
        try proc.run()
    } catch {
        return -1
    }
    proc.waitUntilExit()
    return proc.terminationStatus
}

func agentLoaded(_ label: String) -> Bool {
    return runProcess("/bin/launchctl", ["print", "gui/\(getuid())/\(label)"]) == 0
}

func bootstrap(_ path: String) {
    _ = runProcess("/bin/launchctl", ["bootstrap", "gui/\(getuid())", path])
}

func bootout(_ label: String) {
    _ = runProcess("/bin/launchctl", ["bootout", "gui/\(getuid())/\(label)"])
}

func kickstart(_ label: String, restart: Bool = false) {
    var args = ["kickstart"]
    if restart { args.append("-k") }
    args.append("gui/\(getuid())/\(label)")
    _ = runProcess("/bin/launchctl", args)
}

/// 以追加方式打开日志文件（不存在就建），给子进程当 stdout/stderr
func openAppendHandle(_ path: String) -> FileHandle? {
    let fm = FileManager.default
    if !fm.fileExists(atPath: path) {
        fm.createFile(atPath: path, contents: nil)
    }
    guard let h = FileHandle(forWritingAtPath: path) else { return nil }
    h.seekToEndOfFile()
    return h
}

// MARK: - 目录与配置准备

/// 确保 AppSupport 目录存在；首次启动时把配置搬/建出来。
/// 优先级：旧源码目录的 models.json（用户已配好密钥） > .app 内的脱敏模板 > 什么都不做（等 router 报错提示）
/// 目录只需建一次（此前每次读配置都做两次 createDirectory —— 5 秒轮询下纯属浪费）
private var dirsReady = false

func ensureAppSupportReady() {
    let fm = FileManager.default
    if !dirsReady {
        dirsReady = true
        try? fm.createDirectory(atPath: appSupportDir, withIntermediateDirectories: true)
        try? fm.createDirectory(atPath: imagesDir, withIntermediateDirectories: true)
    }

    guard !fm.fileExists(atPath: configPath) else { return }

    if fm.fileExists(atPath: legacyConfigPath) {
        try? fm.copyItem(atPath: legacyConfigPath, toPath: configPath)
    } else if fm.fileExists(atPath: templatePath) {
        try? fm.copyItem(atPath: templatePath, toPath: configPath)
    }
}

// MARK: - 配置读取

struct RouterConfig {
    var port = 4000
    var authKey = ""
    var defaultModel = ""
    var modelIds: [String] = []
    var modelDetails: [(id: String, upstream: String, model: String)] = []

    // 配置几乎不变：按 mtime+size 缓存，避免「5 秒轮询 + 两个 30 秒监控 + 每次开菜单」都读盘并解析 JSON
    private static let cacheLock = NSLock()
    private static var cached: (stamp: String, cfg: RouterConfig)?

    static func load() -> RouterConfig {
        ensureAppSupportReady()
        let attrs = try? FileManager.default.attributesOfItem(atPath: configPath)
        let mtime = (attrs?[.modificationDate] as? Date)?.timeIntervalSince1970 ?? 0
        let size = (attrs?[.size] as? Int) ?? -1
        let stamp = "\(mtime)-\(size)"

        cacheLock.lock()
        defer { cacheLock.unlock() }
        if let hit = cached, hit.stamp == stamp { return hit.cfg }

        var cfg = RouterConfig()
        guard let data = FileManager.default.contents(atPath: configPath),
              let json = try? JSONSerialization.jsonObject(with: data),
              let obj = json as? [String: Any] else {
            cached = (stamp, cfg)
            return cfg
        }
        if let p = obj["port"] as? Int { cfg.port = p }
        if let k = obj["auth_key"] as? String { cfg.authKey = k }
        if let dm = obj["default_model"] as? String { cfg.defaultModel = dm }
        if let models = obj["models"] as? [[String: Any]] {
            for m in models {
                guard let id = m["id"] as? String else { continue }
                cfg.modelIds.append(id)
                cfg.modelDetails.append((id: id,
                                         upstream: (m["upstream"] as? String) ?? "",
                                         model: (m["model"] as? String) ?? ""))
            }
        }
        cached = (stamp, cfg)
        return cfg
    }
}

// MARK: - 日志尾部

/// 只读文件尾部固定字节数。
/// 旧实现每次开菜单都把整个日志读进内存再切分 —— 日志随时间无限增长，菜单会越用越卡。
func tailLog(_ lines: Int, onlyRequests: Bool) -> [String] {
    guard let fh = FileHandle(forReadingAtPath: logPath) else { return [] }
    defer { try? fh.close() }
    let maxBytes: UInt64 = 16 * 1024
    let size = (try? fh.seekToEnd()) ?? 0
    let offset = size > maxBytes ? size - maxBytes : 0
    do {
        try fh.seek(toOffset: offset)
    } catch {
        return []
    }
    guard let data = try? fh.readToEnd(), !data.isEmpty else { return [] }
    var text = String(decoding: data, as: UTF8.self)
    if offset > 0, let firstBreak = text.firstIndex(of: "\n") {
        text = String(text[text.index(after: firstBreak)...])   // 首行多半被从中间切断，丢弃
    }
    var rows = text.split(separator: "\n").map(String.init)
    if onlyRequests {
        rows = rows.filter { $0.contains(" OK ") || $0.contains(" FAIL ") || $0.contains(" CANCEL ") }
    }
    return Array(rows.suffix(lines))
}

// MARK: - 健康检查

func parseHealth(_ data: Data?) -> (Bool, [String]) {
    guard let d = data,
          let json = try? JSONSerialization.jsonObject(with: d),
          let obj = json as? [String: Any] else { return (false, []) }
    let ok = (obj["ok"] as? Bool) ?? false
    let models = (obj["models"] as? [String]) ?? []
    return (ok, models)
}

func syncHealth(port: Int) -> (Bool, [String]) {
    guard let url = URL(string: "http://127.0.0.1:\(port)/health") else { return (false, []) }
    var req = URLRequest(url: url)
    req.timeoutInterval = 3
    var result: (Bool, [String]) = (false, [])
    let sem = DispatchSemaphore(value: 0)
    URLSession.shared.dataTask(with: req) { data, _, _ in
        result = parseHealth(data)
        sem.signal()
    }.resume()
    _ = sem.wait(timeout: .now() + 5)
    return result
}

// MARK: - 路由服务控制

final class RouterService {
    private(set) var running = false
    private(set) var modelCount = 0

    func refresh(completion: (() -> Void)? = nil) {
        let cfg = RouterConfig.load()
        guard let url = URL(string: "http://127.0.0.1:\(cfg.port)/health") else { return }
        var req = URLRequest(url: url)
        req.timeoutInterval = 3
        URLSession.shared.dataTask(with: req) { [weak self] data, _, _ in
            let (ok, models) = parseHealth(data)
            DispatchQueue.main.async {
                self?.running = ok
                self?.modelCount = models.count
                completion?()
            }
        }.resume()
    }

    /// 启动路由：装了开机自启就走 launchd（有 KeepAlive 托底），否则直接拉 .app 内二进制并记 pid。
    /// launchctl / 端口探测都是同步阻塞调用，统一放后台线程，避免卡住菜单主线程（最坏会卡 3~5 秒）。
    func start() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            self?.startSync()
        }
    }

    private func startSync() {
        ensureAppSupportReady()

        if agentLoaded(routerLabel) {
            kickstart(routerLabel)
            return
        }
        // 已经在跑就别起第二个（避免端口冲突）
        let cfg = RouterConfig.load()
        if syncHealth(port: cfg.port).0 { return }

        guard FileManager.default.fileExists(atPath: routerBin) else { return }

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: routerBin)
        proc.arguments = ["--data-dir", appSupportDir]
        proc.currentDirectoryURL = URL(fileURLWithPath: appSupportDir)
        // 路由器是 onefile 打包的：启动时要自解压，2~4 秒后才真正监听端口（菜单会先显示未运行，稍后自动刷新）
        proc.standardOutput = openAppendHandle(logPath)
        proc.standardError = openAppendHandle(logPath)
        do {
            try proc.run()
            try? "\(proc.processIdentifier)".write(toFile: pidPath, atomically: true, encoding: .utf8)
        } catch {
            try? "启动失败：\(error)\n".data(using: .utf8)?.write(to: URL(fileURLWithPath: logPath))
        }
    }

    /// 停止路由。launchctl print + bootout 都是同步阻塞（实测 print ~30ms、bootout 更久），
    /// 一律放后台线程，避免点菜单后 UI 僵住（与 start() 保持一致）。
    func stop() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            self?.stopSync()
        }
    }

    private func stopSync() {
        if agentLoaded(routerLabel) {
            bootout(routerLabel)
        }
        // 清掉「直接拉起」的那个进程（launchd 那条已被 bootout 带走）
        if let s = try? String(contentsOfFile: pidPath, encoding: .utf8),
           let pid = Int32(s.trimmingCharacters(in: .whitespacesAndNewlines)),
           pid > 1 {
            kill(pid, SIGTERM)
        }
        try? FileManager.default.removeItem(atPath: pidPath)
    }

    /// 重启路由。全程在后台线程串行执行（print → kickstart / stop → sleep → start），
    /// 主线程只负责收到完成回调后刷新界面。
    func restart() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self = self else { return }
            if agentLoaded(routerLabel) {
                kickstart(routerLabel, restart: true)
                return
            }
            self.stopSync()
            // 等旧进程释放端口（0.6s）后再拉起
            Thread.sleep(forTimeInterval: 0.6)
            self.startSync()
        }
    }
}

// MARK: - 开机自启（两个 LaunchAgent 一起管）
//
//   - barAgent   ：拉 .app 内的 ClineRouterBar（菜单栏 App 自己）
//   - routerAgent：拉 .app 内的 router 二进制，参数 --data-dir <AppSupport>
//
// 每次都整份重写：这样从「源码模式」切到「.app 模式」时，plist 里指向
// /usr/bin/python3 <旧路径>/router.py 的旧内容会被自动更新到 .app 内的二进制。

func plistTemplate(label: String, program: String, args: [String], workDir: String, log: String) -> String {
    var argLines = "        <string>\(program)</string>\n"
    for a in args {
        argLines += "        <string>\(a)</string>\n"
    }
    let xml = """
    <?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
    <plist version="1.0">
    <dict>
        <key>Label</key>
        <string>\(label)</string>
        <key>ProgramArguments</key>
        <array>
    \(argLines)    </array>
        <key>WorkingDirectory</key>
        <string>\(workDir)</string>
        <key>RunAtLoad</key>
        <true/>
        <key>KeepAlive</key>
        <true/>
        <key>ProcessType</key>
        <string>Background</string>
        <key>StandardOutPath</key>
        <string>\(log)</string>
        <key>StandardErrorPath</key>
        <string>\(log)</string>
    </dict>
    </plist>
    """
    return xml
}

func loginItemEnabled() -> Bool {
    let fm = FileManager.default
    return fm.fileExists(atPath: routerAgentPath) && fm.fileExists(atPath: barAgentPath)
}

@discardableResult
func setLoginItem(_ enabled: Bool) -> Bool {
    let fm = FileManager.default
    if enabled {
        ensureAppSupportReady()
        let barExe = Bundle.main.bundlePath + "/Contents/MacOS/ClineRouterBar"

        // 先卸旧的，避免 bootstrap 到内容已变的 plist
        if agentLoaded(barLabel) { bootout(barLabel) }
        if agentLoaded(routerLabel) { bootout(routerLabel) }

        let barXML = plistTemplate(label: barLabel, program: barExe, args: [],
                                   workDir: appSupportDir, log: barLogPath)
        let routerXML = plistTemplate(label: routerLabel, program: routerBin,
                                      args: ["--data-dir", appSupportDir],
                                      workDir: appSupportDir, log: stderrLogPath)
        do {
            try barXML.write(toFile: barAgentPath, atomically: true, encoding: .utf8)
            try routerXML.write(toFile: routerAgentPath, atomically: true, encoding: .utf8)
        } catch {
            return false
        }
        bootstrap(routerAgentPath)
        bootstrap(barAgentPath)
        return true
    } else {
        if agentLoaded(barLabel) { bootout(barLabel) }
        if agentLoaded(routerLabel) { bootout(routerLabel) }
        try? fm.removeItem(atPath: barAgentPath)
        try? fm.removeItem(atPath: routerAgentPath)
        return true
    }
}
