// Forethought Desk: a window onto the desk page (http://127.0.0.1:4848/).
//
// Links route by destination: Linear and the publishing app open as new tabs in this app,
// everything else opens in Chrome's Forethought profile. The app starts the desk server if it is down,
// reloads the desk when the page has been re-rendered, and registers itself to open at login.
import AppKit
import ServiceManagement
@preconcurrency import WebKit

let deskURL = URL(string: "http://127.0.0.1:4848/")!
let skillDir = "~/Projects/work/.claude/skills/ft-desk"
let deskIndex = "~/Projects/work/private/ft-desk/desk/index.html"
let chromeProfile = "Profile 9"  // "6. Forethought"
let safariUA = "Version/18.0 Safari/605.1.15"

func isLinear(_ url: URL) -> Bool {
    guard let h = url.host?.lowercased() else { return false }
    return h == "linear.app" || h.hasSuffix(".linear.app")
}

/// Sites that open as tabs in this app; the key groups a site's hosts, so links within a site stay in its tab.
func siteKey(_ url: URL) -> String? {
    if isLinear(url) { return "Linear" }
    if url.host?.lowercased() == "publish.example.org" { return "Publish" }
    return nil
}

func isDesk(_ url: URL) -> Bool {
    return url.scheme == "http" && (url.host == "127.0.0.1" || url.host == "localhost") && url.port == 4848
}

/// linear://forethought/issue/AS-1 → https://linear.app/forethought/issue/AS-1
func normalise(_ url: URL) -> URL {
    guard url.scheme == "linear" else { return url }
    let rest = url.absoluteString.dropFirst("linear://".count)
    return URL(string: "https://linear.app/" + rest) ?? url
}

func openInChrome(_ url: URL) {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/bin/open")
    p.arguments = ["-na", "Google Chrome", "--args", "--profile-directory=\(chromeProfile)", url.absoluteString]
    try? p.run()
}

final class Tab: NSObject, WKNavigationDelegate, WKUIDelegate, NSWindowDelegate {
    let window: NSWindow
    let web: WKWebView
    let isDeskTab: Bool
    let site: String  // the in-app site this tab shows ("Linear", "Publish"); "Desk" for the desk
    weak var app: AppDelegate?

