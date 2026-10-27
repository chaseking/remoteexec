// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "RemoteExecBar",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(name: "RemoteExecBar", path: "Sources/RemoteExecBar"),
    ]
)
