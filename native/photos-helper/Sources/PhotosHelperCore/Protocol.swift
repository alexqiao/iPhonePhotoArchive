import Foundation

public enum JSONValue: Codable, Equatable, Sendable {
    case string(String)
    case integer(Int)
    case double(Double)
    case boolean(Bool)
    case array([JSONValue])
    case object([String: JSONValue])
    case null

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() { self = .null }
        else if let value = try? container.decode(Bool.self) { self = .boolean(value) }
        else if let value = try? container.decode(Int.self) { self = .integer(value) }
        else if let value = try? container.decode(Double.self) { self = .double(value) }
        else if let value = try? container.decode(String.self) { self = .string(value) }
        else if let value = try? container.decode([JSONValue].self) { self = .array(value) }
        else { self = .object(try container.decode([String: JSONValue].self)) }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .string(let value): try container.encode(value)
        case .integer(let value): try container.encode(value)
        case .double(let value): try container.encode(value)
        case .boolean(let value): try container.encode(value)
        case .array(let value): try container.encode(value)
        case .object(let value): try container.encode(value)
        case .null: try container.encodeNil()
        }
    }
}

public extension JSONValue {
    var stringValue: String? {
        if case .string(let value) = self { return value }
        return nil
    }

    var integerValue: Int? {
        if case .integer(let value) = self { return value }
        return nil
    }

    var doubleValue: Double? {
        if case .double(let value) = self { return value }
        if case .integer(let value) = self { return Double(value) }
        return nil
    }

    var objectValue: [String: JSONValue]? {
        if case .object(let value) = self { return value }
        return nil
    }

    var arrayValue: [JSONValue]? {
        if case .array(let value) = self { return value }
        return nil
    }

    var booleanValue: Bool? {
        if case .boolean(let value) = self { return value }
        return nil
    }

    var stringArrayValue: [String]? {
        guard case .array(let values) = self else { return nil }
        return values.compactMap(\.stringValue)
    }
}

public enum EnvelopeType: String, Codable, Equatable, Sendable {
    case request
    case asset
    case resource
    case progress
    case result
    case error
}

public struct Envelope: Codable, Equatable, Sendable {
    public let schemaVersion: Int
    public let type: EnvelopeType
    public let requestID: String
    public let payload: [String: JSONValue]

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case type
        case requestID = "request_id"
        case payload
    }

    public init(
        schemaVersion: Int = 1,
        type: EnvelopeType,
        requestID: String,
        payload: [String: JSONValue]
    ) {
        self.schemaVersion = schemaVersion
        self.type = type
        self.requestID = requestID
        self.payload = payload
    }
}

public func decodeEnvelope(_ line: String) throws -> Envelope {
    try JSONDecoder().decode(Envelope.self, from: Data(line.utf8))
}

public struct CommandResult: Sendable {
    public let envelope: Envelope
    public let exitCode: Int32
}

public func runCommand(_ command: String, requestID: String) -> CommandResult {
    switch command {
    case "version":
        return CommandResult(
            envelope: Envelope(
                type: .result,
                requestID: requestID,
                payload: [
                    "name": .string("photoarchive-media-helper"),
                    "version": .string("0.6.0"),
                    "schema_version": .integer(1),
                    "phone_schema_version": .integer(2),
                    "photo_library_schema_version": .integer(4),
                ]
            ),
            exitCode: 0
        )
    case "self-test":
        return CommandResult(
            envelope: Envelope(
                type: .result,
                requestID: requestID,
                payload: [
                    "contract": .string("ok"),
                    "phone_contract": .string("jsonl-v2"),
                    "photo_library_contract": .string("jsonl-v4"),
                ]
            ),
            exitCode: 0
        )
    default:
        return CommandResult(
            envelope: Envelope(
                type: .error,
                requestID: requestID,
                payload: ["error_code": .string("UNKNOWN_COMMAND")]
            ),
            exitCode: 64
        )
    }
}

public func encodeEnvelope(_ envelope: Envelope) throws -> String {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
    return String(decoding: try encoder.encode(envelope), as: UTF8.self)
}
