@preconcurrency import CryptoKit
import Foundation
@preconcurrency import Photos

public enum PhotoLibraryAdapterError: Error, LocalizedError {
    case invalidRequest(String)
    case unavailable(String)
    case unauthorized(String)
    case unsafe(String)

    public var errorDescription: String? {
        switch self {
        case .invalidRequest(let message), .unavailable(let message),
             .unauthorized(let message), .unsafe(let message):
            return message
        }
    }
}

private final class ResultBox<Value>: @unchecked Sendable {
    private let lock = NSLock()
    private var stored: Value

    init(_ value: Value) {
        stored = value
    }

    func set(_ value: Value) {
        lock.lock()
        stored = value
        lock.unlock()
    }

    func get() -> Value {
        lock.lock()
        defer { lock.unlock() }
        return stored
    }
}

private struct LibraryResourceDescriptor: Sendable {
    let resourceKey: String
    let resourceType: String
    let resourceTypeCode: Int
    let originalName: String
    let uti: String
    let ordinal: Int

    var payload: [String: JSONValue] {
        [
            "resource_key": .string(resourceKey),
            "resource_type": .string(resourceType),
            "resource_type_code": .integer(resourceTypeCode),
            "original_name": .string(originalName),
            "uti": uti.isEmpty ? .null : .string(uti),
            "ordinal": .integer(ordinal),
        ]
    }
}

public struct PhotoLibraryDiscoveryResponse: Sendable {
    public let assets: [[String: JSONValue]]
    public let result: [String: JSONValue]
}

private let resourceTypeNames: [Int: String] = [
    1: "photo",
    2: "video",
    3: "audio",
    4: "alternate_photo",
    5: "full_size_photo",
    6: "full_size_video",
    7: "adjustment_data",
    8: "adjustment_base_photo",
    9: "paired_video",
    10: "full_size_paired_video",
    11: "adjustment_base_paired_video",
    12: "adjustment_base_video",
]

private func sha256(_ value: String) -> String {
    SHA256.hash(data: Data(value.utf8)).map { String(format: "%02x", $0) }.joined()
}

func photoLibraryResourceKey(
    typeCode: Int,
    originalName: String,
    uti: String,
    ordinal: Int
) -> String {
    sha256("\(typeCode)\u{0}\(originalName.precomposedStringWithCanonicalMapping)\u{0}\(uti)\u{0}\(ordinal)")
}

private func iso8601LibraryDate(_ date: Date) -> String {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return formatter.string(from: date)
}

private func parseLibraryDate(_ value: String) -> Date? {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    if let date = formatter.date(from: value) {
        return date
    }
    formatter.formatOptions = [.withInternetDateTime]
    return formatter.date(from: value)
}

private func authorizationName(_ status: PHAuthorizationStatus) -> String {
    switch status {
    case .notDetermined: "not_determined"
    case .restricted: "restricted"
    case .denied: "denied"
    case .authorized: "authorized"
    case .limited: "limited"
    @unknown default: "unknown"
    }
}

private func mediaTypeName(_ asset: PHAsset) -> String? {
    switch asset.mediaType {
    case .image:
        return asset.mediaSubtypes.contains(.photoLive) ? "live_photo" : "image"
    case .video:
        return "video"
    default:
        return nil
    }
}

private func resourceDescriptors(_ asset: PHAsset) -> [LibraryResourceDescriptor] {
    var occurrences: [String: Int] = [:]
    return PHAssetResource.assetResources(for: asset).map { resource in
        let typeCode = resource.type.rawValue
        let originalName = resource.originalFilename
        let uti = resource.uniformTypeIdentifier
        let signature = "\(typeCode)\u{0}\(originalName.precomposedStringWithCanonicalMapping)\u{0}\(uti)"
        let ordinal = occurrences[signature, default: 0]
        occurrences[signature] = ordinal + 1
        return LibraryResourceDescriptor(
            resourceKey: photoLibraryResourceKey(
                typeCode: typeCode,
                originalName: originalName,
                uti: uti,
                ordinal: ordinal
            ),
            resourceType: resourceTypeNames[typeCode] ?? "resource_\(typeCode)",
            resourceTypeCode: typeCode,
            originalName: originalName,
            uti: uti,
            ordinal: ordinal
        )
    }.sorted { $0.resourceKey < $1.resourceKey }
}

