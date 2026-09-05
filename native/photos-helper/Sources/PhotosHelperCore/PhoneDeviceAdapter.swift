@preconcurrency import CryptoKit
@preconcurrency import ImageCaptureCore
import Foundation

public enum PhoneAdapterError: Error, LocalizedError {
    case invalidRequest(String)
    case unavailable(String)
    case unsafe(String)

    public var errorDescription: String? {
        switch self {
        case .invalidRequest(let message), .unavailable(let message), .unsafe(let message):
            return message
        }
    }
}

private func digest(_ value: String) -> String {
    SHA256.hash(data: Data(value.utf8)).map { String(format: "%02x", $0) }.joined()
}

let phoneDevicePasscodeLockedErrorCode = -9943

func isRetryablePhoneOpenError(_ error: Error) -> Bool {
    (error as NSError).code == phoneDevicePasscodeLockedErrorCode
}

func supportsPerItemDeletion(
    capabilities: [String],
    productKind: String?,
    hasOpenSession: Bool,
    accessRestricted: Bool
) -> Bool {
    if capabilities.contains(ICDeviceCapability.cameraDeviceCanDeleteOneFile.rawValue) {
        return true
    }
    // Some iPhones omit the delete capability even when Image Capture enables
    // per-item deletion. Limit the fallback to a trusted, open iPhone PTP session.
    return productKind?.caseInsensitiveCompare("iPhone") == .orderedSame
        && capabilities.contains(ICDeviceCapability.cameraDeviceCanAcceptPTPCommands.rawValue)
        && hasOpenSession
        && !accessRestricted
}

private func iso8601(_ date: Date?) -> JSONValue {
    guard let date else { return .null }
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return .string(formatter.string(from: date))
}

private func ptpTimestampKey(_ value: String) -> String? {
    guard value.count >= 15 else { return nil }
    let key = String(value.prefix(15))
    let digits = key.enumerated().allSatisfy { index, character in
        index == 8 ? character == "T" : character.isNumber
    }
    return digits ? key : nil
}

private func ptpTimestampKeys(
    _ date: Date,
    timeZones: [TimeZone] = [TimeZone.current, TimeZone(secondsFromGMT: 0)!]
) -> Set<String> {
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "en_US_POSIX")
    formatter.calendar = Calendar(identifier: .gregorian)
    formatter.dateFormat = "yyyyMMdd'T'HHmmss"
    return Set(timeZones.map { timeZone in
        formatter.timeZone = timeZone
        return formatter.string(from: date)
    })
}

private func appendUInt16LE(_ value: UInt16, to data: inout Data) {
    data.append(UInt8(truncatingIfNeeded: value))
    data.append(UInt8(truncatingIfNeeded: value >> 8))
}

private func appendUInt32LE(_ value: UInt32, to data: inout Data) {
    for shift in stride(from: 0, through: 24, by: 8) {
        data.append(UInt8(truncatingIfNeeded: value >> UInt32(shift)))
    }
}

func readUInt16LE(_ data: Data, at offset: Int) -> UInt16? {
    guard offset >= 0, offset + 2 <= data.count else { return nil }
    return UInt16(data[offset]) | (UInt16(data[offset + 1]) << 8)
}

func readUInt32LE(_ data: Data, at offset: Int) -> UInt32? {
    guard offset >= 0, offset + 4 <= data.count else { return nil }
    return (0..<4).reduce(UInt32(0)) { value, index in
        value | (UInt32(data[offset + index]) << UInt32(index * 8))
    }
}

func readUInt64LE(_ data: Data, at offset: Int) -> UInt64? {
    guard offset >= 0, offset + 8 <= data.count else { return nil }
    return (0..<8).reduce(UInt64(0)) { value, index in
        value | (UInt64(data[offset + index]) << UInt64(index * 8))
    }
}

func ptpUInt32Array(_ data: Data) -> [UInt32]? {
    guard let count = readUInt32LE(data, at: 0),
          count <= UInt32((data.count - 4) / 4),
          data.count == 4 + Int(count) * 4
    else { return nil }
    return (0..<Int(count)).compactMap { index in
        readUInt32LE(data, at: 4 + index * 4)
    }
}

struct PTPObjectInfo: Equatable {
    let storageID: UInt32
    let objectFormat: UInt16
    let protectionStatus: UInt16
    let objectSize: UInt32
    let parentObject: UInt32
    let associationType: UInt16
    let filename: String
    let captureDate: String
    let modificationDate: String
}

