// Vault.app launcher: starts the bundled Python server, opens the browser, stops the server on quit.
//
// Layout inside Vault.app/Contents/Resources:
//   app/       the release (serve.py, index.html, data/catalog.db, ...)
//   python/    a standalone CPython (bin/python3)
//   bin/       ffmpeg, ffprobe
// Writable data (config, cache, logs) lives in ~/Library/Application Support/Vault.
import Cocoa

let fm = FileManager.default
let res = Bundle.main.resourceURL!
let appDir = res.appendingPathComponent("app")
let python = res.appendingPathComponent("python/bin/python3")
let binDir = res.appendingPathComponent("bin")
// VAULT_DATA overrides the data folder (testing a build next to an installed Vault)
let dataDir = ProcessInfo.processInfo.environment["VAULT_DATA"].map { URL(fileURLWithPath: $0) }
    ?? fm.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/Vault")
let configURL = dataDir.appendingPathComponent("config.json")
let logURL = dataDir.appendingPathComponent("server.log")

func readConfig() -> [String: Any] {
    guard let d = try? Data(contentsOf: configURL),
          let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else { return [:] }
    return o
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    var server: Process?
    var port = 8730
    var ready = false
    var quitting = false

    var url: URL { URL(string: "http://127.0.0.1:\(port)/")! }

    func applicationDidFinishLaunching(_ n: Notification) {
        buildMenu()
        try? fm.createDirectory(at: dataDir, withIntermediateDirectories: true)
        let cfg = readConfig()
        if let p = cfg["port"] as? Int { port = p }
        answers { up in
            if up {                       // already running (a login service, another copy): just show it
                self.ready = true
                self.openBrowser()
                return
            }
            var library = cfg["library"] as? String ?? ""
            var isDir: ObjCBool = false
            if library.isEmpty || !fm.fileExists(atPath: library, isDirectory: &isDir) || !isDir.boolValue {
                guard let chosen = self.chooseLibrary(first: library.isEmpty) else { NSApp.terminate(nil); return }
                library = chosen
            }
            self.start(library: library)
        }
    }

    // MARK: server

    func start(library: String) {
        let p = Process()
        p.executableURL = python
        p.arguments = ["serve.py", "--library", library, "--port", String(port)]
        p.currentDirectoryURL = appDir
        var env = ProcessInfo.processInfo.environment
        env["VAULT_DATA"] = dataDir.path
        env["PATH"] = binDir.path + ":" + (env["PATH"] ?? "/usr/bin:/bin")
        env["PYTHONDONTWRITEBYTECODE"] = "1"          // the bundle stays untouched
        p.environment = env
        fm.createFile(atPath: logURL.path, contents: nil)
        if let h = try? FileHandle(forWritingTo: logURL) {
            p.standardOutput = h
            p.standardError = h
        }
        p.terminationHandler = { proc in
            DispatchQueue.main.async { self.serverStopped(proc.terminationStatus) }
        }
        do { try p.run() } catch {
            alert("Vault could not start", "\(error.localizedDescription)")
            NSApp.terminate(nil)
            return
        }
        server = p
        waitUntilUp(tries: 600)                        // the first start scans the whole library
    }

    func waitUntilUp(tries: Int) {
        answers { up in
            if up {
                self.ready = true
                self.openBrowser()
            } else if tries > 0, self.server?.isRunning == true {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { self.waitUntilUp(tries: tries - 1) }
            }
        }
    }

    func serverStopped(_ status: Int32) {
        server = nil
        if quitting { return }
        let tail = (try? String(contentsOf: logURL, encoding: .utf8))
            .map { $0.split(separator: "\n").suffix(8).joined(separator: "\n") } ?? ""
        if tail.contains("no valid library") {
            if let lib = chooseLibrary(first: false) { start(library: lib); return }
        } else {
            alert("Vault stopped", tail.isEmpty ? "The server ended (code \(status))." : tail)
        }
        NSApp.terminate(nil)
    }

    func answers(_ done: @escaping (Bool) -> Void) {
        var r = URLRequest(url: url)
        r.timeoutInterval = 2
        URLSession.shared.dataTask(with: r) { _, resp, _ in
            let up = (resp as? HTTPURLResponse) != nil
            DispatchQueue.main.async { done(up) }
        }.resume()
    }

    func applicationWillTerminate(_ n: Notification) {
        quitting = true
        guard let p = server, p.isRunning else { return }
        p.terminate()                                  // SIGTERM: Vault saves its state and exits
        let deadline = Date().addingTimeInterval(5)
        while p.isRunning && Date() < deadline { usleep(100_000) }
        if p.isRunning { kill(p.processIdentifier, SIGKILL) }
    }

    func applicationShouldHandleReopen(_ s: NSApplication, hasVisibleWindows: Bool) -> Bool {
        if ready { openBrowser() }                     // clicking the Dock icon shows Vault again
        return false
    }

    // MARK: UI

    @objc func openBrowser() { NSWorkspace.shared.open(url) }

    @objc func changeLibrary() {
        guard let lib = chooseLibrary(first: false) else { return }
        var r = URLRequest(url: url.appendingPathComponent("api/setlibrary"))
        r.httpMethod = "POST"
        r.setValue("application/json", forHTTPHeaderField: "Content-Type")
        r.httpBody = try? JSONSerialization.data(withJSONObject: ["path": lib])
        URLSession.shared.dataTask(with: r).resume()
    }

    @objc func showLog() { NSWorkspace.shared.activateFileViewerSelecting([logURL]) }

    func chooseLibrary(first: Bool) -> String? {
        NSApp.activate(ignoringOtherApps: true)
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.prompt = "Use This Folder"
        panel.message = first ? "Welcome to Vault. Choose the folder that holds your movies."
                              : "Choose the folder that holds your movies."
        return panel.runModal() == .OK ? panel.url?.path : nil
    }

    func alert(_ title: String, _ text: String) {
        NSApp.activate(ignoringOtherApps: true)
        let a = NSAlert()
        a.messageText = title
        a.informativeText = text
        a.runModal()
    }

    func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        main.addItem(appItem)
        let m = NSMenu()
        m.addItem(withTitle: "Open Vault in Browser", action: #selector(openBrowser), keyEquivalent: "o")
        m.addItem(withTitle: "Choose Library Folder…", action: #selector(changeLibrary), keyEquivalent: "")
        m.addItem(withTitle: "Show Server Log", action: #selector(showLog), keyEquivalent: "")
        m.addItem(.separator())
        m.addItem(withTitle: "Quit Vault", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = m
        NSApp.mainMenu = main
        let dock = NSMenu()
        dock.addItem(withTitle: "Open Vault in Browser", action: #selector(openBrowser), keyEquivalent: "")
        dockMenu = dock
    }

    var dockMenu: NSMenu?
    func applicationDockMenu(_ s: NSApplication) -> NSMenu? { dockMenu }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