private func fetchOptions() -> PHFetchOptions {
    let options = PHFetchOptions()
    options.includeHiddenAssets = true
    options.includeAllBurstAssets = true
    options.includeAssetSourceTypes = [.typeUserLibrary]
    options.sortDescriptors = [
        NSSortDescriptor(key: "creationDate", ascending: true),
    ]
    return options
}

private struct RequestedAsset {
    let localIdentifier: String
    let resourceKeys: Set<String>
}

private struct ValidatedAssets {
    let assets: [PHAsset]
    let valid: [String]
    let missing: [String]
    let mismatched: [String]
}

public final class PhotoLibrarySession {
    public init() {}

    public func authorize() throws -> String {
        authorizationName(try ensureAuthorization())
    }

    private func ensureAuthorization() throws -> PHAuthorizationStatus {
        var status = PHPhotoLibrary.authorizationStatus(for: .readWrite)
        if status == .notDetermined {
            let box = ResultBox(status)
            let semaphore = DispatchSemaphore(value: 0)
            PHPhotoLibrary.requestAuthorization(for: .readWrite) { requested in
                box.set(requested)
                semaphore.signal()
            }
            while semaphore.wait(timeout: .now() + .milliseconds(100)) == .timedOut {
                _ = RunLoop.current.run(
                    mode: .default,
                    before: Date(timeIntervalSinceNow: 0.1)
                )
            }
            status = box.get()
        }
        guard status == .authorized else {
            throw PhotoLibraryAdapterError.unauthorized(
                "Full Photos read/write access is required (status: \(authorizationName(status)))."
            )
        }
        if let reason = PHPhotoLibrary.shared().unavailabilityReason {
            throw PhotoLibraryAdapterError.unavailable(reason.localizedDescription)
        }
        return status
    }

    public func discover(payload: [String: JSONValue]) throws -> PhotoLibraryDiscoveryResponse {
        guard let cutoffText = payload["cutoff_at_utc"]?.stringValue,
              let cutoff = parseLibraryDate(cutoffText),
              let requestedTypes = payload["media_types"]?.stringArrayValue
        else {
            throw PhotoLibraryAdapterError.invalidRequest(
                "discover requires cutoff_at_utc and media_types"
            )
        }
        let status = try ensureAuthorization()
        let allowedTypes = Set(requestedTypes)
        let fetched = PHAsset.fetchAssets(with: fetchOptions())
        var totalResources = 0
        var skippedMissingDate = 0
        var candidates: [[String: JSONValue]] = []
        candidates.reserveCapacity(fetched.count)
        for index in 0..<fetched.count {
            let asset = fetched.object(at: index)
            let descriptors = resourceDescriptors(asset)
            totalResources += descriptors.count
            guard let mediaType = mediaTypeName(asset), allowedTypes.contains(mediaType) else {
                continue
            }
            guard let creationDate = asset.creationDate else {
                skippedMissingDate += 1
                continue
            }
            guard creationDate < cutoff else { continue }
            candidates.append([
                "local_identifier": .string(asset.localIdentifier),
                "creation_at_utc": .string(iso8601LibraryDate(creationDate)),
                "media_type": .string(mediaType),
                "resources": .array(descriptors.map { .object($0.payload) }),
            ])
        }
        var warnings: [JSONValue] = []
        if skippedMissingDate > 0 {
            warnings.append(.string("MISSING_CREATION_DATE:\(skippedMissingDate)"))
        }
        return PhotoLibraryDiscoveryResponse(
            assets: candidates,
            result: [
                "authorization": .string(authorizationName(status)),
                "total_assets": .integer(fetched.count),
                "total_resources": .integer(totalResources),
                "candidate_assets": .integer(candidates.count),
                "candidate_resources": .integer(candidates.reduce(0) { total, asset in
                    total + (asset["resources"]?.arrayValue?.count ?? 0)
                }),
                "warnings": .array(warnings),
            ]
        )
    }