private func readPTPString(_ data: Data, offset: inout Int) -> String? {
    guard offset < data.count else { return nil }
    let count = Int(data[offset])
    offset += 1
    guard count > 0 else { return "" }
    guard count <= (data.count - offset) / 2 else { return nil }
    var codeUnits: [UInt16] = []
    codeUnits.reserveCapacity(count - 1)
    for index in 0..<count {
        guard let codeUnit = readUInt16LE(data, at: offset + index * 2) else { return nil }
        if index == count - 1 {
            guard codeUnit == 0 else { return nil }
        } else {
            codeUnits.append(codeUnit)
        }
    }
    offset += count * 2
    return String(decoding: codeUnits, as: UTF16.self)
}

private func readPTPUInt16Array(_ data: Data, offset: inout Int) -> [UInt16]? {
    guard let count = readUInt32LE(data, at: offset) else { return nil }
    offset += 4
    guard count <= UInt32((data.count - offset) / 2) else { return nil }
    let values = (0..<Int(count)).compactMap { index in
        readUInt16LE(data, at: offset + index * 2)
    }
    guard values.count == Int(count) else { return nil }
    offset += Int(count) * 2
    return values
}

func ptpSupportedOperations(_ data: Data) -> [UInt16]? {
    guard data.count >= 8 else { return nil }
    var offset = 8
    guard readPTPString(data, offset: &offset) != nil,
          readUInt16LE(data, at: offset) != nil
    else { return nil }
    offset += 2
    return readPTPUInt16Array(data, offset: &offset)
}

func ptpObjectInfo(_ data: Data) -> PTPObjectInfo? {
    guard data.count >= 52,
          let storageID = readUInt32LE(data, at: 0),
          let objectFormat = readUInt16LE(data, at: 4),
          let protectionStatus = readUInt16LE(data, at: 6),
          let objectSize = readUInt32LE(data, at: 8),
          let parentObject = readUInt32LE(data, at: 38),
          let associationType = readUInt16LE(data, at: 42)
    else { return nil }
    var offset = 52
    guard let filename = readPTPString(data, offset: &offset),
          let captureDate = readPTPString(data, offset: &offset),
          let modificationDate = readPTPString(data, offset: &offset)
    else { return nil }
    return PTPObjectInfo(
        storageID: storageID,
        objectFormat: objectFormat,
        protectionStatus: protectionStatus,
        objectSize: objectSize,
        parentObject: parentObject,
        associationType: associationType,
        filename: filename,
        captureDate: captureDate,
        modificationDate: modificationDate
    )
}

func verifiedPTPObjectHandle(
    originalName: String,
    size: UInt64,
    creationDate: Date?,
    objects: [UInt32: PTPObjectInfo],
    timeZones: [TimeZone] = [TimeZone.current, TimeZone(secondsFromGMT: 0)!]
) -> UInt32? {
    guard size <= UInt64(UInt32.max) else { return nil }
    let sizeMatches = objects.filter { handle, info in
        handle != 0
            && info.associationType == 0
            && info.protectionStatus == 0
            && UInt64(info.objectSize) == size
    }
    if let creationDate {
        let timestamps = ptpTimestampKeys(creationDate, timeZones: timeZones)
        let dateMatches = sizeMatches.filter { _, info in
            ptpTimestampKey(info.captureDate).map(timestamps.contains) == true
        }
        if dateMatches.count == 1 {
            return dateMatches.keys.first
        }
        if dateMatches.count > 1 {
            let expectedName = originalName.precomposedStringWithCanonicalMapping
            let named = dateMatches.filter { _, info in
                info.filename.precomposedStringWithCanonicalMapping == expectedName
            }
            return named.count == 1 ? named.keys.first : nil
        }
    }
    let expectedName = originalName.precomposedStringWithCanonicalMapping
    let named = sizeMatches.filter { _, info in
        info.filename.precomposedStringWithCanonicalMapping == expectedName
    }
    return named.count == 1 ? named.keys.first : nil
}

func ptpDataPayload(_ data: Data, operationCode: UInt16) -> Data? {
    guard data.count >= 12,
          readUInt16LE(data, at: 4) == 2,
          readUInt16LE(data, at: 6) == operationCode,
          let declaredLength = readUInt32LE(data, at: 0),
          declaredLength >= 12
    else { return nil }
    let end = min(Int(declaredLength), data.count)
    return data.subdata(in: 12..<end)
}

func ptpResponseCode(_ data: Data) -> UInt16? {
    guard data.count >= 12, readUInt16LE(data, at: 4) == 3 else { return nil }
    return readUInt16LE(data, at: 6)
}

private func ptpResponseDescription(_ code: UInt16) -> String {
    let names: [UInt16: String] = [
        0x2002: "GeneralError",
        0x2005: "OperationNotSupported",
        0x2009: "InvalidObjectHandle",
        0x200F: "AccessDenied",
        0x2012: "PartialDeletion",
        0x201D: "InvalidParentObject",
    ]
    return "PTP DeleteObject \(names[code] ?? "Error") (0x\(String(code, radix: 16)))"
}