    init(url: URL, desk: Bool, config: WKWebViewConfiguration, app: AppDelegate) {
        isDeskTab = desk
        site = desk ? "Desk" : (siteKey(normalise(url)) ?? "Linear")
        self.app = app
        web = WKWebView(frame: .zero, configuration: config)
        web.allowsBackForwardNavigationGestures = true
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 860),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        super.init()
        web.navigationDelegate = self
        web.uiDelegate = self
        window.contentView = web
        window.tabbingIdentifier = "desk"
        window.tabbingMode = .preferred
        window.title = site
        window.delegate = self
        window.isReleasedWhenClosed = false
        if desk { window.setFrameAutosaveName("DeskWindow") }
        web.addObserver(self, forKeyPath: "title", options: .new, context: nil)
        web.load(URLRequest(url: url))
    }

    override func observeValue(forKeyPath keyPath: String?, of object: Any?,
                               change: [NSKeyValueChangeKey: Any]?, context: UnsafeMutableRawPointer?) {
        if isDeskTab {
            // The page puts the number of urgent arrivals at the front of its title, "(2) Forethought desk": show it on the Dock icon.
            window.title = "Desk"
            let t = web.title ?? ""
            var badge: String? = nil
            if t.hasPrefix("("), let end = t.firstIndex(of: ")") {
                let n = String(t[t.index(after: t.startIndex)..<end])
                if let count = Int(n), count > 0 { badge = n }
            }
            NSApp.dockTile.badgeLabel = badge
            return
        }
        let t = web.title ?? ""
        window.title = t.isEmpty ? site : t
    }

    // Route a navigation the user started (or a page opened in a new window).
    func route(_ raw: URL) -> Bool /* true = handled elsewhere */ {
        let url = normalise(raw)
        if isDesk(url) { if !isDeskTab { app?.showDesk() }; return !isDeskTab }
        if let key = siteKey(url) {
            if key == site { return false }  // stay in this tab
            app?.openTab(url); return true
        }
        if url.scheme == "http" || url.scheme == "https" { openInChrome(url); return true }
        NSWorkspace.shared.open(url)  // mailto: and other schemes
        return true
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { return decisionHandler(.allow) }
        let mainFrame = action.targetFrame?.isMainFrame ?? true
        if url.scheme == "linear" { _ = route(url); return decisionHandler(.cancel) }
        if !mainFrame || url.scheme == "about" || url.scheme == "blob" || url.scheme == "data" {
            return decisionHandler(.allow)
        }
        if isDeskTab {
            // The desk itself and in-page anchors stay; every other page goes elsewhere.
            if isDesk(url) { return decisionHandler(.allow) }
            _ = route(url)
            return decisionHandler(.cancel)
        }
        // Site tab: only links clicked to other sites leave; sign-in redirects stay.
        if action.navigationType == .linkActivated && siteKey(url) != site {
            _ = route(url)
            return decisionHandler(.cancel)
        }
        decisionHandler(.allow)
    }

    // target=_blank links and window.open()
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        guard let url = action.request.url else { return nil }
        let n = normalise(url)
        if siteKey(n) != nil { app?.openTab(n) } else { _ = route(n) }
        return nil
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation nav: WKNavigation!, withError error: Error) {
        if isDeskTab { app?.recoverDesk() }
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) { webView.reload() }

    // Closing the desk hides the app instead; other tabs close for good.
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        if isDeskTab { NSApp.hide(nil); return false }
        return true
    }

    func windowWillClose(_ notification: Notification) {
        web.removeObserver(self, forKeyPath: "title")
        app?.forget(self)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    var desk: Tab!
    var tabs: [Tab] = []
    let config: WKWebViewConfiguration = {
        let c = WKWebViewConfiguration()
        c.websiteDataStore = .default()
        c.applicationNameForUserAgent = safariUA
        return c
    }()
    var loadedAt = Date.distantPast
    var retrying = false

    func applicationDidFinishLaunching(_ n: Notification) {
        buildMenu()
        // ⌘⌥←/→ switch tabs, as in Chrome. Caught before the web view so no page can take the keys.
        NSEvent.addLocalMonitorForEvents(matching: .keyDown) { e in
            let mods = e.modifierFlags.intersection([.command, .option, .shift, .control])
            guard mods == [.command, .option], e.keyCode == 123 || e.keyCode == 124,
                  let w = NSApp.keyWindow, w.tabbedWindows != nil else { return e }
            if e.keyCode == 123 { w.selectPreviousTab(nil) } else { w.selectNextTab(nil) }
            return nil
        }
        ensureServer { [self] in
            desk = Tab(url: deskURL, desk: true, config: config, app: self)
            if !desk.window.setFrameUsingName("DeskWindow") { desk.window.center() }
            desk.window.makeKeyAndOrderFront(nil)
            loadedAt = Date()
        }
        if SMAppService.mainApp.status != .enabled {
            do { try SMAppService.mainApp.register() } catch { NSLog("login item: \(error)") }
        }
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        showDesk()
        return false
    }

    // Reload the desk when a desk run has re-rendered the page since it was loaded.
    func applicationDidBecomeActive(_ n: Notification) {
        guard let desk = desk,
              let m = try? FileManager.default.attributesOfItem(atPath: deskIndex)[.modificationDate] as? Date,
              m > loadedAt else { return }
        loadedAt = Date()
        desk.web.load(URLRequest(url: deskURL))
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { false }

    func showDesk() {
        guard let desk = desk else { return }
        if !desk.window.isVisible { desk.window.makeKeyAndOrderFront(nil) }
        desk.window.tabGroup?.selectedWindow = desk.window
        desk.window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func openTab(_ url: URL) {
        let t = Tab(url: url, desk: false, config: config, app: self)
        tabs.append(t)
        desk.window.addTabbedWindow(t.window, ordered: .above)
        t.window.makeKeyAndOrderFront(nil)
    }

    func forget(_ t: Tab) { tabs.removeAll { $0 === t } }

    func recoverDesk() {
        guard !retrying else { return }
        retrying = true
        ensureServer { [self] in
            DispatchQueue.main.asyncAfter(deadline: .now() + 1) {
                self.retrying = false
                self.desk.web.load(URLRequest(url: deskURL))
            }
        }
    }

    /// Start the desk server unless it is already listening, then call back on the main queue.
    func ensureServer(_ done: @escaping () -> Void) {
        DispatchQueue.global().async {
            let p = Process()
            let py = FileManager.default.fileExists(atPath: "/opt/homebrew/bin/python3")
                ? "/opt/homebrew/bin/python3" : "/usr/bin/python3"
            p.executableURL = URL(fileURLWithPath: py)
            p.arguments = [skillDir + "/scripts/desk_server.py", "ensure"]
            var env = ProcessInfo.processInfo.environment
            let home = NSHomeDirectory()
            env["PATH"] = "\(home)/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
            p.environment = env
            p.standardOutput = FileHandle.nullDevice
            p.standardError = FileHandle.nullDevice
            do { try p.run(); p.waitUntilExit() } catch { NSLog("desk server: \(error)") }
            DispatchQueue.main.async(execute: done)
        }
    }

    @objc func reloadTab(_ s: Any?) { current()?.web.reload() }
    @objc func back(_ s: Any?) { current()?.web.goBack() }
    @objc func forward(_ s: Any?) { current()?.web.goForward() }
    @objc func showDeskAction(_ s: Any?) { showDesk() }
    @objc func openInBrowser(_ s: Any?) {
        if let u = current()?.web.url { openInChrome(u) }
    }
    @objc func toggleLogin(_ s: NSMenuItem) {
        let svc = SMAppService.mainApp
        do { if svc.status == .enabled { try svc.unregister() } else { try svc.register() } }
        catch { NSLog("login item: \(error)") }
    }

    func current() -> Tab? {
        let w = NSApp.keyWindow
        if desk?.window === w { return desk }
        return tabs.first { $0.window === w }
    }

    func buildMenu() {
        let main = NSMenu()
        func sub(_ title: String, _ items: [NSMenuItem]) -> NSMenu {
            let top = NSMenuItem(); let m = NSMenu(title: title)
            items.forEach { m.addItem($0) }
            top.submenu = m; main.addItem(top); return m
        }
        func item(_ t: String, _ a: Selector?, _ k: String, _ mods: NSEvent.ModifierFlags = .command) -> NSMenuItem {
            let i = NSMenuItem(title: t, action: a, keyEquivalent: k)
            i.keyEquivalentModifierMask = mods
            return i
        }
        let login = item("Open at Login", #selector(toggleLogin(_:)), "")
        _ = sub("Forethought Desk", [
            login,
            .separator(),
            item("Hide Forethought Desk", #selector(NSApplication.hide(_:)), "h"),
            item("Quit Forethought Desk", #selector(NSApplication.terminate(_:)), "q"),
        ])
        _ = sub("Edit", [
            item("Undo", Selector(("undo:")), "z"),
            item("Redo", Selector(("redo:")), "z", [.command, .shift]),
            .separator(),
            item("Cut", #selector(NSText.cut(_:)), "x"),
            item("Copy", #selector(NSText.copy(_:)), "c"),
            item("Paste", #selector(NSText.paste(_:)), "v"),
            item("Select All", #selector(NSText.selectAll(_:)), "a"),
        ])
        _ = sub("View", [
            item("Reload", #selector(reloadTab(_:)), "r"),
            item("Back", #selector(back(_:)), "["),
            item("Forward", #selector(forward(_:)), "]"),
            .separator(),
            item("Open in Chrome", #selector(openInBrowser(_:)), "o", [.command, .shift]),
        ])
        let win = sub("Window", [
            item("Desk", #selector(showDeskAction(_:)), "1"),
            item("Show Previous Tab", #selector(NSWindow.selectPreviousTab(_:)), arrowLeft, [.command, .option]),
            item("Show Next Tab", #selector(NSWindow.selectNextTab(_:)), arrowRight, [.command, .option]),
            .separator(),
            item("Close Tab", #selector(NSWindow.performClose(_:)), "w"),
        ])
        NSApp.mainMenu = main
        NSApp.windowsMenu = win
        loginItem = login
    }
    var loginItem: NSMenuItem?
}

extension AppDelegate: NSMenuItemValidation {
    func validateMenuItem(_ m: NSMenuItem) -> Bool {
        if m.action == #selector(toggleLogin(_:)) {
            m.state = SMAppService.mainApp.status == .enabled ? .on : .off
        }
        return true
    }
}

let arrowLeft = String(Character(UnicodeScalar(NSLeftArrowFunctionKey)!))
let arrowRight = String(Character(UnicodeScalar(NSRightArrowFunctionKey)!))

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
