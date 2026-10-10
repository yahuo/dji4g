// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "DJISMS",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "DJISMS",
            path: "Sources/DJISMS",
            swiftSettings: [.swiftLanguageMode(.v6)]
        ),
    ]
)