private struct PhoneStorageCapacity {
    let totalBytes: UInt64
    let availableBytes: UInt64
}

private final class BrowserDelegate: NSObject, ICDeviceBrowserDelegate, @unchecked Sendable {
    var devices: [ICCameraDevice] = []
    var enumerated = false

    func deviceBrowser(
        _ browser: ICDeviceBrowser,
        didAdd device: ICDevice,
        moreComing: Bool
    ) {
        if let camera = device as? ICCameraDevice,
           camera.productKind?.caseInsensitiveCompare("iPhone") == .orderedSame
        {
            devices.append(camera)
        }
        if !moreComing { enumerated = true }
    }

    func deviceBrowser(
        _ browser: ICDeviceBrowser,
        didRemove device: ICDevice,
        moreGoing: Bool
    ) {
        devices.removeAll { $0 === device }
    }

    func deviceBrowserDidEnumerateLocalDevices(_ browser: ICDeviceBrowser) {
        enumerated = true
    }
}

private final class CameraDelegate: NSObject, ICCameraDeviceDelegate, @unchecked Sendable {
    var catalogReady = false
    var removed = false

    func device(_ device: ICDevice, didCloseSessionWithError error: (any Error)?) {}
    func didRemove(_ device: ICDevice) { removed = true }
    func device(_ device: ICDevice, didOpenSessionWithError error: (any Error)?) {}
    func deviceDidBecomeReady(_ device: ICDevice) {}
    func cameraDevice(_ camera: ICCameraDevice, didAdd items: [ICCameraItem]) {}
    func cameraDevice(_ camera: ICCameraDevice, didRemove items: [ICCameraItem]) {}
    func cameraDevice(
        _ camera: ICCameraDevice,
        didReceiveThumbnail thumbnail: CGImage?,
        for item: ICCameraItem,
        error: (any Error)?
    ) {}
    func cameraDevice(
        _ camera: ICCameraDevice,
        didReceiveMetadata metadata: [AnyHashable: Any]?,
        for item: ICCameraItem,
        error: (any Error)?
    ) {}
    func cameraDevice(_ camera: ICCameraDevice, didRenameItems items: [ICCameraItem]) {}
    func cameraDeviceDidChangeCapability(_ camera: ICCameraDevice) {}
    func cameraDevice(_ camera: ICCameraDevice, didReceivePTPEvent eventData: Data) {}
    func deviceDidBecomeReady(withCompleteContentCatalog device: ICCameraDevice) {
        catalogReady = true
    }
    func cameraDeviceDidRemoveAccessRestriction(_ device: ICDevice) {}
    func cameraDeviceDidEnableAccessRestriction(_ device: ICDevice) {}
}

public final class PhoneDeviceSession {
    private let browser = ICDeviceBrowser()
    private let browserDelegate = BrowserDelegate()
    private let cameraDelegate = CameraDelegate()
    private var camera: ICCameraDevice?
    private var filesByToken: [String: ICCameraFile] = [:]
    private var fingerprintsByToken: [String: String] = [:]
    private var cutoff: Date?
    private var ptpTransactionID: UInt32 = 1
    private var ptpObjectCatalogCache: [UInt32: PTPObjectInfo]?

    public init() {}

