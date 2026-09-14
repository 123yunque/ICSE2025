$ErrorActionPreference = 'Stop'
$reportRoot = $PSScriptRoot
$reportDocx = Join-Path $reportRoot '代码大模型文献读后报告_2025-2026.docx'
$reportPdf = Join-Path $reportRoot '代码大模型文献读后报告_2025-2026.pdf'
$wordApp = $null
$wordDoc = $null
try {
    $wordApp = New-Object -ComObject Word.Application
    $wordApp.Visible = $false
    $wordApp.DisplayAlerts = 0
    $wordApp.AutomationSecurity = 3
    $wordDoc = $wordApp.Documents.Open($reportDocx, $false, $false, $false)
    $wordDoc.Fields.Update() | Out-Null
    $wordDoc.Repaginate()
    $pages = $wordDoc.ComputeStatistics(2)
    $wordDoc.Save()
    $wordDoc.ExportAsFixedFormat($reportPdf, 17)
    Write-Output "Rendered pages: $pages"
    Write-Output $reportPdf
} finally {
    if ($null -ne $wordDoc) { $wordDoc.Close(0); [void][Runtime.InteropServices.Marshal]::ReleaseComObject($wordDoc) }
    if ($null -ne $wordApp) { $wordApp.Quit(); [void][Runtime.InteropServices.Marshal]::ReleaseComObject($wordApp) }
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
}
