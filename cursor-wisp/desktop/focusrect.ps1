# Prints the text cursor rectangle. Never prints the text itself.
$ErrorActionPreference = "Continue"
$utf8 = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = $utf8
[Console]::InputEncoding = $utf8

Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class WispDpi {
  [DllImport("user32.dll")]
  public static extern bool SetProcessDpiAwarenessContext(IntPtr value);
}
"@
[void][WispDpi]::SetProcessDpiAwarenessContext([IntPtr](-4))
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

function Write-Rect([string]$kind, $x, $y, $w, $h) {
  $inv = [Globalization.CultureInfo]::InvariantCulture
  [Console]::Out.WriteLine(("{0} {1} {2} {3} {4}" -f $kind, $x.ToString($inv), $y.ToString($inv), $w.ToString($inv), $h.ToString($inv)))
}

while ($true) {
  $line = [Console]::In.ReadLine()
  if ($null -eq $line) { break }
  if ($line -eq "quit") { break }
  try {
    $el = [System.Windows.Automation.AutomationElement]::FocusedElement
    if ($null -eq $el) { [Console]::Out.WriteLine("none"); continue }
    $wrote = $false
    try {
      $pat = $el.GetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern)
      if ($null -ne $pat) {
        $ranges = $pat.GetSelection()
        if ($ranges -and $ranges.Length -gt 0) {
          $rects = $ranges[0].GetBoundingRectangles()
          if ($rects -and $rects.Length -ge 4) {
            $i = $rects.Length - 4
            $x = [double]$rects[$i]
            $y = [double]$rects[$i + 1]
            $w = [double]$rects[$i + 2]
            $h = [double]$rects[$i + 3]
            if ($h -lt 1) { $h = 18 }
            if ($w -lt 1) { $w = 2 }
            if ($h -lt 120 -and $w -lt 600 -and $y -gt -10000) {
              Write-Rect "caret" $x $y $w $h
              $wrote = $true
            }
          }
        }
      }
    } catch {}
    if (-not $wrote) {
      $r = $el.Current.BoundingRectangle
      if ($r.Width -gt 8 -and $r.Height -gt 8 -and $r.Height -lt 160 -and $r.Width -lt 720) {
        Write-Rect "box" $r.X $r.Y $r.Width $r.Height
      } else {
        [Console]::Out.WriteLine("none")
      }
    }
  } catch {
    [Console]::Out.WriteLine("none")
  }
}
