# Reads the Cursor chat draft. The draft is printed only as base64 on stdout
# for the pet process. It is not written to a log.
$ErrorActionPreference = "Continue"
$utf8 = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = $utf8
[Console]::InputEncoding = $utf8

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$script:composer = $null

function Test-Composer($el) {
  if ($null -eq $el) { return $false }
  try {
    return [string]$el.Current.ClassName -eq "aislash-editor-input"
  } catch {
    return $false
  }
}

function Find-Composer {
  if (Test-Composer $script:composer) { return $script:composer }
  $script:composer = $null
  $cond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ClassNameProperty,
    "aislash-editor-input"
  )
  $procs = @(Get-Process -Name Cursor -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 })
  foreach ($proc in $procs) {
    try {
      $win = [System.Windows.Automation.AutomationElement]::FromHandle($proc.MainWindowHandle)
      $found = $win.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)
    } catch {
      continue
    }
    if (Test-Composer $found) {
      $script:composer = $found
      return $found
    }
  }
  return $null
}

function Get-ValuePattern($el) {
  $pat = $null
  $ok = $false
  try {
    $ok = $el.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$pat)
  } catch {
    return $null
  }
  if (-not $ok) { return $null }
  return $pat
}

function Write-Line([string]$text) {
  [Console]::Out.WriteLine($text)
  [Console]::Out.Flush()
}

while ($true) {
  $line = [Console]::In.ReadLine()
  if ($null -eq $line) { break }
  if ($line -eq "quit") { break }
  try {
    if ($line -eq "read") {
      $el = Find-Composer
      $pat = Get-ValuePattern $el
      if ($null -eq $pat) { Write-Line "none"; continue }
      $text = [string]$pat.Current.Value
      $raw = $utf8.GetBytes($text)
      Write-Line ("b64:" + [Convert]::ToBase64String($raw))
      continue
    }
    if ($line -eq "focus") {
      $el = Find-Composer
      if ($null -eq $el) { Write-Line "no"; continue }
      try { $el.SetFocus(); Write-Line "ok" } catch { Write-Line "no" }
      continue
    }
    if ($line.StartsWith("append ")) {
      $el = Find-Composer
      $pat = Get-ValuePattern $el
      if ($null -eq $pat) { Write-Line "none"; continue }
      if ($pat.Current.IsReadOnly) { Write-Line "readonly"; continue }
      $extra = $utf8.GetString([Convert]::FromBase64String($line.Substring(7).Trim()))
      $current = [string]$pat.Current.Value
      $trimmed = $current.TrimEnd()
      $joined = $(if ($trimmed.Length -eq 0) { $extra } else { $trimmed + "`r`n" + $extra })
      $pat.SetValue($joined)
      $back = [string]$pat.Current.Value
      $probe = $extra.Substring(0, [Math]::Min(24, $extra.Length))
      if ($back.Contains($probe)) { Write-Line "set" } else { Write-Line "unchanged" }
      continue
    }
    Write-Line "none"
  } catch {
    $script:composer = $null
    Write-Line "none"
  }
}
