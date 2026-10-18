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

function Find-ValueBubble {
  $cond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ClassNameProperty,
    "aislash-editor-input"
  )
  $procs = @(Get-Process -Name Cursor -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 })
  foreach ($proc in $procs) {
    try {
      $win = [System.Windows.Automation.AutomationElement]::FromHandle($proc.MainWindowHandle)
      $found = $win.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond)
    } catch {
      continue
    }
    foreach ($el in $found) {
      if (-not (Test-Composer $el)) { continue }
      $pat = Get-ValuePattern $el
      if ($null -eq $pat) { continue }
      $text = [string]$pat.Current.Value
      if ($text -match '(?i)(^|\s)@bubble(\s|$)') {
        $script:composer = $el
        return $el
      }
    }
  }
  return $null
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

function Test-BubbleChip($el) {
  try {
    $kids = $el.FindAll(
      [System.Windows.Automation.TreeScope]::Descendants,
      [System.Windows.Automation.Condition]::TrueCondition
    )
  } catch {
    return $false
  }
  foreach ($kid in $kids) {
    $name = ""
    try { $name = [string]$kid.Current.Name } catch { continue }
    if ($name -match '^(?i)@?bubble$') { return $true }
  }
  return $false
}

function Get-DraftText($el) {
  # ValuePattern misses a mention chip. TextPattern and the chip's name still carry it.
  $pat = Get-ValuePattern $el
  $text = $null
  if ($null -ne $pat) { $text = [string]$pat.Current.Value }
  $via = $null
  $tpat = $null
  $ok = $false
  try {
    $ok = $el.TryGetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern, [ref]$tpat)
  } catch {
    $ok = $false
  }
  if ($ok -and $null -ne $tpat) {
    try { $via = [string]$tpat.DocumentRange.GetText(4000) } catch { $via = $null }
  }
  if ($via -and ($null -eq $text -or $text -notmatch '(?i)@bubble') -and $via -match '(?i)(^|\s)@?bubble(\s|$)') {
    $text = $via
  }
  $chip = Test-BubbleChip $el
  if ($null -eq $text -and -not $chip) { return $null }
  if ($null -eq $text) { $text = "" }
  if ($chip -and $text -notmatch '(?i)@bubble') {
    $trimmed = $text.Trim()
    $text = $(if ($trimmed.Length -eq 0) { "@bubble" } else { "@bubble " + $trimmed })
  }
  return $text
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
      if ($null -eq $el) { Write-Line "none"; continue }
      $text = Get-DraftText $el
      if ($null -eq $text) { Write-Line "none"; continue }
      $raw = $utf8.GetBytes($text)
      Write-Line ("b64:" + [Convert]::ToBase64String($raw))
      continue
    }
    if ($line -eq "color") {
      $el = Find-Composer
      if ($null -eq $el) { Write-Line "none"; continue }
      $tpat = $null
      $ok = $false
      try {
        $ok = $el.TryGetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern, [ref]$tpat)
      } catch {
        $ok = $false
      }
      if (-not $ok -or $null -eq $tpat) { Write-Line "nocolor"; continue }
      $range = $null
      try {
        $range = $tpat.DocumentRange.FindText("@bubble", $false, $true)
      } catch {
        $range = $null
      }
      if ($null -eq $range) { Write-Line "missing"; continue }
      $attr = [System.Windows.Automation.TextPattern]::ForegroundColorAttribute
      $val = $null
      try { $val = $range.GetAttributeValue($attr) } catch { Write-Line "nocolor"; continue }
      if ($val -eq [System.Windows.Automation.TextPattern]::MixedAttributeValue) {
        Write-Line "mixed"
        continue
      }
      $color = [int]$val
      $red = $color -band 255
      $green = ($color -shr 8) -band 255
      $blue = ($color -shr 16) -band 255
      Write-Line ("rgb:{0},{1},{2}" -f $red, $green, $blue)
      continue
    }
    if ($line -eq "focus") {
      $el = Find-Composer
      if ($null -eq $el) { Write-Line "no"; continue }
      try { $el.SetFocus(); Write-Line "ok" } catch { Write-Line "no" }
      continue
    }
    if ($line -eq "value") {
      $el = Find-ValueBubble
      if ($null -eq $el) { Write-Line "none"; continue }
      $pat = Get-ValuePattern $el
      if ($null -eq $pat) { Write-Line "none"; continue }
      $raw = $utf8.GetBytes([string]$pat.Current.Value)
      Write-Line ("b64:" + [Convert]::ToBase64String($raw))
      continue
    }
    if ($line.StartsWith("set ")) {
      $el = Find-ValueBubble
      $pat = Get-ValuePattern $el
      if ($null -eq $pat) { Write-Line "none"; continue }
      if ($pat.Current.IsReadOnly) { Write-Line "readonly"; continue }
      $next = $utf8.GetString([Convert]::FromBase64String($line.Substring(4).Trim()))
      $pat.SetValue($next)
      $back = ([string]$pat.Current.Value) -replace "`r`n", "`n"
      $want = $next -replace "`r`n", "`n"
      if ($back -eq $want) { Write-Line "set" } else { Write-Line "unchanged" }
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
