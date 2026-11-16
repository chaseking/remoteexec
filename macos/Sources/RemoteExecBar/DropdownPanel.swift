import AppKit
import SwiftUI

/// The dropdown under the menu bar icon.
///
/// A borderless, non-activating panel that never becomes the key window, the way the
/// system's own menu bar extras behave, rather than an NSPopover.
///
/// - An NSPopover from a menu bar app only takes clicks once the app is activated, and
///   activating it takes focus from the app you were in (its window greys out, then comes
///   back when the dropdown closes). On recent macOS activation is also cooperative and
///   can lag by up to a second.
/// - Making a non-activating panel key instead leaves AppKit thinking the app is active
///   while another app is frontmost. Clicks on the menu bar icon then reach the app up to
///   ~750 ms late, so closing the dropdown felt slow.
///
/// Clicks in the panel work without it being key. Closing (outside clicks, the icon,
/// Escape while the app is active) is handled in AppDelegate.
final class DropdownPanel: NSPanel {
    private let hosting: NSHostingController<AnyView>
    private let effect = NSVisualEffectView()
    private var sizeObservation: NSKeyValueObservation?
    private static let cornerRadius: CGFloat = 10

    init<Content: View>(rootView: Content) {
        hosting = NSHostingController(rootView: AnyView(rootView))
        super.init(contentRect: NSRect(x: 0, y: 0, width: 380, height: 120),
                   styleMask: [.borderless, .nonactivatingPanel],
                   backing: .buffered, defer: false)
        isFloatingPanel = true
        level = .statusBar  // over normal windows, under the menus it opens (the host switcher)
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .transient, .ignoresCycle]
        hidesOnDeactivate = false  // the app is usually not active
        becomesKeyOnlyIfNeeded = true
        isReleasedWhenClosed = false
        isMovable = false
        animationBehavior = .none
        backgroundColor = .clear
        isOpaque = false
        hasShadow = true

        effect.material = .popover
        effect.blendingMode = .behindWindow
        effect.state = .active  // looks the same whether or not the app is active
        effect.maskImage = Self.roundedMask(radius: Self.cornerRadius)
        effect.autoresizingMask = [.width, .height]
        contentView = effect

        // Sized by hand from SwiftUI's measurement, not by constraints, so it stays
        // pinned under the menu bar as its content grows and shrinks.
        hosting.sizingOptions = [.preferredContentSize]
        hosting.view.autoresizingMask = [.width, .height]
        hosting.view.frame = effect.bounds
        effect.addSubview(hosting.view)
        sizeObservation = hosting.observe(\.preferredContentSize) { [weak self] controller, _ in
            let size = controller.preferredContentSize
            MainActor.assumeIsolated { self?.fit(size) }
        }
    }

    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }

    /// Where the panel's top-left goes: under the status item, kept on screen.
    private var anchor: NSPoint?

    func show(below button: NSStatusBarButton) {
        guard let buttonWindow = button.window else { return }
        let rect = buttonWindow.convertToScreen(button.convert(button.bounds, to: nil))
        let screen = buttonWindow.screen ?? NSScreen.main
        anchor = NSPoint(x: rect.minX, y: rect.minY - 5)
        // Measure now, so it opens at the right size rather than resizing a frame later.
        let size = hosting.sizeThatFits(in: NSSize(width: 10_000, height: 10_000))
        place(size: size.width > 0 && size.height > 0 ? NSSize(width: ceil(size.width), height: ceil(size.height)) : frame.size,
              on: screen)
        orderFrontRegardless()
    }

    private func fit(_ size: CGSize) {
        guard size.width > 0, size.height > 0 else { return }
        place(size: NSSize(width: ceil(size.width), height: ceil(size.height)), on: screen ?? NSScreen.main)
    }

    private func place(size: NSSize, on screen: NSScreen?) {
        var origin = NSPoint(x: frame.minX, y: frame.maxY - size.height)
        if let anchor {
            origin = NSPoint(x: anchor.x, y: anchor.y - size.height)
            if let visible = screen?.visibleFrame {
                origin.x = min(max(origin.x, visible.minX + 8), visible.maxX - size.width - 8)
            }
        }
        let target = NSRect(origin: origin, size: size)
        if target != frame {
            setFrame(target, display: true)
            invalidateShadow()
        }
    }

    private static func roundedMask(radius: CGFloat) -> NSImage {
        let edge = radius * 2 + 1
        let image = NSImage(size: NSSize(width: edge, height: edge), flipped: false) { rect in
            NSColor.black.setFill()
            NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius).fill()
            return true
        }
        image.capInsets = NSEdgeInsets(top: radius, left: radius, bottom: radius, right: radius)
        image.resizingMode = .stretch
        return image
    }
}
