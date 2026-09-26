import Foundation

// MARK: - WorkBuddy / CodeBuddy 积分
//
// 数据来自本机路由的只读端点 /api/workbuddy（路由内部每 300s 拉一次
// copilot.tencent.com 的积分接口，菜单栏每 30s 读一次内存快照）。
// 登录令牌由路由自动发现（~/.workbuddy-status/config.json 或桌面端 auth/*.info）。

struct CreditPackage {
    let name: String
    let total: Double
    let remain: Double

    /// "1,044.58 / 7,500"
    var detail: String { "\(fuelNum(remain)) / \(fuelNum(total))" }
}

struct WorkbuddyCheckin {
    let today: Bool
    let streak: Int
    let week: Int
    let daily: Double

    /// "今日已签 · 连续 7 天"
    var brief: String {
        if today { return "今日已签 · 连续 \(streak) 天" }
        return daily > 0 ? "今日未签（每日 \(fuelNum(daily)) 分）" : "今日未签"
    }
}

struct WorkbuddySnapshot {
    let ok: Bool
    let error: String
    let updatedAt: String
    let totalRemain: Double
    let totalCapacity: Double
    let ratio: Double
    let packages: [CreditPackage]
    let checkin: WorkbuddyCheckin?
    let tokenExpiresAt: Double

    /// "1,186.58 / 16,642（7.1%）"
    var brief: String {
        "\(fuelNum(totalRemain)) / \(fuelNum(totalCapacity))（\(String(format: "%.1f%%", ratio * 100))）"
    }

    /// 令牌剩余天数（过期前 3 天需要在菜单里提示重新登录桌面端）
    var tokenDaysLeft: Double? {
        guard tokenExpiresAt > 0 else { return nil }
        return (tokenExpiresAt - Date().timeIntervalSince1970) / 86400.0
    }
}

extension WorkbuddySnapshot {
    init(json obj: [String: Any]) {
        ok = (obj["ok"] as? Bool) ?? false
        error = (obj["error"] as? String) ?? ""
        updatedAt = (obj["updated_at_text"] as? String) ?? ""
        totalRemain = fuelDbl(obj["total_remain"])
        totalCapacity = fuelDbl(obj["total_capacity"])
        ratio = fuelDbl(obj["ratio"])
        tokenExpiresAt = fuelDbl(obj["token_expires_at"])
        var list: [CreditPackage] = []
        for item in (obj["packages"] as? [[String: Any]]) ?? [] {
            list.append(CreditPackage(
                name: (item["name"] as? String) ?? "积分包",
                total: fuelDbl(item["total"]),
                remain: fuelDbl(item["remain"])
            ))
        }
        packages = list.filter { $0.total > 0 || $0.remain > 0 }
        if let c = obj["checkin"] as? [String: Any] {
            checkin = WorkbuddyCheckin(
                today: (c["today"] as? Bool) ?? false,
                streak: Int(fuelDbl(c["streak"])),
                week: Int(fuelDbl(c["week"])),
                daily: fuelDbl(c["daily"])
            )
        } else {
            checkin = nil
        }
    }
}

final class WorkbuddyMonitor {
    static let shared = WorkbuddyMonitor()

    private(set) var snapshot: WorkbuddySnapshot?
    private(set) var busy = false          // 正在刷新/签到（防重复点击）
    var onUpdate: (() -> Void)?
    private var timer: Timer?

    func start(interval: TimeInterval = 30) {
        fetch()
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: interval, repeats: true) { [weak self] _ in
            self?.fetch()
        }
    }

    func fetch() {
        let port = RouterConfig.load().port
        guard let url = URL(string: "http://127.0.0.1:\(port)/api/workbuddy") else { return }
        var req = URLRequest(url: url)
        req.timeoutInterval = 6
        URLSession.shared.dataTask(with: req) { [weak self] data, _, _ in
            guard let data = data,
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
            let snap = WorkbuddySnapshot(json: obj)
            DispatchQueue.main.async {
                self?.snapshot = snap
                self?.onUpdate?()
            }
        }.resume()
    }

    /// 立即刷新（让路由去上游拉一次，约 1~3 秒）
    func refresh(completion: (() -> Void)? = nil) {
        guard !busy else { return }
        busy = true
        post("/api/workbuddy/refresh") { [weak self] _ in
            self?.busy = false
            self?.fetch()
            completion?()
        }
    }

    /// 每日签到（幂等：已签到会返回「今天已签到」），随后自动刷新余额
    func checkin(completion: ((String) -> Void)?) {
        guard !busy else { return }
        busy = true
        post("/api/workbuddy/checkin") { [weak self] obj in
            self?.busy = false
            let checkin = obj?["checkin"] as? [String: Any]
            let message = (checkin?["message"] as? String) ?? (obj == nil ? "签到失败（路由未响应）" : "签到完成")
            self?.fetch()
            completion?(message)
        }
    }

    private func post(_ path: String, completion: (([String: Any]?) -> Void)?) {
        let port = RouterConfig.load().port
        guard let url = URL(string: "http://127.0.0.1:\(port)\(path)") else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.timeoutInterval = 20
        req.setValue("1", forHTTPHeaderField: "X-Router-UI")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = "{}".data(using: .utf8)
        URLSession.shared.dataTask(with: req) { data, _, _ in
            var obj: [String: Any]?
            if let data = data {
                obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            }
            DispatchQueue.main.async { completion?(obj) }
        }.resume()
    }
}
