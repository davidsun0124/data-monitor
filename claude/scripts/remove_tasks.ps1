$tasks = @(
  'DataMonitor-DbHealth-0900','DataMonitor-DbHealth-1800',
  'DataMonitor-FreightVariance-0900','DataMonitor-FreightVariance-1800',
  'DataMonitor-OrderConsistency-0900','DataMonitor-OrderConsistency-1800',
  'DataMonitor-SpmFreightCheck-0900','DataMonitor-SpmFreightCheck-1800'
)
foreach ($t in $tasks) {
  schtasks /delete /tn $t /f
  Write-Host "Deleted: $t"
}
Write-Host "All DataMonitor tasks removed."
