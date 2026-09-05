import Foundation
import ImageCaptureCore
import Testing
@testable import PhotosHelperCore

@Test func versionEnvelopeUsesJSONLV1Contract() throws {
    let result = runCommand("version", requestID: "test-request")
    #expect(result.exitCode == 0)
    #expect(result.envelope.schemaVersion == 1)
    #expect(result.envelope.type == .result)
    #expect(result.envelope.requestID == "test-request")
    let line = try encodeEnvelope(result.envelope)
    #expect(!line.contains("\n"))
    let decoded = try JSONDecoder().decode(Envelope.self, from: Data(line.utf8))
    #expect(decoded == result.envelope)
}

@Test func requestEnvelopeRoundTrips() throws {
    let envelope = Envelope(
        schemaVersion: 2,
        type: .request,
        requestID: "request",
        payload: [
            "command": .string("discover"),
            "discovery_timeout_sec": .double(15),
        ]
    )
    #expect(try decodeEnvelope(encodeEnvelope(envelope)) == envelope)
}

@Test func photoLibraryResourceKeysAreStableAndOrdinalSensitive() {
    let first = photoLibraryResourceKey(
        typeCode: 1,
        originalName: "IMG_1000.HEIC",
        uti: "public.heic",
        ordinal: 0
    )
    #expect(first.count == 64)
    #expect(first == photoLibraryResourceKey(
        typeCode: 1,
        originalName: "IMG_1000.HEIC",
        uti: "public.heic",
        ordinal: 0
    ))
    #expect(first != photoLibraryResourceKey(
        typeCode: 1,
        originalName: "IMG_1000.HEIC",
        uti: "public.heic",
        ordinal: 1
    ))
}

@Test func onlyPasscodeLockOpenErrorsAreRetried() {
    let locked = NSError(
        domain: "com.apple.ImageCaptureCore",
        code: phoneDevicePasscodeLockedErrorCode
    )
    let failed = NSError(domain: "com.apple.ImageCaptureCore", code: -9927)
    #expect(isRetryablePhoneOpenError(locked))
    #expect(!isRetryablePhoneOpenError(failed))
}

@Test func trustedOpenIPhoneWithPTPSupportCanRequestPerItemDeletion() {
    let ptp = ICDeviceCapability.cameraDeviceCanAcceptPTPCommands.rawValue
    #expect(supportsPerItemDeletion(
        capabilities: [ptp],
        productKind: "iPhone",
        hasOpenSession: true,
        accessRestricted: false
    ))
    #expect(!supportsPerItemDeletion(
        capabilities: [ptp],
        productKind: "iPhone",
        hasOpenSession: true,
        accessRestricted: true
    ))
    #expect(!supportsPerItemDeletion(
        capabilities: [ptp],
        productKind: "Camera",
        hasOpenSession: true,
        accessRestricted: false
    ))
}

@Test func ptpDataContainerExtractsLittleEndianPayload() {
    let container = Data([
        16, 0, 0, 0,
        2, 0,
        4, 16,
        1, 0, 0, 0,
        1, 0, 0, 0,
    ])
    #expect(ptpDataPayload(container, operationCode: 0x1004) == Data([1, 0, 0, 0]))
    #expect(readUInt32LE(container, at: 12) == 1)
    #expect(readUInt64LE(Data([1, 0, 0, 0, 0, 0, 0, 2]), at: 0) == 0x0200000000000001)
}

@Test func ptpResponseContainerExtractsResponseCode() {
    let success = Data([
        12, 0, 0, 0,
        3, 0,
        1, 32,
        1, 0, 0, 0,
    ])
    #expect(ptpResponseCode(success) == 0x2001)
    #expect(ptpResponseCode(Data([1, 2, 3])) == nil)
}

@Test func ptpUInt32ArrayRequiresExactLittleEndianPayload() {
    let payload = Data([
        2, 0, 0, 0,
        4, 3, 2, 1,
        8, 7, 6, 5,
    ])
    #expect(ptpUInt32Array(payload) == [0x01020304, 0x05060708])
    #expect(ptpUInt32Array(payload + Data([0])) == nil)
    #expect(ptpUInt32Array(Data([2, 0, 0, 0, 1, 0, 0, 0])) == nil)
}

