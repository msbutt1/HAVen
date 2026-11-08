// swift-tools-version:5.9
// Builds and tests the on-phone processing engine (VibeGuard/Engine) without Xcode, e.g. on Linux:
//   cd ios && swift test
// The app itself is built with Xcode (VibeGuard.xcodeproj), which compiles the same Engine files.
import PackageDescription

let package = Package(
    name: "VibeGuardEngine",
    targets: [
        .target(name: "VibeGuardEngine", path: "VibeGuard/Engine"),
        .testTarget(name: "EngineTests", dependencies: ["VibeGuardEngine"], path: "EngineTests",
                    exclude: ["make_fixtures.py"], resources: [.copy("Fixtures")]),
    ]
)
