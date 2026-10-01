// Menubar front-end for `tng`. All data work happens in the Python CLI;
// this app runs `tng menubar <day>` every minute and renders the JSON.

import Charts
import ServiceManagement
import SwiftUI

// MARK: - Payload

struct Workspace: Decodable, Identifiable {
    let key: String
    let name: String
    let path: String
    let seconds: Int
    let sources: [String: Int]
    let sessions: Int
    let last: Int?
    var id: String { key }
}

struct WorkspaceSlice: Decodable {
    let key: String
    let name: String
    let seconds: Int
}

struct DayTotal: Decodable, Identifiable {
    let date: String
    let total_seconds: Int
    let workspaces: [WorkspaceSlice]
    var id: String { date }
}

struct DayPayload: Decodable {
    let date: String
    let total_seconds: Int
    let workspaces: [Workspace]
}

struct WeekPayload: Decodable {
    let days: [DayTotal]
    let total_seconds: Int
    let workspaces: [Workspace]
}

struct Payload: Decodable {
    let generated_at: Int
    let today: DayPayload
    let week: WeekPayload
}

// MARK: - Model

@MainActor
final class Tracker: ObservableObject {
    @Published var payload: Payload?
    @Published var error: String?
    @Published var dayOffset = 0 { didSet { Task { await refresh() } } }
    @Published var todayTotal = 0

    private let binary = Tracker.findBinary()

    init() {
        Task {
            while true {
                await refresh()
                try? await Task.sleep(for: .seconds(60))
            }
        }
    }

    static func findBinary() -> String? {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let candidates = [
            ProcessInfo.processInfo.environment["TNG_PATH"],
            "\(home)/.local/bin/tng",
            "/opt/homebrew/bin/tng",
            "/usr/local/bin/tng",
        ]
        return candidates.compactMap { $0 }.first { FileManager.default.isExecutableFile(atPath: $0) }
    }

    var anchorDay: Date {
        Calendar.current.date(byAdding: .day, value: dayOffset, to: Date())!
    }

    func refresh() async {
        guard let binary else {
            error = "`tng` not found. Run `uv tool install -e .` in the project folder."
            return
        }
        let day = ISO8601DateFormatter.string(
            from: anchorDay, timeZone: .current, formatOptions: [.withFullDate])
        let offset = dayOffset
        do {
            let data = try await Task.detached { try Self.run(binary, ["menubar", day]) }.value
            let decoded = try JSONDecoder().decode(Payload.self, from: data)
            guard offset == dayOffset else { return }  // user navigated meanwhile
            payload = decoded
            if offset == 0 { todayTotal = decoded.today.total_seconds }
            error = nil
        } catch {
            self.error = "\(error)"
        }
    }

    nonisolated static func run(_ binary: String, _ args: [String]) throws -> Data {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: binary)
        proc.arguments = args
        let out = Pipe()
        proc.standardOutput = out
        proc.standardError = Pipe()
        try proc.run()
        let data = out.fileHandleForReading.readDataToEndOfFile()
        proc.waitUntilExit()
        if proc.terminationStatus != 0 {
            throw NSError(domain: "tng", code: Int(proc.terminationStatus),
                          userInfo: [NSLocalizedDescriptionKey: "tng exited with \(proc.terminationStatus)"])
        }
        return data
    }
}

// MARK: - Formatting

func fmt(_ seconds: Int) -> String {
    let minutes = Int((Double(seconds) / 60).rounded())
    let (h, m) = (minutes / 60, minutes % 60)
    return h > 0 ? String(format: "%dh %02dm", h, m) : "\(m)m"
}

let sourceColors: [String: Color] = [
    "Desktop": .orange, "VS Code": .blue, "CLI": .green, "Cowork": .purple,
]

func color(for source: String) -> Color { sourceColors[source] ?? .gray }

/// Distinct colours for the busiest workspaces of the week; the rest share "Other".
let workspacePalette: [Color] = [.blue, .orange, .green, .pink, .purple, .teal, .yellow, .indigo]
let otherName = "Other"