    private func wait(until condition: () -> Bool, timeout: TimeInterval) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition() && Date() < deadline {
            RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.05))
        }
        return condition()
    }

    private func pumpRunLoop(for duration: TimeInterval) {
        let deadline = Date().addingTimeInterval(duration)
        while Date() < deadline {
            RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.05))
        }
    }

    private func ptpCommand(operationCode: UInt16, parameters: [UInt32] = []) -> Data {
        var data = Data()
        appendUInt32LE(UInt32(12 + parameters.count * 4), to: &data)
        appendUInt16LE(1, to: &data)
        appendUInt16LE(operationCode, to: &data)
        appendUInt32LE(ptpTransactionID, to: &data)
        ptpTransactionID &+= 1
        for parameter in parameters {
            appendUInt32LE(parameter, to: &data)
        }
        return data
    }

    private func sendPTP(
        _ camera: ICCameraDevice,
        operationCode: UInt16,
        parameters: [UInt32] = [],
        timeout: TimeInterval
    ) -> Data? {
        var finished = false
        var first = Data()
        var second = Data()
        var failure: Error?
        camera.requestSendPTPCommand(
            ptpCommand(operationCode: operationCode, parameters: parameters),
            outData: nil
        ) { firstResponse, secondResponse, error in
            first = firstResponse
            second = secondResponse
            failure = error
            finished = true
        }
        guard wait(until: { finished }, timeout: timeout), failure == nil else { return nil }
        for candidate in [first, second] {
            if let payload = ptpDataPayload(candidate, operationCode: operationCode) {
                return payload
            }
        }
        let responseCandidates = [first, second].filter {
            readUInt16LE($0, at: 4) != 3 && !$0.isEmpty
        }
        return responseCandidates.first
    }

    private func deletePTPObject(
        _ camera: ICCameraDevice,
        handle: UInt32,
        timeout: TimeInterval
    ) -> String? {
        let deleteObject = UInt16(0x100B)
        var finished = false
        var first = Data()
        var second = Data()
        var failure: Error?
        camera.requestSendPTPCommand(
            ptpCommand(operationCode: deleteObject, parameters: [handle]),
            outData: nil
        ) { firstResponse, secondResponse, error in
            first = firstResponse
            second = secondResponse
            failure = error
            finished = true
        }
        guard wait(until: { finished }, timeout: timeout) else {
            return "PTP DeleteObject timed out"
        }
        if let failure {
            let value = failure as NSError
            return "\(value.domain) code \(value.code): \(value.localizedDescription)"
        }
        guard let code = [first, second].compactMap(ptpResponseCode).first else {
            return "PTP DeleteObject returned no response code"
        }
        return code == 0x2001 ? nil : ptpResponseDescription(code)
    }

    private func storageCapacity(
        _ camera: ICCameraDevice,
        timeout: TimeInterval
    ) -> PhoneStorageCapacity? {
        let getStorageIDs = UInt16(0x1004)
        let getStorageInfo = UInt16(0x1005)
        guard camera.capabilities.contains(
            ICDeviceCapability.cameraDeviceCanAcceptPTPCommands.rawValue
        ),
            let idsPayload = sendPTP(
                camera, operationCode: getStorageIDs, timeout: timeout
            ),
            let count = readUInt32LE(idsPayload, at: 0),
            count > 0,
            idsPayload.count >= 4 + Int(count) * 4
        else { return nil }
        var total = UInt64(0)
        var available = UInt64(0)
        var found = false
        for index in 0..<Int(count) {
            guard let storageID = readUInt32LE(idsPayload, at: 4 + index * 4),
                  let info = sendPTP(
                      camera,
                      operationCode: getStorageInfo,
                      parameters: [storageID],
                      timeout: timeout
                  ),
                  let capacity = readUInt64LE(info, at: 6),
                  let free = readUInt64LE(info, at: 14)
            else { continue }
            let (nextTotal, totalOverflow) = total.addingReportingOverflow(capacity)
            let (nextAvailable, availableOverflow) = available.addingReportingOverflow(free)
            guard !totalOverflow, !availableOverflow else { return nil }
            total = nextTotal
            available = nextAvailable
            found = true
        }
        return found ? PhoneStorageCapacity(totalBytes: total, availableBytes: available) : nil
    }

    private func ptpObjectHandles(
        _ camera: ICCameraDevice,
        timeout: TimeInterval
    ) -> [UInt32]? {
        let getObjectHandles = UInt16(0x1007)
        guard let payload = sendPTP(
            camera,
            operationCode: getObjectHandles,
            parameters: [UInt32.max, 0, 0],
            timeout: timeout
        ) else { return nil }
        return ptpUInt32Array(payload)
    }

    private func ptpOperations(
        _ camera: ICCameraDevice,
        timeout: TimeInterval
    ) -> Set<UInt16>? {
        let getDeviceInfo = UInt16(0x1001)
        guard let payload = sendPTP(
            camera,
            operationCode: getDeviceInfo,
            timeout: timeout
        ), let operations = ptpSupportedOperations(payload)
        else { return nil }
        return Set(operations)
    }

    private func ptpObjectInfo(
        _ camera: ICCameraDevice,
        handle: UInt32,
        timeout: TimeInterval
    ) -> PTPObjectInfo? {
        let getObjectInfo = UInt16(0x1008)
        guard let payload = sendPTP(
            camera,
            operationCode: getObjectInfo,
            parameters: [handle],
            timeout: timeout
        ) else { return nil }
        return PhotosHelperCore.ptpObjectInfo(payload)
    }

    private func ptpObjectCatalog(
        _ camera: ICCameraDevice,
        timeout: TimeInterval
    ) -> [UInt32: PTPObjectInfo]? {
        if let ptpObjectCatalogCache { return ptpObjectCatalogCache }
        guard let operations = ptpOperations(camera, timeout: timeout),
              operations.isSuperset(of: [0x1007, 0x1008, 0x100B]),
              let handles = ptpObjectHandles(camera, timeout: timeout)
        else { return nil }
        var catalog: [UInt32: PTPObjectInfo] = [:]
        catalog.reserveCapacity(handles.count)
        for handle in handles {
            guard handle != 0,
                  catalog[handle] == nil,
                  let info = ptpObjectInfo(camera, handle: handle, timeout: timeout)
            else { return nil }
            catalog[handle] = info
        }
        guard catalog.count == handles.count else { return nil }
        ptpObjectCatalogCache = catalog
        return catalog
    }

    private func openCamera(timeout: TimeInterval) throws -> ICCameraDevice {
        if let camera { return camera }
        let deadline = Date().addingTimeInterval(timeout)
        let remainingTime = { max(0, deadline.timeIntervalSinceNow) }
        browser.delegate = browserDelegate
        browser.browsedDeviceTypeMask = .camera
        browser.start()
        guard wait(until: { self.browserDelegate.enumerated }, timeout: remainingTime()) else {
            throw PhoneAdapterError.unavailable("Timed out while looking for an iPhone.")
        }
        guard browserDelegate.devices.count == 1,
              let selected = browserDelegate.devices.first
        else {
            throw PhoneAdapterError.unavailable("Connect exactly one unlocked iPhone.")
        }
        selected.delegate = cameraDelegate
        while !selected.hasOpenSession {
            var openFinished = false
            var openError: Error?
            selected.requestOpenSession(options: [.enumerationChronologicalOrder: true]) { error in
                openError = error
                openFinished = true
            }
            guard wait(until: { openFinished }, timeout: remainingTime()) else {
                throw PhoneAdapterError.unavailable("Timed out while opening the iPhone.")
            }
            guard let openError else { break }
            guard isRetryablePhoneOpenError(openError), remainingTime() > 0 else {
                throw openError
            }
            pumpRunLoop(for: min(0.5, remainingTime()))
        }
        guard selected.hasOpenSession else {
            throw PhoneAdapterError.unavailable(
                "The iPhone did not expose its photos after unlock. Keep it unlocked, "
                    + "close Photos and Image Capture, reconnect it, and retry."
            )
        }
        guard wait(
            until: {
                self.cameraDelegate.catalogReady
                    || selected.contentCatalogPercentCompleted >= 100
            },
            timeout: remainingTime()
        ) else {
            throw PhoneAdapterError.unavailable(
                "The iPhone did not finish publishing its media catalog."
            )
        }
        camera = selected
        return selected
    }

    private func itemFingerprint(_ file: ICCameraFile, deviceKey: String) -> String {
        let creation = (iso8601(file.fileCreationDate).stringValue ?? "")
        return digest(
            [
                deviceKey,
                file.originalFilename ?? file.name ?? "",
                String(file.fileSize),
                creation,
                file.relatedUUID ?? "",
                file.groupUUID ?? "",
                file.originatingAssetID ?? "",
            ].joined(separator: "\0")
        )
    }

    private func assetKey(_ file: ICCameraFile, fingerprint: String) -> String {
        let relationship = file.relatedUUID
            ?? file.groupUUID
            ?? file.originatingAssetID
            ?? file.pairedRawImage?.relatedUUID
            ?? file.burstUUID
        return digest(relationship ?? fingerprint)
    }

    private func mediaType(_ file: ICCameraFile) -> String {
        let value = (file.uti ?? "").lowercased()
        if value.contains("movie") || value.contains("video") || value.contains("quicktime") {
            return "video"
        }
        return "image"
    }

    private func logicalAssetCount(_ files: [ICCameraFile]) -> Int {
        var relationshipKeys: Set<String> = []
        var standaloneCount = 0
        for file in files {
            let relationship = file.relatedUUID
                ?? file.groupUUID
                ?? file.originatingAssetID
                ?? file.pairedRawImage?.relatedUUID
                ?? file.burstUUID
            if let relationship {
                relationshipKeys.insert(relationship)
            } else {
                standaloneCount += 1
            }
        }
        return relationshipKeys.count + standaloneCount
    }

    private func devicePayload(
        _ camera: ICCameraDevice,
        storage: PhoneStorageCapacity?
    ) -> [String: JSONValue] {
        let persistent = camera.persistentIDString ?? camera.uuidString ?? "unidentified-device"
        let accessRestricted = camera.isAccessRestrictedAppleDevice
        let catalogAccessible = camera.hasOpenSession
            && camera.contentCatalogPercentCompleted >= 100
            && !accessRestricted
        let canDelete = supportsPerItemDeletion(
            capabilities: camera.capabilities,
            productKind: camera.productKind,
            hasOpenSession: camera.hasOpenSession,
            accessRestricted: accessRestricted
        )
        let deleteCapabilityDeclared = camera.capabilities.contains(
            ICDeviceCapability.cameraDeviceCanDeleteOneFile.rawValue
        )
        let canAcceptPTPCommands = camera.capabilities.contains(
            ICDeviceCapability.cameraDeviceCanAcceptPTPCommands.rawValue
        )
        return [
            "device_key": .string(digest(persistent)),
            "name": .string(camera.name ?? "iPhone"),
            "product_kind": .string(camera.productKind ?? "iPhone"),
            "locked": .boolean(camera.isLocked && !catalogAccessible),
            "trusted": .boolean(!accessRestricted),
            "icloud_photos_enabled": .boolean(camera.iCloudPhotosEnabled),
            "can_delete": .boolean(canDelete),
            "delete_capability_declared": .boolean(deleteCapabilityDeclared),
            "can_accept_ptp_commands": .boolean(canAcceptPTPCommands),
            "total_capacity_bytes": storage.map {
                .integer(Int(clamping: $0.totalBytes))
            } ?? .null,
            "available_capacity_bytes": storage.map {
                .integer(Int(clamping: $0.availableBytes))
            } ?? .null,
        ]
    }

    public func discover(payload: [String: JSONValue]) throws -> (
        resources: [[String: JSONValue]], result: [String: JSONValue]
    ) {
        guard let cutoffText = payload["cutoff_at_utc"]?.stringValue,
              let cutoffDate = ISO8601DateFormatter().date(from: cutoffText)
        else {
            throw PhoneAdapterError.invalidRequest("discover requires cutoff_at_utc")
        }
        let timeout = payload["discovery_timeout_sec"]?.doubleValue ?? 15
        let camera = try openCamera(timeout: timeout)
        cutoff = cutoffDate
        let device = devicePayload(camera, storage: storageCapacity(camera, timeout: 3))
        let deviceKey = device["device_key"]?.stringValue ?? ""
        filesByToken.removeAll()
        fingerprintsByToken.removeAll()
        var resources: [[String: JSONValue]] = []
        var warnings: [JSONValue] = []
        let mediaFiles = (camera.mediaFiles ?? []).compactMap { $0 as? ICCameraFile }
        let sortedFiles = mediaFiles.sorted(by: {
            ($0.fileCreationDate ?? .distantFuture) < ($1.fileCreationDate ?? .distantFuture)
        })
        let totalMediaBytes = sortedFiles.reduce(Int64(0)) { total, file in
            total + max(0, Int64(file.fileSize))
        }
        let candidates = sortedFiles.filter { file in
            guard let created = file.fileCreationDate ?? file.creationDate else { return false }
            return created < cutoffDate
        }
        let fingerprints = candidates.map { itemFingerprint($0, deviceKey: deviceKey) }
        let fingerprintCounts = Dictionary(grouping: fingerprints, by: { $0 })
            .mapValues(\.count)
        let candidateItems = Set(candidates.map(ObjectIdentifier.init))
        if sortedFiles.contains(where: { ($0.fileCreationDate ?? $0.creationDate) == nil }) {
            warnings.append(.string("ITEM_WITHOUT_CREATION_DATE_SKIPPED"))
        }
        for file in candidates {
            guard let created = file.fileCreationDate ?? file.creationDate else { continue }
            let fingerprint = itemFingerprint(file, deviceKey: deviceKey)
            guard fingerprintCounts[fingerprint] == 1 else {
                warnings.append(.string("AMBIGUOUS_ITEM_FINGERPRINT_SKIPPED"))
                continue
            }
            let token = UUID().uuidString.lowercased()
            filesByToken[token] = file
            fingerprintsByToken[token] = fingerprint
            let name = file.originalFilename ?? file.name ?? "unnamed-media"
            let warning = file.pairedRawImage.flatMap {
                candidateItems.contains(ObjectIdentifier($0)) ? nil : "INCOMPLETE_RAW_PAIR"
            }
            resources.append([
                "token": .string(token),
                "item_fingerprint": .string(fingerprint),
                "ptp_object_handle": .integer(Int(file.ptpObjectHandle)),
                "original_name": .string(name),
                "size": .integer(Int(file.fileSize)),
                "creation_at_utc": iso8601(created),
                "modification_at_utc": iso8601(file.fileModificationDate ?? file.modificationDate),
                "uti": file.uti.map(JSONValue.string) ?? .null,
                "asset_key": .string(assetKey(file, fingerprint: fingerprint)),
                "media_type": .string(mediaType(file)),
                "required": .boolean(true),
                "downloadable": .boolean(file.fileSize >= 0),
                "warning": warning.map(JSONValue.string) ?? .null,
            ])
        }
        return (
            resources,
            [
                "device": .object(device),
                "warnings": .array(warnings),
                "total_media_assets": .integer(logicalAssetCount(sortedFiles)),
                "total_media_resources": .integer(sortedFiles.count),
                "total_media_bytes": .integer(Int(clamping: totalMediaBytes)),
            ]
        )
    }

    public func download(payload: [String: JSONValue]) throws -> [String: JSONValue] {
        guard let token = payload["token"]?.stringValue,
              let path = payload["path"]?.stringValue,
              let file = filesByToken[token]
        else {
            throw PhoneAdapterError.invalidRequest("download requires a current item token and path")
        }
        let destination = URL(fileURLWithPath: path)
        try FileManager.default.createDirectory(
            at: destination.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        if FileManager.default.fileExists(atPath: destination.path) {
            throw PhoneAdapterError.unsafe("Refusing to overwrite an existing download.")
        }
        var finished = false
        var failure: Error?
        var savedName: String?
        file.requestDownload(
            options: [
                .downloadsDirectoryURL: destination.deletingLastPathComponent(),
                .saveAsFilename: destination.lastPathComponent,
                .overwrite: false,
                .deleteAfterSuccessfulDownload: false,
            ]
        ) { filename, error in
            savedName = filename
            failure = error
            finished = true
        }
        guard wait(until: { finished }, timeout: 3600) else {
            throw PhoneAdapterError.unavailable("Timed out while downloading from iPhone.")
        }
        if let failure { throw failure }
        let actual = destination.deletingLastPathComponent()
            .appendingPathComponent(savedName ?? destination.lastPathComponent)
        if actual != destination, FileManager.default.fileExists(atPath: actual.path) {
            try FileManager.default.moveItem(at: actual, to: destination)
        }
        let attributes = try FileManager.default.attributesOfItem(atPath: destination.path)
        return ["token": .string(token), "size": .integer((attributes[.size] as? NSNumber)?.intValue ?? -1)]
    }

    public func revalidate(payload: [String: JSONValue]) throws -> [String: JSONValue] {
        guard let items = payload["items"]?.arrayValue else {
            throw PhoneAdapterError.invalidRequest("revalidate requires items")
        }
        var valid: [JSONValue] = []
        for item in items.compactMap(\.objectValue) {
            guard let token = item["token"]?.stringValue,
                  let expected = item["item_fingerprint"]?.stringValue,
                  let expectedSize = item["size"]?.integerValue,
                  let file = filesByToken[token],
                  fingerprintsByToken[token] == expected,
                  Int(file.fileSize) == expectedSize,
                  file.fileCreationDate.map({ $0 < (cutoff ?? .distantPast) }) == true
            else { continue }
            valid.append(.string(token))
        }
        return ["valid_tokens": .array(valid)]
    }

    public func delete(payload: [String: JSONValue]) throws -> [String: JSONValue] {
        guard payload["batch_id"]?.stringValue?.isEmpty == false,
              payload["plan_sha256"]?.stringValue?.count == 64,
              let cutoffText = payload["cutoff_at_utc"]?.stringValue,
              let requestCutoff = ISO8601DateFormatter().date(from: cutoffText),
              requestCutoff == cutoff,
              let tokens = payload["tokens"]?.stringArrayValue,
              let camera
        else {
            throw PhoneAdapterError.unsafe("Delete request does not match the active discovery session.")
        }
        guard !camera.isAccessRestrictedAppleDevice,
              supportsPerItemDeletion(
                  capabilities: camera.capabilities,
                  productKind: camera.productKind,
                  hasOpenSession: camera.hasOpenSession,
                  accessRestricted: camera.isAccessRestrictedAppleDevice
              )
        else {
            throw PhoneAdapterError.unsafe("The iPhone is locked, untrusted, or cannot delete files.")
        }
        let files = tokens.compactMap { filesByToken[$0] }
        guard files.count == tokens.count else {
            throw PhoneAdapterError.unsafe("Delete request contains an unknown item token.")
        }
        var finished = false
        var deletedFiles: [ICCameraItem] = []
        var failedFiles: [ICCameraItem] = []
        var failureReasonByItem: [ObjectIdentifier: String] = [:]
        var failureReasonByToken: [String: String] = [:]
        var completionError: Error?
        _ = camera.requestDeleteFiles(files, deleteFailed: { failures in
            for (reason, item) in failures {
                failedFiles.append(item)
                failureReasonByItem[ObjectIdentifier(item)] = String(describing: reason)
            }
        }, completion: { result, error in
            deletedFiles.append(contentsOf: result[.successful] ?? [])
            failedFiles.append(contentsOf: result[.failed] ?? [])
            failedFiles.append(contentsOf: result[.canceled] ?? [])
            completionError = error
            finished = true
        })
        guard wait(until: { finished }, timeout: 3600) else {
            throw PhoneAdapterError.unavailable("Timed out while deleting from iPhone.")
        }
        let deletedItems = Set(deletedFiles.map(ObjectIdentifier.init))
        let failedItems = Set(failedFiles.map(ObjectIdentifier.init))
        var deleted = tokens.filter { token in
            filesByToken[token].map {
                let identity = ObjectIdentifier($0)
                return deletedItems.contains(identity) && !failedItems.contains(identity)
            } ?? false
        }
        let successfullyDeleted = Set(deleted)
        var failed = tokens.filter { token in
            !successfullyDeleted.contains(token)
        }
        let deleteCapabilityDeclared = camera.capabilities.contains(
            ICDeviceCapability.cameraDeviceCanDeleteOneFile.rawValue
        )
        let canAcceptPTPCommands = camera.capabilities.contains(
            ICDeviceCapability.cameraDeviceCanAcceptPTPCommands.rawValue
        )
        let completionCode = (completionError as NSError?)?.code
        let entireRequestFailed = deleted.isEmpty
            && failed.count == tokens.count
            && completionCode == -9941
            && failureReasonByItem.isEmpty
        var deleteMethod = "requestDeleteFiles"
        if entireRequestFailed, !deleteCapabilityDeclared, canAcceptPTPCommands {
            deleteMethod = "requestDeleteFiles+PTPDeleteObject(verified-object-info)"
            var ptpDeleted: [String] = []
            if let catalog = ptpObjectCatalog(camera, timeout: 30) {
                var verifiedHandles: [String: UInt32] = [:]
                for token in failed {
                    guard let file = filesByToken[token], file.fileSize >= 0,
                          let handle = verifiedPTPObjectHandle(
                              originalName: file.originalFilename ?? file.name ?? "",
                              size: UInt64(file.fileSize),
                              creationDate: file.fileCreationDate ?? file.creationDate,
                              objects: catalog
                          )
                    else {
                        failureReasonByToken[token] = "PTP_OBJECT_IDENTITY_NOT_UNIQUE"
                        continue
                    }
                    verifiedHandles[token] = handle
                }
                let uniqueHandles = Set(verifiedHandles.values)
                if verifiedHandles.count == failed.count,
                   uniqueHandles.count == failed.count
                {
                    for token in failed {
                        guard let handle = verifiedHandles[token] else { continue }
                        if let reason = deletePTPObject(
                            camera,
                            handle: handle,
                            timeout: 30
                        ) {
                            failureReasonByToken[token] = reason
                        } else {
                            ptpDeleted.append(token)
                            ptpObjectCatalogCache?.removeValue(forKey: handle)
                        }
                    }
                } else {
                    for token in failed where failureReasonByToken[token] == nil {
                        failureReasonByToken[token] = "PTP_OBJECT_IDENTITY_NOT_ONE_TO_ONE"
                    }
                }
            } else {
                for token in failed {
                    failureReasonByToken[token] = "PTP_OBJECT_CATALOG_UNAVAILABLE"
                }
            }
            deleted.append(contentsOf: ptpDeleted)
            let ptpDeletedSet = Set(ptpDeleted)
            failed.removeAll { ptpDeletedSet.contains($0) }
        }
        var failureReasons: [String: JSONValue] = [:]
        for token in failed {
            guard let file = filesByToken[token] else {
                failureReasons[token] = .string("UNKNOWN_SESSION_ITEM")
                continue
            }
            let identity = ObjectIdentifier(file)
            if let reason = failureReasonByToken[token] {
                failureReasons[token] = .string(reason)
            } else if let reason = failureReasonByItem[identity] {
                failureReasons[token] = .string(reason)
            } else if failedItems.contains(identity) {
                failureReasons[token] = .string("ICDeleteFailed")
            } else if completionError == nil {
                failureReasons[token] = .string("NO_SUCCESS_RESULT")
            }
        }
        let completionErrorText = completionError.map { error in
            let value = error as NSError
            return "\(value.domain) code \(value.code): \(value.localizedDescription)"
        }
        return [
            "deleted_tokens": .array(deleted.map(JSONValue.string)),
            "failed_tokens": .array(failed.map(JSONValue.string)),
            "failure_reasons": .object(failureReasons),
            "completion_error": failed.isEmpty
                ? .null
                : completionErrorText.map(JSONValue.string) ?? .null,
            "delete_method": .string(deleteMethod),
        ]
    }

    public func close() {
        if let camera, camera.hasOpenSession {
            camera.requestCloseSession(options: nil) { _ in }
        }
        browser.stop()
        camera = nil
        filesByToken.removeAll()
        fingerprintsByToken.removeAll()
        ptpObjectCatalogCache = nil
    }
}

public func phoneErrorEnvelope(_ error: Error, requestID: String) -> Envelope {
    let message = (error as? LocalizedError)?.errorDescription ?? String(describing: error)
    return Envelope(
        schemaVersion: 2,
        type: .error,
        requestID: requestID,
        payload: [
            "error_code": .string("PHONE_ADAPTER_ERROR"),
            "message": .string(message),
        ]
    )
}
