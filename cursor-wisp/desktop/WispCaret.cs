using System;
using System.Globalization;
using System.Runtime.InteropServices;

public static class WispCaret
{
    public static string LastError = "";
    static string FormatRect(double x, double y, double w, double h)
    {
        var inv = CultureInfo.InvariantCulture;
        if (h < 1) h = 18;
        if (w < 1) w = 2;
        if (h >= 200 || w >= 800 || y <= -10000) return null;
        return string.Format(inv, "caret {0} {1} {2} {3}", x, y, w, h);
    }

    static string FromRects(object rects)
    {
        var arr = rects as double[];
        if (arr == null || arr.Length < 4) return null;
        int i = arr.Length - 4;
        return FormatRect(arr[i], arr[i + 1], arr[i + 2], arr[i + 3]);
    }

    public static string TryCaretRange()
    {
        try
        {
            var uiaType = Type.GetTypeFromCLSID(new Guid("FF48DBA4-60EF-4201-AA87-54103EEF594E"));
            dynamic uia = Activator.CreateInstance(uiaType);
            dynamic el = uia.GetFocusedElement();
            if (el == null) { LastError = "no-focus"; return null; }
            Guid iid = new Guid("5347AD7B-C355-46F8-ACC5-2500598090CC");
            object pat = el.GetCurrentPatternAs(10024, ref iid);
            if (pat == null) { LastError = "no-pattern"; return null; }
            dynamic tp2 = pat;
            object active = false;
            dynamic range = tp2.GetCaretRange(out active);
            if (range == null) { LastError = "no-range"; return null; }
            object rects = range.GetBoundingRectangles();
            string line = FromRects(rects);
            if (line == null) LastError = "bad-rects " + (rects == null ? "null" : rects.GetType().Name);
            return line;
        }
        catch (Exception error)
        {
            LastError = error.GetType().Name + ": " + error.Message;
            return null;
        }
    }

    public static string TrySelection()
    {
        try
        {
            var el = System.Windows.Automation.AutomationElement.FocusedElement;
            if (el == null) return null;
            var pat = el.GetCurrentPattern(System.Windows.Automation.TextPattern.Pattern)
                as System.Windows.Automation.TextPattern;
            if (pat == null) return null;
            var ranges = pat.GetSelection();
            if (ranges == null || ranges.Length == 0) return null;
            return FromRects(ranges[0].GetBoundingRectangles());
        }
        catch (Exception error)
        {
            LastError = error.GetType().Name + ": " + error.Message;
            return null;
        }
    }
}