    public func download(payload: [String: JSONValue]) throws -> [String: JSONValue] {
        _ = try ensureAuthorization()
        guard let localIdentifier = payload["local_identifier"]?.stringValue,
              let resourceKey = payload["resource_key"]?.stringValue,
              let path = payload["path"]?.stringValue,
              !path.isEmpty
        else {
            throw PhotoLibraryAdapterError.invalidRequest(
                "download requires local_identifier, resource_key, and path"
            )
        }
        let fetched = PHAsset.fetchAssets(withLocalIdentifiers: [localIdentifier], options: nil)
        guard fetched.count == 1 else {
            throw PhotoLibraryAdapterError.unavailable("The requested Photos asset is missing.")
        }
        let asset = fetched.object(at: 0)
        let resources = PHAssetResource.assetResources(for: asset)
        let descriptors = resourceDescriptors(asset)
        guard let descriptorIndex = descriptors.firstIndex(where: { $0.resourceKey == resourceKey })
        else {
            throw PhotoLibraryAdapterError.unsafe("The requested Photos resource changed.")
        }
        let descriptor = descriptors[descriptorIndex]
        var occurrence = 0
        var selected: PHAssetResource?
        for resource in resources {
            guard resource.type.rawValue == descriptor.resourceTypeCode,
                  resource.originalFilename == descriptor.originalName,
                  resource.uniformTypeIdentifier == descriptor.uti
            else { continue }
            if occurrence == descriptor.ordinal {
                selected = resource
                break
            }
            occurrence += 1
        }
        guard let selected else {
            throw PhotoLibraryAdapterError.unsafe("The requested Photos resource changed.")
        }
        let destination = URL(fileURLWithPath: path)
        guard !FileManager.default.fileExists(atPath: destination.path) else {
            throw PhotoLibraryAdapterError.unsafe("The download destination already exists.")
        }
        let options = PHAssetResourceRequestOptions()
        options.isNetworkAccessAllowed = true
        let errorBox = ResultBox<Error?>(nil)
        let semaphore = DispatchSemaphore(value: 0)
        PHAssetResourceManager.default().writeData(
            for: selected,
            toFile: destination,
            options: options
        ) { error in
            errorBox.set(error)
            semaphore.signal()
        }
        semaphore.wait()
        if let error = errorBox.get() {
            try? FileManager.default.removeItem(at: destination)
            throw PhotoLibraryAdapterError.unavailable(error.localizedDescription)
        }
        let attributes = try FileManager.default.attributesOfItem(atPath: destination.path)
        guard let size = attributes[.size] as? NSNumber else {
            throw PhotoLibraryAdapterError.unavailable("Photos did not write a regular resource.")
        }
        return ["size": .integer(size.intValue)]
    }

    public func revalidate(payload: [String: JSONValue]) throws -> [String: JSONValue] {
        _ = try ensureAuthorization()
        guard let cutoffText = payload["cutoff_at_utc"]?.stringValue,
              let cutoff = parseLibraryDate(cutoffText),
              let requested = try? requestedAssets(payload)
        else {
            throw PhotoLibraryAdapterError.invalidRequest(
                "revalidate requires cutoff_at_utc and assets"
            )
        }
        let validated = validate(requested, cutoff: cutoff)
        return [
            "valid_local_identifiers": .array(validated.valid.map(JSONValue.string)),
            "missing_local_identifiers": .array(validated.missing.map(JSONValue.string)),
            "mismatched_local_identifiers": .array(validated.mismatched.map(JSONValue.string)),
        ]
    }

