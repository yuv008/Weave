using System.Windows.Forms;
using Microsoft.Web.WebView2.WinForms;

namespace WeaveWisp
{
    public class PetForm : Form
    {
        protected override CreateParams CreateParams
        {
            get
            {
                CreateParams cp = base.CreateParams;
                // No redirection bitmap keeps the page transparent.
                // Tool window avoids the hidden owner that blocks topmost.
                // Topmost keeps the pet above ordinary windows.
                cp.ExStyle |= 0x00200000 | 0x00000080 | 0x00000008;
                return cp;
            }
        }

        protected override bool ShowWithoutActivation
        {
            get { return true; }
        }
    }

    public class AlphaView : WebView2
    {
        protected override CreateParams CreateParams
        {
            get
            {
                CreateParams cp = base.CreateParams;
                cp.ExStyle |= 0x00200000;
                return cp;
            }
        }
    }
}
