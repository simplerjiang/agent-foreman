$sig = @'
using System;
using System.Runtime.InteropServices;
public class Win {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int n);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
  [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint a, uint b, bool attach);
  [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
}
'@
Add-Type $sig
$p = Get-Process foreman -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
if ($null -eq $p) { Write-Output "no foreman window"; exit }
$h = $p.MainWindowHandle
$fg = [Win]::GetForegroundWindow()
$tid = [Win]::GetCurrentThreadId()
$fgTid = [Win]::GetWindowThreadProcessId($fg, [ref]([uint32]0))
[Win]::AttachThreadInput($fgTid, $tid, $true) | Out-Null
[Win]::ShowWindow($h, 3) | Out-Null
[Win]::SetForegroundWindow($h) | Out-Null
[Win]::AttachThreadInput($fgTid, $tid, $false) | Out-Null
Write-Output ("focused " + $p.Id)
