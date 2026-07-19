# Stage 3 Implementation Quick Reference

## Overview

This document provides a quick reference for implementing Stage 3 (Frontend Progressive Loading) of the two-phase DFM architecture.

## Current Status

✅ **Backend (GeometryService)**: Complete and tested
- Two-phase API endpoints working
- All tests passing (35/35)
- Ready for integration

⏳ **BFF Layer (OrderService.Api)**: Not started
- Needs wrapper endpoints for GeometryService

⏳ **Frontend (Maliev.Intranet.Client)**: Not started
- Needs to call new two-phase endpoints
- Needs progressive loading states

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────┐
│ Blazor Frontend (Maliev.Intranet.Client)                    │
│ - PartConfigSidebar.razor (existing process dropdown)       │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ↓ HTTP/JSON
┌─────────────────────────────────────────────────────────────┐
│ BFF API (OrderService.Api) - NEEDS IMPLEMENTATION          │
│ - GeometryAnalysisController.cs (NEW)                      │
│ - DTOs for quality check and DFM analysis (NEW)            │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ↓ HTTP/JSON
┌─────────────────────────────────────────────────────────────┐
│ GeometryService (Python FastAPI) - ✅ COMPLETE             │
│ - POST /uploads/{id}/quality-check                         │
│ - POST /uploads/{id}/dfm/{process_code}                    │
│ - DELETE /uploads/{id}                                     │
└─────────────────────────────────────────────────────────────┘
```

## Step 1: Implement BFF Layer

### File: `Maliev.OrderService.Api/Controllers/GeometryAnalysisController.cs`

**Create new controller** (see full code in `STAGE3_FRONTEND_INTEGRATION.md`):

```csharp
[ApiController]
[Route("api/[controller]")]
public class GeometryAnalysisController : ControllerBase
{
    // POST: api/geometryanalysis/{uploadId}/quality-check
    [HttpPost("{uploadId}/quality-check")]
    public async Task<ActionResult<QualityCheckResponse>> QualityCheck(...) { }

    // POST: api/geometryanalysis/{uploadId}/dfm/{processCode}
    [HttpPost("{uploadId}/dfm/{processCode}")]
    public async Task<ActionResult<DfmAnalysisResponse>> AnalyzeForProcess(...) { }

    // DELETE: api/geometryanalysis/{uploadId}
    [HttpDelete("{uploadId}")]
    public async Task<ActionResult> CleanupUpload(string uploadId) { }
}
```

### Files: DTOs (Create new folder: `Maliev.OrderService.Application/DTOs/`)

1. **`QualityCheckRequest.cs`**
```csharp
public class QualityCheckRequest
{
    public string StlBytes { get; set; } = string.Empty;
    public string? CadBytes { get; set; }
    public string? CadExtension { get; set; }
}
```

2. **`QualityCheckResponse.cs`**
```csharp
public class QualityCheckResponse
{
    public string UploadId { get; set; } = string.Empty;
    public string Status { get; set; } = string.Empty;
    public QualityMetrics Quality { get; set; } = new();
    public bool ReadyForProcessSelection { get; set; }
}
```

3. **`DfmAnalysisResponse.cs`**
```csharp
public class DfmAnalysisResponse
{
    public string UploadId { get; set; } = string.Empty;
    public string ProcessCode { get; set; } = string.Empty;
    public string Status { get; set; } = string.Empty;
    public DfmReport DfmReport { get; set; } = new();
}
```

## Step 2: Update Frontend Component

### File: `Maliev.Intranet.Client/Components/Project/PartConfigSidebar.razor`

**Existing process dropdown is at lines 83-98** - use this, don't create new one.

**Add to `@code` block** (see full code in `STAGE3_FRONTEND_INTEGRATION.md`):

```csharp
// Two-phase DFM analysis state
private bool _isAnalyzingDfm;
private string _analyzingProcessName = string.Empty;
private Dictionary<string, DfmReport> _dfmReports = new();
private string? _currentUploadId;
private QualityMetrics? _qualityMetrics;

// Triggered when user selects process from existing dropdown
private async Task OnProcessChanged(ProcessDto? process)
{
    // Call BFF endpoint for process-specific analysis
    var response = await Http.PostAsJsonAsync(
        $"api/geometryanalysis/{_currentUploadId}/dfm/{process.Code}",
        new { }
    );

    // Display results
    if (response.IsSuccessStatusCode)
    {
        var result = await response.Content.ReadFromJsonAsync<DfmAnalysisResponse>();
        _dfmReports[process.Code] = result.DfmReport;
        StateHasChanged();
    }
}

// Call quality check after file upload
private async Task RunQualityCheck(string uploadId)
{
    var response = await Http.PostAsJsonAsync(
        $"api/geometryanalysis/{uploadId}/quality-check",
        new QualityCheckRequest { StlBytes = "..." }
    );

    if (response.IsSuccessStatusCode)
    {
        var result = await response.Content.ReadFromJsonAsync<QualityCheckResponse>();
        _qualityMetrics = result.Quality;
        _currentUploadId = uploadId;
    }
}
```

**Add loading state display** (after line 98):

```razor
@* Loading indicator during DFM analysis *@
@if (_isAnalyzingDfm)
{
    <MudProgressLinear Color="Color.Primary" Indeterminate="true" />
    <MudText>Analyzing @_analyzingProcessName requirements...</MudText>
}