struct WorkspaceColors {
    let byKey: [String: Color]
    let names: [String]  // chart series in palette order, plus "Other" if needed
    let colors: [Color]

    init(ranked: [Workspace]) {
        let top = ranked.prefix(workspacePalette.count)
        byKey = Dictionary(uniqueKeysWithValues: top.enumerated().map { ($1.key, workspacePalette[$0]) })
        var names = top.map(\.name)
        var colors = Array(workspacePalette.prefix(top.count))
        if ranked.count > top.count {
            names.append(otherName)
            colors.append(.gray)
        }
        self.names = names
        self.colors = colors
    }

    func color(_ key: String) -> Color { byKey[key] ?? .gray }
    func series(_ slice: WorkspaceSlice) -> String { byKey[slice.key] != nil ? slice.name : otherName }
}

// MARK: - Views

enum Range: String, CaseIterable { case day = "Day", week = "7 days" }

struct WorkspaceRow: View {
    let ws: Workspace
    let maxSeconds: Int
    /// nil = split the bar by source (day view); otherwise one colour per workspace.
    var workspaceColor: Color? = nil

    var segments: [(String, Int, Color)] {
        if let workspaceColor { return [(ws.key, ws.seconds, workspaceColor)] }
        return ws.sources.sorted { $0.value > $1.value }.map { ($0.key, $0.value, color(for: $0.key)) }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline) {
                if let workspaceColor {
                    Circle().fill(workspaceColor).frame(width: 8, height: 8)
                }
                Text(ws.name).font(.system(.body, weight: .medium)).lineLimit(1)
                Spacer()
                Text(fmt(ws.seconds)).font(.system(.body, design: .rounded)).monospacedDigit()
            }
            GeometryReader { geo in
                let width = geo.size.width * CGFloat(ws.seconds) / CGFloat(max(maxSeconds, 1))
                HStack(spacing: 0) {
                    ForEach(segments, id: \.0) { _, secs, fill in
                        Rectangle().fill(fill)
                            .frame(width: width * CGFloat(secs) / CGFloat(max(ws.seconds, 1)))
                    }
                }
                .clipShape(RoundedRectangle(cornerRadius: 3))
            }
            .frame(height: 6)
            HStack(spacing: 6) {
                if !ws.path.isEmpty {
                    Text(ws.path).lineLimit(1).truncationMode(.head)
                }
                Spacer()
                Text("\(ws.sessions) session\(ws.sessions == 1 ? "" : "s")")
            }
            .font(.caption).foregroundStyle(.secondary)
        }
        .padding(.vertical, 4)
        .help(ws.sources.sorted { $0.value > $1.value }.map { "\($0.key): \(fmt($0.value))" }
            .joined(separator: "\n"))
    }
}

struct WeekChart: View {
    let days: [DayTotal]
    let colors: WorkspaceColors

    var body: some View {
        Chart {
            ForEach(days) { day in
                ForEach(day.workspaces, id: \.key) { slice in
                    BarMark(
                        x: .value("Day", dayLabel(day.date)),
                        y: .value("Hours", Double(slice.seconds) / 3600)
                    )
                    .foregroundStyle(by: .value("Workspace", colors.series(slice)))
                }
            }
        }
        .chartForegroundStyleScale(domain: colors.names, range: colors.colors)
        .chartLegend(.hidden)
        .chartXScale(domain: days.map { dayLabel($0.date) })
        .chartYAxis {
            AxisMarks(position: .leading) { value in
                AxisGridLine()
                AxisValueLabel { if let h = value.as(Double.self) { Text("\(Int(h))h") } }
            }
        }
        .frame(height: 150)
    }

    func dayLabel(_ iso: String) -> String {
        let parser = DateFormatter()
        parser.dateFormat = "yyyy-MM-dd"
        guard let date = parser.date(from: iso) else { return iso }
        let out = DateFormatter()
        out.dateFormat = "EEE d"
        return out.string(from: date)
    }
}