@Test func ptpObjectInfoParsesIdentityFields() {
    var payload = Data(repeating: 0, count: 52)
    payload[0] = 1
    payload[4] = 1
    payload[5] = 0x38
    payload[8] = 12
    payload[38] = 9
    payload[42] = 1
    for value in ["IMG_0001.JPG", "20200102T030405", "20200102T040506"] {
        let units = Array(value.utf16) + [0]
        payload.append(UInt8(units.count))
        for unit in units {
            payload.append(UInt8(truncatingIfNeeded: unit))
            payload.append(UInt8(truncatingIfNeeded: unit >> 8))
        }
    }

    let info = ptpObjectInfo(payload)

    #expect(info == PTPObjectInfo(
        storageID: 1,
        objectFormat: 0x3801,
        protectionStatus: 0,
        objectSize: 12,
        parentObject: 9,
        associationType: 1,
        filename: "IMG_0001.JPG",
        captureDate: "20200102T030405",
        modificationDate: "20200102T040506"
    ))
    #expect(ptpObjectInfo(payload.dropLast()) == nil)
}

@Test func ptpDeviceInfoParsesSupportedOperations() {
    var payload = Data([
        0, 1,
        0, 0, 0, 0,
        0, 1,
        0,
        0, 0,
        3, 0, 0, 0,
    ])
    for operation in [UInt16(0x1001), 0x1007, 0x100B] {
        payload.append(UInt8(truncatingIfNeeded: operation))
        payload.append(UInt8(truncatingIfNeeded: operation >> 8))
    }

    #expect(ptpSupportedOperations(payload) == [0x1001, 0x1007, 0x100B])
    #expect(ptpSupportedOperations(payload.dropLast()) == nil)
}

@Test func ptpDeleteHandleUsesUniqueDateAndExactSize() throws {
    let created = try #require(ISO8601DateFormatter().date(from: "2020-01-02T03:04:05Z"))
    let objects = [
        UInt32(7): PTPObjectInfo(
            storageID: 1,
            objectFormat: 0x3801,
            protectionStatus: 0,
            objectSize: 12,
            parentObject: 9,
            associationType: 0,
            filename: "device-native-name.jpg",
            captureDate: "20200102T030405",
            modificationDate: ""
        ),
        UInt32(8): PTPObjectInfo(
            storageID: 1,
            objectFormat: 0x3801,
            protectionStatus: 0,
            objectSize: 13,
            parentObject: 9,
            associationType: 0,
            filename: "IMG_0001.JPG",
            captureDate: "20200102T030405",
            modificationDate: ""
        ),
    ]

    #expect(verifiedPTPObjectHandle(
        originalName: "IMG_0001.JPG",
        size: 12,
        creationDate: created,
        objects: objects,
        timeZones: [try #require(TimeZone(secondsFromGMT: 0))]
    ) == 7)
}

@Test func ptpDeleteHandleRejectsAmbiguousOrProtectedObjects() throws {
    let created = try #require(ISO8601DateFormatter().date(from: "2020-01-02T03:04:05Z"))
    let base = PTPObjectInfo(
        storageID: 1,
        objectFormat: 0x3801,
        protectionStatus: 0,
        objectSize: 12,
        parentObject: 9,
        associationType: 0,
        filename: "different-name.jpg",
        captureDate: "20200102T030405",
        modificationDate: ""
    )
    let protected = PTPObjectInfo(
        storageID: 1,
        objectFormat: 0x3801,
        protectionStatus: 1,
        objectSize: 12,
        parentObject: 9,
        associationType: 0,
        filename: "IMG_0001.JPG",
        captureDate: "20200102T030405",
        modificationDate: ""
    )

    #expect(verifiedPTPObjectHandle(
        originalName: "IMG_0001.JPG",
        size: 12,
        creationDate: created,
        objects: [7: base, 8: base],
        timeZones: [try #require(TimeZone(secondsFromGMT: 0))]
    ) == nil)
    #expect(verifiedPTPObjectHandle(
        originalName: "IMG_0001.JPG",
        size: 12,
        creationDate: created,
        objects: [7: protected],
        timeZones: [try #require(TimeZone(secondsFromGMT: 0))]
    ) == nil)
}

@Test func inspectFileClassifiesAnImportedImage() async throws {
    let url = FileManager.default.temporaryDirectory
        .appendingPathComponent(UUID().uuidString)
        .appendingPathExtension("jpg")
    try Data("fixture".utf8).write(to: url)
    defer { try? FileManager.default.removeItem(at: url) }
    let payload = try await inspectMediaFile(payload: ["path": .string(url.path)])
    #expect(payload["kind"] == .string("photo"))
    #expect(payload["creation_at_utc"] == .null)
}
