import Foundation

// MARK: - 火山方舟 Agent Plan 燃料（AFP）余额
//
// 数据来自本机路由的只读端点 /api/fuel（路由内部每 300s 拉一次火山 OpenAPI，
// 菜单栏每 30s 读一次内存快照），凭据文件 volc-fuel.json。

struct FuelWindow {
    let key: String
    let label: String
    let shortLabel: String
    let quota: Double
    let used: Double
    let remaining: Double
    let percent: Double
    let resetAt: String
    let resetIn: String

    /// "4.1%"
    var brief: String { String(format: "%.1f%%", percent) }

    /// "剩余 9,588 / 10,000  已用 4.1%"
    var detail: String {
        "剩余 \(fuelNum(remaining)) / \(fuelNum(quota))  已用 \(brief)"
    }
}

struct FuelSnapshot {
    let ok: Bool
    let error: String
    let planType: String
    let updatedAt: String
    let windows: [FuelWindow]

    /// 紧要窗口（已用比例最高）——菜单栏标题用它做一眼可见的指示
    var tightest: FuelWindow? {
        windows.max { $0.percent < $1.percent }
    }
}

extension FuelSnapshot {
    init(json obj: [String: Any]) {
        ok = (obj["ok"] as? Bool) ?? false
        error = (obj["error"] as? String) ?? ""
        planType = (obj["plan_type"] as? String) ?? ""
        updatedAt = (obj["updated_at"] as? String) ?? ""
        let raw = (obj["windows"] as? [String: Any]) ?? [:]
        let order: [(String, String, String)] = [
            ("five_hour", "5 小时", "5h"),
            ("daily", "近一天", "日"),
            ("weekly", "近一周", "周"),
            ("monthly", "近一月", "月"),
        ]
        var list: [FuelWindow] = []
        for (key, fallbackLabel, short) in order {
            guard let w = raw[key] as? [String: Any] else { continue }
            let quota = fuelDbl(w["quota"])
            let used = fuelDbl(w["used"])
            let remaining = w["remaining"] == nil ? max(quota - used, 0) : fuelDbl(w["remaining"])
            list.append(FuelWindow(
                key: key,
                label: (w["label"] as? String) ?? fallbackLabel,
                shortLabel: short,
                quota: quota,
                used: used,
                remaining: remaining,
                percent: w["percent"] == nil ? (quota > 0 ? used / quota * 100 : 0) : fuelDbl(w["percent"]),
                resetAt: (w["reset_at"] as? String) ?? "",
                resetIn: (w["reset_in"] as? String) ?? ""
            ))
        }
        windows = list
    }
}

func fuelDbl(_ any: Any?) -> Double {
    if let n = any as? NSNumber { return n.doubleValue }
    if let s = any as? String { return Double(s) ?? 0 }
    return 0
}

// 两个静态 formatter 复用：NumberFormatter 创建开销大，而菜单里会连续格式化十几个数字。
// 仅在主线程（菜单构建 / 状态栏刷新）使用，无需加锁。
private let fuelIntFormatter: NumberFormatter = {
    let f = NumberFormatter()
    f.numberStyle = .decimal
    f.maximumFractionDigits = 0
    return f
}()

private let fuelDecFormatter: NumberFormatter = {
    let f = NumberFormatter()
    f.numberStyle = .decimal
    f.minimumFractionDigits = 2
    f.maximumFractionDigits = 2
    return f
}()

/// 大数带千分位、整数不带小数；非整数保留两位
func fuelNum(_ value: Double) -> String {
    let f = abs(value - value.rounded()) < 0.005 ? fuelIntFormatter : fuelDecFormatter
    return f.string(from: NSNumber(value: value)) ?? String(format: "%.2f", value)
}

final class FuelMonitor {
    static let shared = FuelMonitor()

    private(set) var snapshot: FuelSnapshot?
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
        guard let url = URL(string: "http://127.0.0.1:\(port)/api/fuel") else { return }
        var req = URLRequest(url: url)
        req.timeoutInterval = 6
        URLSession.shared.dataTask(with: req) { [weak self] data, _, _ in
            guard let data = data,
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
            let snap = FuelSnapshot(json: obj)
            DispatchQueue.main.async {
                self?.snapshot = snap
                self?.onUpdate?()
            }
        }.resume()
    }
}
