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

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$dll = Join-Path $here "WispCaret.dll"
if (-not (Test-Path $dll)) {
  [Console]::Error.WriteLine("WispCaret.dll missing")
  exit 1
}
Add-Type -Path $dll

while ($true) {
  $line = [Console]::In.ReadLine()
  if ($null -eq $line) { break }
  if ($line -eq "quit") { break }
  try {
    $wrote = $false
    $caret = [WispCaret]::TryCaretRange()
    if ($caret) {
      [Console]::Out.WriteLine($caret)
      $wrote = $true
    }
    if (-not $wrote) {
      $caret = [WispCaret]::TrySelection()
      if ($caret) {
        [Console]::Out.WriteLine($caret)
        $wrote = $true
      }
    }
    if (-not $wrote) {
      [Console]::Out.WriteLine("none")
    }
  } catch {
    [Console]::Out.WriteLine("none")
  }
}
