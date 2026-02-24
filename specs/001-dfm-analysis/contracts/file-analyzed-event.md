# Message Contract: FileAnalyzedEvent

**Version**: 2.0.0 (extends v1.0.0 with DFM report)
**Date**: 2026-02-21
**Feature**: 001-dfm-analysis

## Overview

`FileAnalyzedEvent` is published by `Maliev.GeometryService` after successful geometry analysis of an uploaded 3D file. This contract extends v1.0.0 with optional DFM analysis results.

## Message Envelope

MassTransit envelope format:

```json
{
  "messageId": "uuid",
  "correlationId": "uuid | null",
  "conversationId": "uuid | null",
  "sourceAddress": "string | null",
  "destinationAddress": "string | null",
  "messageType": ["urn:message:Maliev.MessagingContracts:FileAnalyzedEvent"],
  "headers": {},
  "message": { ... }
}
```

## Message Payload (FileAnalyzedMessage)

```json
{
  "fileId": "string",
  "metrics": {
    "volumeCm3": 12.5,
    "supportVolumeCm3": 2.3,
    "surfaceAreaCm2": 45.6,
    "boundingBox": {
      "x": 50.0,
      "y": 30.0,
      "z": 10.0
    },
    "isManifold": true,
    "triangleCount": 5000,
    "eulerNumber": 2,
    "dfmReport": {
      "thinWallCount": 15,
      "thinWallRegions": [
        [25.3, 12.1, 5.0],
        [30.2, 15.8, 5.0]
      ],
      "overhangFaceCount": 120,
      "overhangAreaCm2": 3.45
    }
  },
  "processedAt": "2026-02-21T12:00:00Z",
  "glbStoragePath": "string | null",
  "thumbnailStoragePath": "string | null"
}
```

## Field Specifications

### FileAnalyzedMessage

| Field                    | Type                          | Required | Description                              |
| ------------------------ | ----------------------------- | -------- | ---------------------------------------- |
| `fileId`                 | `string`                      | Yes      | Unique identifier for the analyzed file  |
| `metrics`                | `GeometryMetrics`             | Yes      | Geometry analysis results                |
| `processedAt`            | `datetime (ISO 8601)`         | Yes      | Timestamp of analysis completion         |
| `glbStoragePath`         | `string \| null`              | No       | GCS path to generated GLB file           |
| `thumbnailStoragePath`   | `string \| null`              | No       | GCS path to generated thumbnail          |

### GeometryMetrics

| Field                | Type                  | Required | Description                              |
| -------------------- | --------------------- | -------- | ---------------------------------------- |
| `volumeCm3`          | `float`               | Yes      | Part volume in cm³                       |
| `supportVolumeCm3`   | `float`               | Yes      | Support volume estimate in cm³           |
| `surfaceAreaCm2`     | `float`               | Yes      | Surface area in cm²                      |
| `boundingBox`        | `BoundingBox`         | Yes      | Bounding box dimensions                  |
| `isManifold`         | `boolean`             | Yes      | Mesh watertight status                   |
| `triangleCount`      | `integer`             | Yes      | Number of triangles                      |
| `eulerNumber`        | `integer`             | Yes      | Euler characteristic                     |
| `dfmReport`          | `DfmReport \| null`   | No       | DFM analysis results (v2.0.0)            |

### DfmReport (v2.0.0)

| Field                | Type                 | Required | Description                              |
| -------------------- | -------------------- | -------- | ---------------------------------------- |
| `thinWallCount`      | `integer`            | Yes      | Count of thin wall sample points         |
| `thinWallRegions`    | `array[[x,y,z]]`     | Yes      | Coordinates of thin wall regions (mm)    |
| `overhangFaceCount`  | `integer`            | Yes      | Count of overhang faces                  |
| `overhangAreaCm2`    | `float`              | Yes      | Total overhang area in cm²               |

### BoundingBox

| Field   | Type      | Required | Description          |
| ------- | --------- | -------- | -------------------- |
| `x`     | `float`   | Yes      | X dimension (mm)     |
| `y`     | `float`   | Yes      | Y dimension (mm)     |
| `z`     | `float`   | Yes      | Z dimension (mm)     |

## Backward Compatibility

- **v1.0.0 consumers**: Can safely ignore `dfmReport` field
- **v2.0.0 consumers**: Should handle `dfmReport: null` gracefully (DFM analysis may fail)

## Routing

- **Exchange**: `maliev.events`
- **Routing Key**: `maliev.geometryservice.v1.analysis.completed`
- **Consumer Services**: `Maliev.PricingService`, `Maliev.QuotationService`

## C# Contract Reference

Corresponding C# contract in `Maliev.MessagingContracts`:

```csharp
namespace Maliev.MessagingContracts;

public record FileAnalyzedEvent(
    Guid MessageId,
    FileAnalyzedMessage Message
);

public record FileAnalyzedMessage(
    string FileId,
    GeometryMetrics Metrics,
    DateTime ProcessedAt,
    string? GlbStoragePath = null,
    string? ThumbnailStoragePath = null
);

public record GeometryMetrics(
    double VolumeCm3,
    double SupportVolumeCm3,
    double SurfaceAreaCm2,
    BoundingBox BoundingBox,
    bool IsManifold,
    int TriangleCount,
    int EulerNumber,
    DfmReport? DfmReport = null  // v2.0.0
);

public record DfmReport(
    int ThinWallCount,
    List<double[]> ThinWallRegions,
    int OverhangFaceCount,
    double OverhangAreaCm2
);

public record BoundingBox(
    double X,
    double Y,
    double Z
);
```
