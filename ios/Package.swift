// swift-tools-version:5.9
// Builds and tests the on-phone processing engine (HAVen/Engine) without Xcode, e.g. on Linux:
//   cd ios && swift test
// The app itself is built with Xcode (HAVen.xcodeproj), which compiles the same Engine files.
import PackageDescription

let package = Package(
    name: "HAVenEngine",
    targets: [
        .target(name: "HAVenEngine", path: "HAVen/Engine"),
        .testTarget(name: "EngineTests", dependencies: ["HAVenEngine"], path: "EngineTests",
                    exclude: ["make_fixtures.py"], resources: [.copy("Fixtures")]),
    ]
)
