import Darwin
import AppKit
import Foundation
import PhotosHelperCore

func emit(_ envelope: Envelope) throws {
    print(try encodeEnvelope(envelope))
    fflush(stdout)
}

let arguments = Array(CommandLine.arguments.dropFirst())
let command = arguments.first ?? "version"
let requestIndex = arguments.firstIndex(of: "--request-id")
let requestID = requestIndex.flatMap { index in
    arguments.indices.contains(index + 1) ? arguments[index + 1] : nil
} ?? UUID().uuidString.lowercased()

func preparePhotoLibraryApplication() {
    _ = NSApplication.shared
    NSApp.setActivationPolicy(.accessory)
    NSApp.activate(ignoringOtherApps: true)
}

func runPhoneSession() {
    let session = PhoneDeviceSession()
    defer { session.close() }
    while let line = readLine() {
        do {
            let request = try decodeEnvelope(line)
            guard request.schemaVersion == 2,
                  request.type == .request,
                  let command = request.payload["command"]?.stringValue
            else {
                throw PhoneAdapterError.invalidRequest("Expected a JSONL v2 request envelope.")
            }
            switch command {
            case "discover":
                let response = try session.discover(payload: request.payload)
                for resource in response.resources {
                    try emit(Envelope(
                        schemaVersion: 2,
                        type: .resource,
                        requestID: request.requestID,
                        payload: resource
                    ))
                }
                try emit(Envelope(
                    schemaVersion: 2,
                    type: .result,
                    requestID: request.requestID,
                    payload: response.result
                ))
            case "download":
                try emit(Envelope(
                    schemaVersion: 2,
                    type: .result,
                    requestID: request.requestID,
                    payload: try session.download(payload: request.payload)
                ))
            case "revalidate":
                try emit(Envelope(
                    schemaVersion: 2,
                    type: .result,
                    requestID: request.requestID,
                    payload: try session.revalidate(payload: request.payload)
                ))
            case "delete":
                try emit(Envelope(
                    schemaVersion: 2,
                    type: .result,
                    requestID: request.requestID,
                    payload: try session.delete(payload: request.payload)
                ))
            case "close":
                try emit(Envelope(
                    schemaVersion: 2,
                    type: .result,
                    requestID: request.requestID,
                    payload: ["status": .string("closed")]
                ))
                return
            default:
                throw PhoneAdapterError.invalidRequest("Unknown phone command: \(command)")
            }
        } catch {
            let fallbackID = (try? decodeEnvelope(line).requestID) ?? UUID().uuidString.lowercased()
            try? emit(phoneErrorEnvelope(error, requestID: fallbackID))
        }
    }
}

func runPhotoLibrarySession() {
    preparePhotoLibraryApplication()
    let session = PhotoLibrarySession()
    while let line = readLine() {
        do {
            let request = try decodeEnvelope(line)
            guard request.schemaVersion == 4,
                  request.type == .request,
                  let command = request.payload["command"]?.stringValue
            else {
                throw PhotoLibraryAdapterError.invalidRequest(
                    "Expected a JSONL v4 request envelope."
                )
            }
            switch command {
            case "discover":
                let response = try session.discover(payload: request.payload)
                for asset in response.assets {
                    try emit(Envelope(
                        schemaVersion: 4,
                        type: .asset,
                        requestID: request.requestID,
                        payload: asset
                    ))
                }
                try emit(Envelope(
                    schemaVersion: 4,
                    type: .result,
                    requestID: request.requestID,
                    payload: response.result
                ))
            case "discover-large-videos":
                let response = try session.discoverLargeVideos()
                for asset in response.assets {
                    try emit(Envelope(
                        schemaVersion: 4,
                        type: .asset,
                        requestID: request.requestID,
                        payload: asset
                    ))
                }
                try emit(Envelope(
                    schemaVersion: 4,
                    type: .result,
                    requestID: request.requestID,
                    payload: response.result
                ))
            case "probe-resource-size":
                try emit(Envelope(
                    schemaVersion: 4,
                    type: .result,
                    requestID: request.requestID,
                    payload: try session.probeResourceSize(payload: request.payload)
                ))
            case "download":
                try emit(Envelope(
                    schemaVersion: 4,
                    type: .result,
                    requestID: request.requestID,
                    payload: try session.download(payload: request.payload)
                ))
            case "revalidate":
                try emit(Envelope(
                    schemaVersion: 4,
                    type: .result,
                    requestID: request.requestID,
                    payload: try session.revalidate(payload: request.payload)
                ))
            case "delete":
                try emit(Envelope(
                    schemaVersion: 4,
                    type: .result,
                    requestID: request.requestID,
                    payload: try session.delete(payload: request.payload)
                ))
            case "close":
                try emit(Envelope(
                    schemaVersion: 4,
                    type: .result,
                    requestID: request.requestID,
                    payload: ["status": .string("closed")]
                ))
                return
            default:
                throw PhotoLibraryAdapterError.invalidRequest(
                    "Unknown Photos library command: \(command)"
                )
            }
        } catch {
            let fallbackID = (try? decodeEnvelope(line).requestID) ?? UUID().uuidString.lowercased()
            let message = (error as? LocalizedError)?.errorDescription ?? String(describing: error)
            try? emit(Envelope(schemaVersion: 4, type: .error, requestID: fallbackID, payload: [
                "error_code": .string("PHOTO_LIBRARY_ERROR"),
                "message": .string(message),
            ]))
        }
    }
}

if command == "phone-session" {
    runPhoneSession()
    exit(0)
}

if command == "photo-library-session" {
    runPhotoLibrarySession()
    exit(0)
}

if command == "photo-library-authorize" {
    preparePhotoLibraryApplication()
    do {
        let status = try PhotoLibrarySession().authorize()
        try emit(Envelope(
            schemaVersion: 4,
            type: .result,
            requestID: requestID,
            payload: ["authorization": .string(status)]
        ))
        exit(0)
    } catch {
        let message = (error as? LocalizedError)?.errorDescription ?? String(describing: error)
        try? emit(Envelope(schemaVersion: 4, type: .error, requestID: requestID, payload: [
            "error_code": .string("PHOTO_LIBRARY_AUTHORIZATION_ERROR"),
            "message": .string(message),
        ]))
        exit(2)
    }
}

do {
    if command == "version" || command == "self-test" {
        let result = runCommand(command, requestID: requestID)
        try emit(result.envelope)
        exit(result.exitCode)
    }
    let line = readLine() ?? ""
    let request = try decodeEnvelope(line)
    guard request.schemaVersion == 1, request.requestID == requestID else {
        throw MediaMetadataError.invalidRequest("Mismatched JSONL request envelope.")
    }
    switch command {
    case "inspect-file":
        try emit(Envelope(
            type: .result,
            requestID: requestID,
            payload: try await inspectMediaFile(payload: request.payload)
        ))
    default:
        let result = runCommand(command, requestID: requestID)
        try emit(result.envelope)
        exit(result.exitCode)
    }
} catch {
    let message = (error as? LocalizedError)?.errorDescription ?? String(describing: error)
    try? emit(Envelope(type: .error, requestID: requestID, payload: [
        "error_code": .string("MEDIA_METADATA_ERROR"),
        "message": .string(message),
    ]))
    exit(2)
}