struct PopoverView: View {
    @ObservedObject var tracker: Tracker
    @State private var range: Range = .day
    @State private var launchAtLogin = SMAppService.mainApp.status == .enabled

    var dayTitle: String {
        switch tracker.dayOffset {
        case 0: return "Today"
        case -1: return "Yesterday"
        default: return tracker.anchorDay.formatted(.dateTime.weekday(.wide).day().month())
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Button { tracker.dayOffset -= 1 } label: { Image(systemName: "chevron.left") }
                    .buttonStyle(.borderless)
                Text(range == .day ? dayTitle : "7 days to \(dayTitle.lowercased())")
                    .font(.headline)
                Button { tracker.dayOffset += 1 } label: { Image(systemName: "chevron.right") }
                    .buttonStyle(.borderless).disabled(tracker.dayOffset >= 0)
                Spacer()
                Picker("", selection: $range) {
                    ForEach(Range.allCases, id: \.self) { Text($0.rawValue) }
                }
                .pickerStyle(.segmented).labelsHidden().frame(width: 130)
            }

            if let error = tracker.error {
                Text(error).font(.callout).foregroundStyle(.red).textSelection(.enabled)
            }

            if let p = tracker.payload {
                let total = range == .day ? p.today.total_seconds : p.week.total_seconds
                let list = range == .day ? p.today.workspaces : p.week.workspaces
                Text(fmt(total)).font(.system(size: 30, weight: .semibold, design: .rounded))
                    + Text("  with Claude").font(.callout).foregroundColor(.secondary)

                let colors = WorkspaceColors(ranked: p.week.workspaces)
                if range == .week { WeekChart(days: p.week.days, colors: colors) }

                Divider()
                if list.isEmpty {
                    Text("No Claude activity.").foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, minHeight: 60)
                } else {
                    ScrollView {
                        VStack(spacing: 6) {
                            ForEach(list) {
                                WorkspaceRow(ws: $0, maxSeconds: list[0].seconds,
                                             workspaceColor: range == .week ? colors.color($0.key) : nil)
                            }
                        }
                        .padding(.trailing, 8)
                    }
                    // MenuBarExtra windows size to ideal content height, which collapses
                    // a ScrollView; give it an explicit height instead.
                    .frame(height: min(CGFloat(list.count) * 62, range == .week ? 380 : 520))
                }
                if range == .day { Legend() }
            } else if tracker.error == nil {
                ProgressView().frame(maxWidth: .infinity, minHeight: 80)
            }

            Divider()
            HStack {
                Toggle("Open at login", isOn: $launchAtLogin)
                    .toggleStyle(.checkbox).font(.caption)
                    .onChange(of: launchAtLogin) { _, on in
                        do {
                            if on { try SMAppService.mainApp.register() }
                            else { try SMAppService.mainApp.unregister() }
                        } catch { launchAtLogin = SMAppService.mainApp.status == .enabled }
                    }
                Spacer()
                Button("Refresh") { Task { await tracker.refresh() } }
                Button("Quit") { NSApp.terminate(nil) }
            }
            .controlSize(.small)
        }
        .padding(14)
        .frame(width: 420)
    }
}

struct Legend: View {
    var body: some View {
        HStack(spacing: 12) {
            ForEach(["Desktop", "VS Code", "CLI", "Cowork"], id: \.self) { src in
                HStack(spacing: 4) {
                    Circle().fill(color(for: src)).frame(width: 7, height: 7)
                    Text(src)
                }
            }
        }
        .font(.caption2).foregroundStyle(.secondary)
    }
}

@main
struct TimeNotGoneApp: App {
    @StateObject private var tracker = Tracker()

    var body: some Scene {
        MenuBarExtra {
            PopoverView(tracker: tracker)
        } label: {
            Image(systemName: "hourglass")
            Text(fmt(tracker.todayTotal))
        }
        .menuBarExtraStyle(.window)
    }
}