@* Display DFM results when available *@
@if (_dfmReports.Count > 0)
{
    <MudPaper Outlined="true">
        <MudText>DFM Analysis (@SelectedProcess.Code)</MudText>
        @foreach (var issue in _dfmReports[SelectedProcess.Code].Issues)
        {
            <MudAlert Severity="@GetSeverity(issue.Severity)">
                @issue.Title
            </MudAlert>
        }
    </MudPaper>
}
```

## Step 3: Update Upload Flow

### File: `Maliev.Intranet.Client/Components/Project/UploadModelDialog.razor`

**After successful upload** (line 172):

```csharp
if (response.IsSuccessStatusCode)
{
    var result = await response.Content.ReadFromJsonAsync<BffUploadResponse>();
    if (result != null)
    {
        file.IsCompleted = true;
        file.Progress = 100;
        file.UploadId = result.UploadId;

        // NEW: Trigger quality check (Phase 1)
        await OnUploadComplete?.Invoke(result.UploadId);
    }
}
```

## User Experience Flow

```
1. User uploads file
   ↓
2. Upload completes (existing flow)
   ↓
3. NEW: Quality check runs (<5 seconds)
   ↓
4. NEW: Show preview + enable process dropdown
   ↓
5. User selects "FDM 3D Printing" from existing dropdown
   ↓
6. NEW: "Analyzing FDM requirements..." (<15 seconds)
   ↓
7. NEW: Display FDM-specific issues
   ↓
8. User can change process to "CNC Milling"
   ↓
9. NEW: "Analyzing CNC requirements..." (<15 seconds)
   ↓
10. NEW: Display CNC-specific issues
```

## Performance Comparison

| Metric | Before | After |
|--------|--------|-------|
| Upload | 5s | 5s |
| Analysis | **90s timeout** | **0.01s** |
| Preview | Never | **<5s** |
| Process selection | After 90s | After 5s |
| **Total** | **Never** | **~6s** |

## Testing Checklist

### Manual Testing

1. **Upload file**
   - [ ] Verify upload completes successfully
   - [ ] Check quality check runs automatically
   - [ ] Confirm preview loads quickly

2. **Process selection**
   - [ ] Select FDM from dropdown
   - [ ] Verify "Analyzing FDM..." appears
   - [ ] Check DFM issues display correctly
   - [ ] Change to CNC Milling
   - [ ] Verify new analysis runs
   - [ ] Check CNC issues display correctly

3. **Error handling**
   - [ ] Test with invalid file
   - [ ] Test with timeout
   - [ ] Test network failure

### Automated Testing

```csharp
public class TwoPhaseDfmTests
{
    [Fact]
    public async Task QualityCheck_ShouldCompleteQuickly() { }

    [Fact]
    public async Task ProcessAnalysis_ShouldReturnCorrectProcess() { }

    [Fact]
    public async Task ProcessSelection_ShouldTriggerAnalysis() { }
}
```

## Deployment Checklist

### Backend (OrderService.Api)
- [ ] Add `GeometryAnalysisController` with 3 endpoints
- [ ] Add DTOs for quality check and DFM analysis
- [ ] Update service discovery to include new endpoints
- [ ] Add authentication/authorization
- [ ] Add error handling and logging
- [ ] Write unit tests
- [ ] Update API documentation

### Frontend (Intranet.Client)
- [ ] Update `PartConfigSidebar.razor` with loading states
- [ ] Add DFM results display section
- [ ] Wire up quality check after upload
- [ ] Wire up process selection to trigger analysis
- [ ] Add error handling and user feedback
- [ ] Test with real files

### Integration
- [ ] Test end-to-end workflow
- [ ] Verify performance improvements
- [ ] Test with production files (MEC031233_01.stp)
- [ ] Load testing with concurrent users
- [ ] Monitor for errors and timeouts

## Success Criteria

- [ ] File preview shows in <5 seconds (was 90s)
- [ ] Process selection triggers analysis
- [ ] Analysis completes in <15 seconds
- [ ] DFM results display correctly
- [ ] User can change process and re-analyze
- [ ] No timeout errors on production files
- [ ] Loading states provide good feedback

## Full Documentation

See `STAGE3_FRONTEND_INTEGRATION.md` for:
- Complete code examples
- Detailed implementation guide
- Error handling patterns
- Progressive loading states
- Testing strategies

## Questions?

Refer to:
- `STAGE3_FRONTEND_INTEGRATION.md` - Full implementation guide
- `TWO_PHASE_DFM_COMPLETE.md` - Stages 1-2 summary
- `STAGES_1_2_VERIFICATION_COMPLETE.md` - Verification results

---

**Status**: 🔄 **Ready for Implementation** - Backend complete, frontend/BFF design ready

**Estimated Effort**: 2-3 days for BFF + frontend integration
