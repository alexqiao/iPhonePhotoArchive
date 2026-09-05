// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "PhotosHelper",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "photos-helper", targets: ["photos-helper"]),
        .library(name: "PhotosHelperCore", targets: ["PhotosHelperCore"]),
    ],
    targets: [
        .target(
            name: "PhotosHelperCore",
            linkerSettings: [
                .linkedFramework("AVFoundation"),
                .linkedFramework("AppKit"),
                .linkedFramework("CryptoKit"),
                .linkedFramework("ImageCaptureCore"),
                .linkedFramework("ImageIO"),
                .linkedFramework("Photos"),
                .linkedFramework("UniformTypeIdentifiers"),
            ]
        ),
        .executableTarget(name: "photos-helper", dependencies: ["PhotosHelperCore"]),
        .testTarget(name: "PhotosHelperCoreTests", dependencies: ["PhotosHelperCore"]),
    ]
)