    public func delete(payload: [String: JSONValue]) throws -> [String: JSONValue] {
        _ = try ensureAuthorization()
        guard let batchID = payload["batch_id"]?.stringValue, !batchID.isEmpty,
              let planSHA256 = payload["plan_sha256"]?.stringValue,
              planSHA256.count == 64,
              let cutoffText = payload["cutoff_at_utc"]?.stringValue,
              let cutoff = parseLibraryDate(cutoffText),
              let requested = try? requestedAssets(payload),
              !requested.isEmpty
        else {
            throw PhotoLibraryAdapterError.invalidRequest("delete request is incomplete")
        }
        let validated = validate(requested, cutoff: cutoff)
        guard validated.valid.count == requested.count,
              validated.missing.isEmpty,
              validated.mismatched.isEmpty
        else {
            throw PhotoLibraryAdapterError.unsafe(
                "One or more Photos assets changed; the entire deletion was blocked."
            )
        }
        do {
            try PHPhotoLibrary.shared().performChangesAndWait {
                PHAssetChangeRequest.deleteAssets(validated.assets as NSArray)
            }
            return [
                "deleted_local_identifiers": .array(validated.valid.map(JSONValue.string)),
                "failed_local_identifiers": .array([]),
                "delete_method": .string("PhotoKit.deleteAssets"),
            ]
        } catch {
            return [
                "deleted_local_identifiers": .array([]),
                "failed_local_identifiers": .array(requested.map {
                    .string($0.localIdentifier)
                }),
                "failure_reason": .string(error.localizedDescription),
                "delete_method": .string("PhotoKit.deleteAssets"),
            ]
        }
    }

    private func requestedAssets(_ payload: [String: JSONValue]) throws -> [RequestedAsset] {
        guard let values = payload["assets"]?.arrayValue else {
            throw PhotoLibraryAdapterError.invalidRequest("assets must be an array")
        }
        var seen: Set<String> = []
        return try values.map { value in
            guard let object = value.objectValue,
                  let identifier = object["local_identifier"]?.stringValue,
                  !identifier.isEmpty,
                  let keys = object["resource_keys"]?.stringArrayValue,
                  !keys.isEmpty,
                  keys.count == Set(keys).count,
                  seen.insert(identifier).inserted
            else {
                throw PhotoLibraryAdapterError.invalidRequest(
                    "asset identifiers and resource keys must be unique and non-empty"
                )
            }
            return RequestedAsset(localIdentifier: identifier, resourceKeys: Set(keys))
        }
    }

    private func validate(_ requested: [RequestedAsset], cutoff: Date) -> ValidatedAssets {
        let identifiers = requested.map(\.localIdentifier)
        let fetched = PHAsset.fetchAssets(withLocalIdentifiers: identifiers, options: nil)
        var current: [String: PHAsset] = [:]
        for index in 0..<fetched.count {
            let asset = fetched.object(at: index)
            current[asset.localIdentifier] = asset
        }
        var assets: [PHAsset] = []
        var valid: [String] = []
        var missing: [String] = []
        var mismatched: [String] = []
        for item in requested {
            guard let asset = current[item.localIdentifier] else {
                missing.append(item.localIdentifier)
                continue
            }
            let keys = Set(resourceDescriptors(asset).map(\.resourceKey))
            guard let creationDate = asset.creationDate,
                  creationDate < cutoff,
                  keys == item.resourceKeys,
                  mediaTypeName(asset) != nil
            else {
                mismatched.append(item.localIdentifier)
                continue
            }
            assets.append(asset)
            valid.append(item.localIdentifier)
        }
        return ValidatedAssets(
            assets: assets,
            valid: valid,
            missing: missing,
            mismatched: mismatched
        )
    }
}
