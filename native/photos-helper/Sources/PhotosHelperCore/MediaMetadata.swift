import AVFoundation
import Foundation
import ImageIO
import UniformTypeIdentifiers

public enum MediaMetadataError: Error, LocalizedError {
    case invalidRequest(String)
    case unsupportedFile(String)

    public var errorDescription: String? {
        switch self {
        case .invalidRequest(let message): return message
        case .unsupportedFile(let name): return "Unsupported import file: \(name)"
        }
    }
}

private let rawExtensions: Set<String> = [
    "dng", "cr2", "cr3", "nef", "arw", "raf", "orf", "rw2",
]
private let sidecarExtensions: Set<String> = ["aae", "xmp"]

private func fileKind(url: URL, type: UTType?) throws -> String {
    let ext = url.pathExtension.lowercased()
    if sidecarExtensions.contains(ext) { return "sidecar" }
    if rawExtensions.contains(ext) { return "raw" }
    if type?.conforms(to: .image) == true { return "photo" }
    if type?.conforms(to: .movie) == true || type?.conforms(to: .video) == true {
        return "video"
    }
    throw MediaMetadataError.unsupportedFile(url.lastPathComponent)
}

private func parseImageDate(_ value: String) -> Date? {
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "en_US_POSIX")
    formatter.timeZone = TimeZone.current
    formatter.dateFormat = "yyyy:MM:dd HH:mm:ss"
    return formatter.date(from: value)
}

private func imageMetadata(url: URL) -> (Date?, String?) {
    guard
        let source = CGImageSourceCreateWithURL(url as CFURL, nil),
        let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil)
            as? [String: Any]
    else { return (nil, nil) }
    let exif = properties[kCGImagePropertyExifDictionary as String] as? [String: Any]
    let tiff = properties[kCGImagePropertyTIFFDictionary as String] as? [String: Any]
    let dateText = exif?[kCGImagePropertyExifDateTimeOriginal as String] as? String
        ?? tiff?[kCGImagePropertyTIFFDateTime as String] as? String
    let maker = properties[kCGImagePropertyMakerAppleDictionary as String] as? [String: Any]
    let identifier = maker?["17"] as? String
    return (dateText.flatMap(parseImageDate), identifier)
}

private func videoMetadata(url: URL) async -> (Date?, String?) {
    let asset = AVURLAsset(url: url)
    var created: Date?
    var identifier: String?
    let metadata = (try? await asset.load(.metadata)) ?? []
    for item in metadata {
        if item.commonKey == .commonKeyCreationDate,
           let value = try? await item.load(.dateValue)
        {
            created = value
        }
        if item.identifier?.rawValue == "mdta/com.apple.quicktime.content.identifier" {
            identifier = try? await item.load(.stringValue)
        }
    }
    return (created, identifier)
}

private func iso8601(_ date: Date) -> String {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return formatter.string(from: date)
}

public func inspectMediaFile(payload: [String: JSONValue]) async throws -> [String: JSONValue] {
    guard let path = payload["path"]?.stringValue else {
        throw MediaMetadataError.invalidRequest("inspect-file requires a path")
    }
    let url = URL(fileURLWithPath: path)
    let type = UTType(filenameExtension: url.pathExtension)
    let kind = try fileKind(url: url, type: type)
    let values: (Date?, String?)
    switch kind {
    case "photo", "raw": values = imageMetadata(url: url)
    case "video": values = await videoMetadata(url: url)
    default: values = (nil, nil)
    }
    var result: [String: JSONValue] = [
        "kind": .string(kind),
        "uti": type.map { .string($0.identifier) } ?? .null,
        "creation_at_utc": values.0.map { .string(iso8601($0)) } ?? .null,
        "content_identifier": values.1.map(JSONValue.string) ?? .null,
    ]
    result["name"] = .string(url.lastPathComponent)
    return result
}
